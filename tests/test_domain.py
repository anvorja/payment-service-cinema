from datetime import datetime, timedelta, timezone

from app.domain.payments import Outcome, is_abandoned, new_reference, settled_status, to_cents, view_status

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def outcome(status="approved", amount=3_760_000, currency="COP"):
    return Outcome(reference="cinemaplus-x", transaction_id="t-1", status=status, amount_in_cents=amount, currency=currency)


def test_reference_uses_prefix_and_uuid():
    ref = new_reference("cinemaplus")
    prefix, _, rest = ref.partition("-")
    assert prefix == "cinemaplus"
    assert len(rest) == 36


def test_to_cents_rounds():
    assert to_cents(37_600) == 3_760_000


def test_pending_payment_takes_the_outcome():
    assert settled_status("pending", 3_760_000, "COP", outcome("approved")) == "approved"
    assert settled_status("pending", 3_760_000, "COP", outcome("declined")) == "declined"


def test_final_payment_never_changes():
    assert settled_status("approved", 3_760_000, "COP", outcome("declined")) is None
    assert settled_status("declined", 3_760_000, "COP", outcome("approved")) is None


def test_still_pending_is_no_change():
    assert settled_status("pending", 3_760_000, "COP", outcome("pending")) is None


def test_amount_or_currency_mismatch_is_error():
    assert settled_status("pending", 3_760_000, "COP", outcome(amount=100)) == "error"
    assert settled_status("pending", 3_760_000, "COP", outcome(currency="USD")) == "error"


def test_expired_only_when_no_transaction_started():
    past = NOW - timedelta(minutes=1)
    assert is_abandoned("pending", None, past, NOW)
    assert view_status("pending", None, past, NOW) == "expired"
    # Una transacción que empezó a tiempo (PSE lento) sigue pendiente.
    assert view_status("pending", "t-1", past, NOW) == "pending"
    assert view_status("pending", None, NOW + timedelta(minutes=1), NOW) == "pending"
    assert view_status("approved", None, past, NOW) == "approved"
