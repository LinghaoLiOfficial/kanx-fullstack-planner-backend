from __future__ import annotations

import argparse
import importlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .core.config import Settings, get_settings
from .core.installed_modules import INSTALLED_MODULES

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yml"


def _run(command: list[str], *, env: dict[str, str] | None = None, quiet: bool = False) -> None:
    output = subprocess.DEVNULL if quiet else None
    subprocess.run(command, check=True, cwd=ROOT, env=env, stdout=output, stderr=output, text=True)


def _has(name: str) -> bool:
    return name in INSTALLED_MODULES


def _compose_environment(settings: Settings) -> dict[str, str]:
    environment = {
        **os.environ,
        "APP_SLUG": settings.app_slug,
        "DATABASE_USER": settings.database_user,
        "DATABASE_PASSWORD": settings.database_password,
        "DATABASE_NAME": settings.database_name,
        "DATABASE_HOST_PORT": str(settings.database_host_port),
        "TEMPORAL_HOST_PORT": str(settings.temporal_host_port),
        "TEMPORAL_UI_PORT": str(settings.temporal_ui_port),
        "TEMPORAL_NAMESPACE": settings.temporal_namespace,
        "EMAIL_SMTP_HOST_PORT": str(settings.email_smtp_host_port),
        "EMAIL_UI_PORT": str(settings.email_ui_port),
        "STORAGE_HOST_PORT": str(settings.storage_host_port),
        "STORAGE_CONSOLE_PORT": str(settings.storage_console_port),
    }
    if _has("storage"):
        storage_module = importlib.import_module(f"{__package__}.modules.storage.settings")
        storage = storage_module.get_storage_settings()
        environment.update(
            {
                "STORAGE_ACCESS_KEY": storage.access_key.get_secret_value(),
                "STORAGE_SECRET_KEY": storage.secret_key.get_secret_value(),
                "STORAGE_BUCKET": storage.bucket,
            }
        )
    return environment


def _compose_command(settings: Settings) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        settings.docker_project_name,
        "-f",
        str(COMPOSE_FILE),
    ]
    if _has("temporal"):
        command.extend(["--profile", "workflow"])
    if _has("email"):
        command.extend(["--profile", "identity"])
    if _has("storage"):
        command.extend(["--profile", "saas"])
    return command


def _compose(settings: Settings, *arguments: str) -> None:
    _run([*_compose_command(settings), *arguments], env=_compose_environment(settings))


