from __future__ import annotations

from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator
from typing_extensions import TypedDict

from ..ai.provider import structured_output

TASK = "infer_project_description"


class DescriptionOption(BaseModel):
    description: str = Field(min_length=1)


class DescriptionOptions(BaseModel):
    options: list[DescriptionOption] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_options(self) -> DescriptionOptions:
        descriptions = [option.description.strip() for option in self.options]
        if any(not description for description in descriptions) or len(set(descriptions)) != 3:
            raise ValueError("Project descriptions must be non-empty and distinct")
        for option, description in zip(self.options, descriptions, strict=True):
            option.description = description
        return self


class DescriptionState(TypedDict, total=False):
    name: str
    language: str
    options: list[dict[str, str]]


class DescriptionProvider(Protocol):
    async def generate(self, name: str, language: str) -> DescriptionOptions: ...


class DefaultDescriptionProvider:
    async def generate(self, name: str, language: str) -> DescriptionOptions:
        output_language = "English" if language == "en" else "简体中文"
        result = await structured_output(
            DescriptionOptions,
            [
                (
                    "system",
                    f"你是产品分析师。根据项目名称推断项目用途，用{output_language}给出恰好三条不同的项目描述。"
                    "每条用一到两句话说明目标用户、核心用途或业务场景。"
                    "名称信息不足时只做合理推断，不编造确定的技术栈、客户或已有功能。"
                    '只返回 JSON 对象，格式为 {"options": [{"description": "..."}]}，'
                    "其中 options 恰好包含三项，不要输出 JSON 以外的内容。",
                ),
                ("user", f"项目名称：{name}"),
            ],
            task=TASK,
        )
        return DescriptionOptions.model_validate(result)


def build_description_graph(provider: DescriptionProvider | None = None) -> Any:
    llm = provider or DefaultDescriptionProvider()

    async def infer_description(state: DescriptionState) -> DescriptionState:
        result = DescriptionOptions.model_validate(
            await llm.generate(state["name"], state["language"])
        )
        return {"options": [option.model_dump() for option in result.options]}

    graph = StateGraph(DescriptionState)
    graph.add_node(TASK, infer_description)
    graph.add_edge(START, TASK)
    graph.add_edge(TASK, END)
    return graph.compile()


compiled_description_graph = build_description_graph()


async def generate_description_options(name: str, language: str) -> list[dict[str, str]]:
    result = await compiled_description_graph.ainvoke({"name": name, "language": language})
    options = DescriptionOptions.model_validate({"options": result["options"]})
    return [option.model_dump() for option in options.options]
