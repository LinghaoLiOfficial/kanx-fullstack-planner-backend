from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from .config import Settings
from .request_context import (
    get_organization_id,
    get_request_id,
    get_trace_context,
    get_user_id,
)

_SENSITIVE = re.compile(
    r"(password|token|secret|api[_-]?key|authorization|cookie|set-cookie)", re.IGNORECASE
)


def redact(value: Any, fields: set[str] | None = None) -> Any:
    fields = fields or {"password", "token", "secret", "api_key", "authorization", "cookie"}
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.casefold().replace("-", "_") in fields or _SENSITIVE.search(key)
            else redact(item, fields)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, fields) for item in value]
    return value


class ContextFilter(logging.Filter):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        record.trace_id, record.span_id = get_trace_context()
        record.user_id = get_user_id()
        record.organization_id = get_organization_id()
        record.app_name = self.settings.app_name
        record.app_env = self.settings.app_env
        record.duration_ms = getattr(record, "duration_ms", None)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, fields: set[str]) -> None:
        super().__init__()
        self.fields = fields

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", get_request_id()),
            "trace_id": getattr(record, "trace_id", get_trace_context()[0]),
            "span_id": getattr(record, "span_id", get_trace_context()[1]),
            "user_id": getattr(record, "user_id", get_user_id()),
            "organization_id": getattr(record, "organization_id", get_organization_id()),
            "app_name": getattr(record, "app_name", None),
            "app_env": getattr(record, "app_env", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "http_method": getattr(record, "http_method", None),
            "http_path": getattr(record, "http_path", None),
            "http_status": getattr(record, "http_status", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload, self.fields), ensure_ascii=True)


class OptionalHttpExporter(logging.Handler):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.url, self.timeout = (
            settings.observability_exporter_url,
            settings.observability_exporter_timeout_seconds,
        )
        self.kind = settings.observability_exporter_kind.casefold()

    def emit(self, record: logging.LogRecord) -> None:
        if not self.url:
            return
        try:
            rendered = self.format(record)
            if self.kind == "loki":
                body = json.dumps(
                    {
                        "streams": [
                            {
                                "stream": {
                                    "app": str(getattr(record, "app_name", "app")),
                                    "environment": str(getattr(record, "app_env", "unknown")),
                                    "level": record.levelname.casefold(),
                                },
                                "values": [[str(time.time_ns()), rendered]],
                            }
                        ]
                    }
                ).encode()
            else:
                body = (rendered + ("\n" if self.kind == "elasticsearch" else "")).encode()
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={"content-type": "application/json"},
            )
            urllib.request.urlopen(request, timeout=self.timeout).close()
        except (OSError, ValueError):
            self.handleError(record)


def configure_logging(settings: Settings) -> None:
    fields = {item.strip().casefold() for item in settings.log_redact_fields.split(",")}
    context = ContextFilter(settings)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        handlers.append(
            RotatingFileHandler(
                settings.log_file,
                maxBytes=settings.log_file_max_bytes,
                backupCount=settings.log_file_backup_count,
            )
        )
    if settings.observability_exporter_url:
        handlers.append(OptionalHttpExporter(settings))
    formatter = JsonFormatter(fields)
    for handler in handlers:
        handler.addFilter(context)
        handler.setFormatter(formatter)
    logging.basicConfig(level=settings.log_level, handlers=handlers, force=True)
