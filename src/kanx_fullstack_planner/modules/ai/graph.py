from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class State(TypedDict):
    value: str


def _step(state: State) -> State:
    return {"value": f"{state['value']}-graph"}


graph = StateGraph(State)
graph.add_node("step", _step)
graph.add_edge(START, "step")
graph.add_edge("step", END)
compiled_graph = graph.compile()


async def run_smoke_graph(value: str) -> str:
    result = await compiled_graph.ainvoke({"value": value})
    return str(result["value"])
