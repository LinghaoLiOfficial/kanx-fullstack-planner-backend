import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

Check = Callable[[], Awaitable[None]]


async def readiness(checks: Mapping[str, Check], timeout: float) -> tuple[dict[str, Any], int]:
    results = await asyncio.gather(
        *(asyncio.wait_for(check(), timeout) for check in checks.values()), return_exceptions=True
    )
    states = {
        name: "ok" if not isinstance(result, BaseException) else "error"
        for name, result in zip(checks, results, strict=True)
    }
    status = 200 if all(value == "ok" for value in states.values()) else 503
    return {"status": "ok" if status == 200 else "error", "checks": states}, status