def check_prerequisites() -> None:
    for command in ("uv", "docker"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Required command is not installed: {command}")
    _run(["docker", "info"], quiet=True)
    _run(["docker", "compose", "version"], quiet=True)


def ensure_port_available(settings: Settings, port: int, service: str) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return
        except OSError:
            pass
    owners = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"publish={port}",
            "--format",
            '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if f"{settings.docker_project_name}/{service}" not in owners:
        raise RuntimeError(
            f"Port {port} for {service} is occupied outside {settings.docker_project_name}"
        )


def wait_for_port(port: int, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for localhost:{port}")


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::", "[::]", "localhost"}:
        return "127.0.0.1"
    return host.split(":", 1)[0] if host.count(":") == 1 else host


def _url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{_display_host(host)}:{port}"


def service_urls(settings: Settings, *, include_local: bool = True) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    if include_local:
        urls.append(("API", _url("http", settings.api_host, settings.api_port)))
        if _has("ai"):
            urls.append(("Gradio", _url("http", settings.gradio_host, settings.gradio_port)))
    urls.append(("PostgreSQL", _url("postgresql", "localhost", settings.database_host_port)))
    if _has("temporal"):
        urls.extend(
            [
                ("Temporal", _url("temporal", "localhost", settings.temporal_host_port)),
                ("Temporal UI", f"http://localhost:{settings.temporal_ui_port}"),
            ]
        )
    if _has("email"):
        urls.extend(
            [
                ("Mailpit SMTP", _url("smtp", "localhost", settings.email_smtp_host_port)),
                ("Mailpit UI", _url("http", "localhost", settings.email_ui_port)),
            ]
        )
    if _has("storage"):
        urls.extend(
            [
                ("MinIO API", _url("http", "localhost", settings.storage_host_port)),
                ("MinIO Console", _url("http", "localhost", settings.storage_console_port)),
            ]
        )
    return urls


def print_service_urls(settings: Settings, *, include_local: bool = True) -> None:
    print("\nServices started:")
    for label, url in service_urls(settings, include_local=include_local):
        print(f"  {label:<16} {url}")
    print(flush=True)


def infra_up(settings: Settings) -> None:
    check_prerequisites()
    targets = [(settings.database_host_port, "postgres")]
    services = ["postgres"]
    if _has("temporal"):
        targets.extend(
            [
                (settings.temporal_host_port, "temporal"),
                (settings.temporal_ui_port, "temporal-ui"),
            ]
        )
        services.extend(["temporal", "temporal-ui"])
    if _has("email"):
        targets.extend(
            [
                (settings.email_smtp_host_port, "mailpit"),
                (settings.email_ui_port, "mailpit"),
            ]
        )
        services.append("mailpit")
    if _has("storage"):
        targets.extend(
            [
                (settings.storage_host_port, "minio"),
                (settings.storage_console_port, "minio"),
            ]
        )
        services.append("minio")
    for port, service in targets:
        ensure_port_available(settings, port, service)
    _compose(settings, "up", "-d", "--wait", *services)
    for port, _service in targets:
        wait_for_port(port)
    if _has("temporal") and settings.temporal_auto_register_namespace:
        _compose(settings, "run", "--rm", "namespace")
    if _has("storage"):
        _compose(settings, "run", "--rm", "minio-init")


def _has_unknown_migration_revision() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode != 0 and "Can't locate revision identified by" in output


def migrate(settings: Settings) -> None:
    if _has_unknown_migration_revision():
        if settings.deployed:
            raise RuntimeError(
                "Database uses a migration revision missing from this project. "
                "Refusing to delete deployed data; restore the matching migration chain."
            )
        print(
            "Detected an incompatible local database migration revision; "
            "resetting local infrastructure volumes."
        )
        _compose(settings, "down", "--volumes", "--remove-orphans")
        infra_up(settings)
    _run([sys.executable, "-m", "alembic", "upgrade", "head"])


def run_application_processes(settings: Settings) -> None:
    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "kanx_fullstack_planner.app:app",
            "--reload",
            "--host",
            settings.api_host,
            "--port",
            str(settings.api_port),
        ]
    ]
    if _has("temporal"):
        commands.append([sys.executable, "-m", "kanx_fullstack_planner.modules.temporal.worker"])
    if _has("jobs"):
        commands.append([sys.executable, "-m", "kanx_fullstack_planner.modules.jobs.dispatcher"])
    if _has("ai"):
        commands.append([sys.executable, "-m", "kanx_fullstack_planner.gradio_app"])
    processes = [subprocess.Popen(command, cwd=ROOT) for command in commands]
    wait_for_port(settings.api_port)
    if _has("ai"):
        wait_for_port(settings.gradio_port)
    print_service_urls(settings)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping and all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if not stopping and any(process.returncode != 0 for process in processes):
        raise RuntimeError("An application process exited unexpectedly; its peers were stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local development orchestration")
    parser.add_argument(
        "command",
        choices=("dev", "infra-up", "infra-down", "infra-status", "infra-reset", "smoke"),
    )
    command = parser.parse_args().command
    settings = get_settings()
    try:
        if command == "dev":
            infra_up(settings)
            migrate(settings)
            run_application_processes(settings)
        elif command == "infra-up":
            infra_up(settings)
            print_service_urls(settings, include_local=False)
        elif command == "infra-down":
            check_prerequisites()
            _compose(settings, "down", "--remove-orphans")
        elif command == "infra-status":
            check_prerequisites()
            _compose(settings, "ps")
        elif command == "infra-reset":
            check_prerequisites()
            _compose(settings, "down", "--volumes", "--remove-orphans")
        else:
            print(f"smoke: ok ({', '.join(INSTALLED_MODULES)})")
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
