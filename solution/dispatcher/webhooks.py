"""HTTP side of the dispatcher: the provider's result webhook, and a /status page."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .store import Store

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("request_id", "status", "ended_at")


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """X-AU-Signature is hex HMAC-SHA256 of the *raw* body. Constant-time comparison."""
    if not signature:
        return False
    return hmac.compare_digest(sign(secret, body).encode(), signature.strip().lower().encode())


def create_app(store: Store, secret: str, notify: Callable[[], None],
               status_extra: Callable[[], dict] = dict) -> FastAPI:
    app = FastAPI(title="Campaign dispatcher", docs_url=None, redoc_url=None, openapi_url=None)

    # `async def` on purpose: handlers run on the event loop thread, never in a thread pool,
    # so they can't interleave with the scheduler inside a store transaction.
    @app.post("/webhooks/telephony")
    async def telephony(request: Request):
        raw = await request.body()
        if not verify_signature(secret, raw, request.headers.get("X-AU-Signature")):
            log.warning("rejected webhook with a bad signature")
            return JSONResponse({"error": "bad_signature"}, status_code=401)
        try:
            event = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(event, dict) or event.get("event") != "call.completed":
            return {"ok": True, "ignored": True}
        missing = [k for k in REQUIRED_FIELDS if not event.get(k)]
        if missing:
            return JSONResponse({"error": "missing_fields", "fields": missing}, status_code=400)
        # If this raises, FastAPI answers 500 and the provider redelivers: nothing is lost,
        # because the store transaction rolled back.
        outcome = store.apply_result(event, source="webhook")
        if outcome == "applied":
            notify()
        return {"ok": True, "outcome": outcome}

    @app.get("/status")
    async def status():
        return {**status_extra(), **store.status()}

    return app
