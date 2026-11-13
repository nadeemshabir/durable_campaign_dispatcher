"""Webhook receiver: signature verification, idempotency, validation. Runs the ASGI app
in-process (same thread), exactly like production."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from solution.dispatcher.rules import ACTIVE, COMPLETED
from solution.dispatcher.webhooks import create_app, sign, verify_signature

from .conftest import ended, ist, only_leads

T0 = ist("2026-11-12 17:30")


@pytest.fixture
def setup(store, cfg, margins):
    only_leads(store, ("c_acme_renewals", "ren_0001"))
    [it] = store.reserve_dials(T0, 0.0, margins)
    store.mark_placed(it.request_id, "CA1")
    app = create_app(store, cfg.webhook_secret, notify=lambda: None)
    payload = ended(it, "completed", "interested", ended_at=ist("2026-11-12 17:36"), sid="CA1")
    return app, store, cfg.webhook_secret, payload


def post(app, body: bytes, signature: str | None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            headers = {"Content-Type": "application/json"}
            if signature is not None:
                headers["X-AU-Signature"] = signature
            return await c.post("/webhooks/telephony", content=body, headers=headers)
    return asyncio.run(go())


def lead_state(store):
    return store.lead("c_acme_renewals", "ren_0001")["state"]


def test_valid_webhook_is_applied(setup):
    app, store, secret, payload = setup
    body = json.dumps(payload).encode()
    r = post(app, body, sign(secret, body))
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    assert lead_state(store) == COMPLETED


@pytest.mark.parametrize("signature", [None, "", "0" * 64, "not-hex"])
def test_bad_or_missing_signature_is_rejected_and_ignored(setup, signature):
    app, store, _, payload = setup
    r = post(app, json.dumps(payload).encode(), signature)
    assert r.status_code == 401
    assert lead_state(store) == ACTIVE


def test_signature_covers_the_exact_raw_bytes(setup):
    app, store, secret, payload = setup
    signed = json.dumps(payload).encode()
    reformatted = json.dumps(payload, indent=2).encode()  # same JSON, different bytes
    assert post(app, reformatted, sign(secret, signed)).status_code == 401
    assert lead_state(store) == ACTIVE


def test_duplicate_delivery_is_harmless(setup):
    app, store, secret, payload = setup
    body = json.dumps(payload).encode()
    assert post(app, body, sign(secret, body)).json()["outcome"] == "applied"
    r = post(app, body, sign(secret, body))
    assert r.status_code == 200 and r.json()["outcome"] == "duplicate"
    assert store.attempts("c_acme_renewals", "ren_0001") == 1


def test_missing_fields_are_a_400(setup):
    app, store, secret, payload = setup
    payload = {**payload, "ended_at": None}
    body = json.dumps(payload).encode()
    assert post(app, body, sign(secret, body)).status_code == 400
    assert lead_state(store) == ACTIVE


def test_other_events_are_acknowledged_and_ignored(setup):
    app, store, secret, payload = setup
    body = json.dumps({**payload, "event": "call.ringing"}).encode()
    assert post(app, body, sign(secret, body)).status_code == 200
    assert lead_state(store) == ACTIVE


def test_verify_signature_is_case_and_whitespace_tolerant():
    body = b'{"a":1}'
    sig = sign("s3cret", body)
    assert verify_signature("s3cret", body, sig.upper() + " ")
    assert not verify_signature("other", body, sig)


def test_status_endpoint(setup):
    app, *_ = setup

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.get("/status")
    data = asyncio.run(go()).json()
    assert data["open_calls_by_tenant"] == {"acme_insurance": 1}
    assert data["unfinished_leads"] == 1
