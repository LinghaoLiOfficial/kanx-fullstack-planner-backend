import socket

import pytest

from kanx_fullstack_planner.core.config import Settings
from kanx_fullstack_planner.dev import ensure_local_application_ports


def _settings(api_port: int, gradio_port: int) -> Settings:
    return Settings(api_port=api_port, gradio_port=gradio_port, _env_file=None)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_local_application_port_check_accepts_ports_without_listeners() -> None:
    ensure_local_application_ports(_settings(_free_port(), _free_port()))


def test_local_application_port_check_rejects_active_listener() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied_port = int(listener.getsockname()[1])

        with pytest.raises(RuntimeError, match=f"Port {occupied_port} for API"):
            ensure_local_application_ports(_settings(occupied_port, _free_port()))
