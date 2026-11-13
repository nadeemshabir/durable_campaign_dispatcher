"""Thin async client for the telephony provider API, plus the pure response classifier."""
from __future__ import annotations

from dataclasses import dataclass

import httpx


class Provider:
    def __init__(self, base_url: str, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Default timeout for GETs (lookups, polls, clock). POST /v1/calls passes its own
        # short timeout: the dialer gives up after 0.5 s and re-sends the same request_id.
        self.http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def clock(self) -> dict:
        r = await self.http.get("/v1/clock")
        r.raise_for_status()
        return r.json()

    async def create_call(self, payload: dict, timeout: float) -> httpx.Response:
        return await self.http.post("/v1/calls", json=payload, timeout=timeout)

    async def get_call(self, call_sid: str) -> httpx.Response:
        return await self.http.get(f"/v1/calls/{call_sid}")

    async def find_call(self, request_id: str) -> httpx.Response:
        return await self.http.get("/v1/calls", params={"request_id": request_id})


@dataclass(frozen=True)
class Placement:
    kind: str                   # 'accepted' | 'not_placed' | 'rejected' | 'unknown'
    detail: str = ""
    error: str = ""             # provider error code for rejections, e.g. 'dnd_violation'
    call_sid: str | None = None
    result: dict | None = None  # present if the call had already ended (e.g. 200 replay)


def _error_code(body: dict) -> str:
    # Rule rejections look like {"error": ...}; FastAPI HTTPExceptions like {"detail": {"error": ...}}.
    if isinstance(body.get("error"), str):
        return body["error"]
    detail = body.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return detail["error"]
    return ""


def classify_create(status_code: int, body: object) -> Placement:
    """What a POST /v1/calls response tells us about whether a call now exists."""
    body = body if isinstance(body, dict) else {}
    if status_code in (200, 201):  # 200 = this request_id already existed: same call, safe
        if not body.get("call_sid"):
            return Placement("unknown", detail=f"{status_code} without call_sid")
        return Placement("accepted", call_sid=body["call_sid"], result=body.get("result"))
    if status_code == 503:         # documented: transient, call NOT placed
        return Placement("not_placed", detail="503 service unavailable")
    if 400 <= status_code < 500:   # rule broken (recorded as a violation) or bad request: not placed
        code = _error_code(body)
        return Placement("rejected", detail=f"{status_code} {code} {body.get('detail', '')}".strip(), error=code)
    # 504 (documented as "may or may not have been placed") and any other 5xx: unknown.
    return Placement("unknown", detail=f"{status_code} {_error_code(body)}".strip())
