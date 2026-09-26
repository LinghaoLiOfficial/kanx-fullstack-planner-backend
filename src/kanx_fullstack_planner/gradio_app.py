from typing import cast

import gradio as gr

from .core.config import get_settings
from .modules.ai.graph import run_smoke_graph


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="kanx-fullstack-planner AI") as demo:
        value = gr.Textbox(label="Input", value="hello")
        output = gr.Textbox(label="Output")
        gr.Button("Run").click(run_smoke_graph, inputs=value, outputs=output)
    return cast(gr.Blocks, demo)


def main() -> None:
    settings = get_settings()
    build_demo().launch(
        server_name=settings.gradio_host,
        server_port=settings.gradio_port,
    )


if __name__ == "__main__":
    main()
