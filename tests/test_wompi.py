import asyncio
import hashlib
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from app.gateways.wompi import WompiGateway, WompiSettings, wompi_time

SETTINGS = WompiSettings(
    public_key="pub_test_x",
    private_key="prv_test_x",
    integrity_secret="test_integrity_x",
    events_secret="test_events_x",
    api_url="https://sandbox.wompi.co/v1",
    checkout_url="https://checkout.wompi.co/p/",
    redirect_url="http://lvh.me:5173/pago/resultado",
    timeout_seconds=5,
)
EXPIRES = datetime(2026, 9, 25, 12, 30, tzinfo=timezone.utc)
TRANSACTION = {
    "id": "12-34-56",
    "reference": "cinemaplus-abc",
    "status": "APPROVED",
    "amount_in_cents": 3_760_000,
    "currency": "COP",
    "payment_method_type": "CARD",
    "payment_method": {"extra": {"last_four": "4242"}},
    "created_at": "2026-09-25T12:01:00.000Z",
}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def gateway(handler=None) -> WompiGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return WompiGateway(SETTINGS, client)


def test_wompi_time_is_utc_with_millis_and_z():
    assert wompi_time(EXPIRES) == "2026-09-25T12:30:00.000Z"


def test_checkout_url_signs_reference_amount_currency_and_expiration():
    url = gateway().checkout_url("cinemaplus-abc", 3_760_000, "COP", EXPIRES, "ana@example.com")
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://checkout.wompi.co/p/?")
    assert params["expiration-time"] == "2026-09-25T12:30:00.000Z"
    assert params["signature:integrity"] == sha("cinemaplus-abc3760000COP2026-09-25T12:30:00.000Ztest_integrity_x")
    assert params["redirect-url"] == SETTINGS.redirect_url
    assert params["customer-data:email"] == "ana@example.com"


def event(checksum=None, timestamp=1727265600):
    tx = dict(TRANSACTION)
    body = {
        "event": "transaction.updated",
        "data": {"transaction": tx},
        "signature": {"properties": ["transaction.id", "transaction.status", "transaction.amount_in_cents"]},
        "timestamp": timestamp,
    }
    body["signature"]["checksum"] = checksum or sha(f"12-34-56APPROVED3760000{timestamp}test_events_x")
    return body


def test_valid_event_returns_the_outcome():
    valid, outcome = gateway().verify_event(event())
    assert valid
    assert outcome.reference == "cinemaplus-abc"
    assert outcome.status == "approved"
    assert outcome.last_four == "4242"


def test_tampered_event_is_rejected():
    body = event()
    body["data"]["transaction"]["amount_in_cents"] = 100
    assert gateway().verify_event(body) == (False, None)
    assert gateway().verify_event({"event": "x"}) == (False, None)
    assert gateway().verify_event("nope") == (False, None)


def test_fetch_uses_the_private_key():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": TRANSACTION})

    outcome = asyncio.run(gateway(handler).fetch_transaction("12-34-56"))
    assert seen == {"auth": "Bearer prv_test_x", "path": "/v1/transactions/12-34-56"}
    assert outcome.transaction_id == "12-34-56"


def test_fetch_unknown_is_none():
    assert asyncio.run(gateway(lambda r: httpx.Response(404)).fetch_transaction("x")) is None


def test_find_by_reference_takes_the_latest():
    older = {**TRANSACTION, "id": "old", "status": "DECLINED", "created_at": "2026-09-25T12:00:00.000Z"}

    def handler(request: httpx.Request):
        assert request.url.params["reference"] == "cinemaplus-abc"
        return httpx.Response(200, json={"data": [older, TRANSACTION]})

    outcome = asyncio.run(gateway(handler).find_by_reference("cinemaplus-abc"))
    assert outcome.transaction_id == "12-34-56"
    assert asyncio.run(gateway(lambda r: httpx.Response(200, json={"data": []})).find_by_reference("x")) is None


def test_void_reports_wompi_refusal_without_raising():
    ok = asyncio.run(gateway(lambda r: httpx.Response(201, json={"data": {}})).void("12-34-56"))
    assert ok.ok
    refused = asyncio.run(
        gateway(lambda r: httpx.Response(422, json={"error": {"type": "INPUT_VALIDATION_ERROR", "reason": "no anulable"}})).void("x")
    )
    assert not refused.ok and "no anulable" in refused.reason
