from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kanx_fullstack_planner.app import app
from kanx_fullstack_planner.modules.projects.description_graph import (
    TASK,
    DefaultDescriptionProvider,
    DescriptionOption,
    DescriptionOptions,
    build_description_graph,
)


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate(self, name: str, language: str) -> DescriptionOptions:
        self.calls.append((name, language))
        return DescriptionOptions(
            options=[
                DescriptionOption(description=f"{name} option {index}") for index in range(1, 4)
            ]
        )


@pytest.mark.asyncio
async def test_default_provider_requests_json_output_in_prompt() -> None:
    response = DescriptionOptions(
        options=[DescriptionOption(description=f"Option {index}") for index in range(3)]
    )
    with patch(
        "kanx_fullstack_planner.modules.projects.description_graph.structured_output",
        return_value=response,
    ) as generate:
        result = await DefaultDescriptionProvider().generate("Inventory", "en")

    assert result == response
    args, kwargs = generate.call_args
    assert args[0] is DescriptionOptions
    assert "json" in args[1][0][1].lower()
    assert args[1][1] == ("user", "项目名称：Inventory")
    assert kwargs["task"] == TASK


@pytest.mark.asyncio
async def test_description_graph_has_one_llm_node() -> None:
    provider = FakeProvider()
    graph = build_description_graph(provider)

    assert set(graph.get_graph().nodes) == {"__start__", TASK, "__end__"}
    result = await graph.ainvoke({"name": "Inventory", "language": "en"})

    assert provider.calls == [("Inventory", "en")]
    assert result["options"] == [
        {"description": f"Inventory option {index}"} for index in range(1, 4)
    ]


@pytest.mark.parametrize("values", [["a", "b"], ["a", "a", "c"], ["a", " ", "c"]])
def test_description_options_reject_invalid_candidates(values: list[str]) -> None:
    with pytest.raises(ValidationError):
        DescriptionOptions.model_validate({"options": [{"description": value} for value in values]})


def test_description_options_endpoint_passes_normalized_input() -> None:
    async def generate(name: str, language: str) -> list[dict[str, str]]:
        assert (name, language) == ("Inventory", "en")
        return [{"description": f"Option {index}"} for index in range(3)]

    with patch(
        "kanx_fullstack_planner.modules.projects.api.generate_description_options",
        side_effect=generate,
    ):
        with TestClient(app) as client:
            response = client.post(
                "/v1/projects/description-options",
                json={"name": " Inventory ", "llm_prompt_language": "en"},
            )

    assert response.status_code == 200
    assert response.json() == {
        "options": [{"description": f"Option {index}"} for index in range(3)]
    }


def test_description_options_endpoint_reports_missing_ai_configuration() -> None:
    with patch(
        "kanx_fullstack_planner.modules.projects.api.generate_description_options",
        side_effect=RuntimeError("AI_API_KEY is required to create a chat model"),
    ):
        with TestClient(app) as client:
            response = client.post("/v1/projects/description-options", json={"name": "Inventory"})

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "AI service is not configured"
