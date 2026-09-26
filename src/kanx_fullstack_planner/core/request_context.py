from contextvars import ContextVar, Token

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
user_id_context: ContextVar[str | None] = ContextVar("user_id", default=None)
organization_id_context: ContextVar[str | None] = ContextVar("organization_id", default=None)
trace_id_context: ContextVar[str] = ContextVar("trace_id", default="-")
span_id_context: ContextVar[str] = ContextVar("span_id", default="-")


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


def set_business_context(user_id: str | None = None, organization_id: str | None = None) -> None:
    user_id_context.set(user_id)
    organization_id_context.set(organization_id)


def get_user_id() -> str | None:
    return user_id_context.get()


def get_organization_id() -> str | None:
    return organization_id_context.get()


def set_trace_context(trace_id: str, span_id: str) -> None:
    trace_id_context.set(trace_id)
    span_id_context.set(span_id)


def get_trace_context() -> tuple[str, str]:
    return trace_id_context.get(), span_id_context.get()
