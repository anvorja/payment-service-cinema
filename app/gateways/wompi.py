# app/gateways/wompi.py — Wompi (https://docs.wompi.co): Web Checkout, consulta,
# anulación y eventos firmados.
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.domain.payments import Outcome

logger = logging.getLogger(__name__)

_STATUSES = {
    "APPROVED": "approved",
    "DECLINED": "declined",
    "VOIDED": "voided",
    "ERROR": "error",
    "PENDING": "pending",
}


class GatewayUnavailable(Exception):
    """Wompi no respondió o respondió algo inesperado."""


@dataclass(frozen=True)
class WompiSettings:
    public_key: str
    private_key: str
    integrity_secret: str
    events_secret: str
    api_url: str        # https://sandbox.wompi.co/v1 o https://production.wompi.co/v1
    checkout_url: str   # https://checkout.wompi.co/p/
    redirect_url: str
    timeout_seconds: float


@dataclass(frozen=True)
class VoidResult:
    ok: bool
    reason: str | None = None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def wompi_time(moment: datetime) -> str:
    """ISO 8601 en UTC con milisegundos y Z, el formato que Wompi firma."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_outcome(t: dict[str, Any]) -> Outcome:
    extra = (t.get("payment_method") or {}).get("extra") or {}
    return Outcome(
        reference=t["reference"],
        transaction_id=t["id"],
        status=_STATUSES.get(t.get("status", ""), "error"),
        amount_in_cents=int(t["amount_in_cents"]),
        currency=t["currency"],
        payment_method_type=t.get("payment_method_type"),
        last_four=extra.get("last_four"),
    )


class WompiGateway:
    def __init__(self, settings: WompiSettings, client: httpx.AsyncClient | None = None):
        self._s = settings
        self._client = client

    def checkout_url(
        self,
        reference: str,
        amount_in_cents: int,
        currency: str,
        expires_at: datetime,
        customer_email: str,
    ) -> str:
        """
        URL del Web Checkout. Con fecha de expiración la firma de integridad es
        SHA256(referencia + monto + moneda + expiración + secreto); pasada esa
        hora Wompi no cobra el enlace.
        """
        expiration = wompi_time(expires_at)
        signature = _sha256(
            f"{reference}{amount_in_cents}{currency}{expiration}{self._s.integrity_secret}"
        )
        params = {
            "public-key": self._s.public_key,
            "currency": currency,
            "amount-in-cents": str(amount_in_cents),
            "reference": reference,
            "signature:integrity": signature,
            "expiration-time": expiration,
            "redirect-url": self._s.redirect_url,
            "customer-data:email": customer_email,
        }
        return f"{self._s.checkout_url}?{urlencode(params, quote_via=quote)}"

    async def _request(self, method: str, path: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._s.private_key}"}
        url = f"{self._s.api_url}{path}"
        try:
            if self._client is not None:
                return await self._client.request(method, url, headers=headers, timeout=self._s.timeout_seconds)
            async with httpx.AsyncClient(timeout=self._s.timeout_seconds) as client:
                return await client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(f"Wompi no responde: {exc}") from exc

    async def fetch_transaction(self, transaction_id: str) -> Outcome | None:
        """Consulta una transacción (exige la llave privada). None si Wompi no la conoce."""
        res = await self._request("GET", f"/transactions/{quote(transaction_id, safe='')}")
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise GatewayUnavailable(f"Wompi respondió {res.status_code}")
        return _to_outcome(res.json()["data"])

    async def find_by_reference(self, reference: str) -> Outcome | None:
        """
        Busca la transacción de una referencia (llave privada). Sirve cuando no
        llegó el evento ni la persona volvió de Wompi. Si hay varias, gana la
        más reciente; None si no hay ninguna.
        """
        res = await self._request("GET", f"/transactions?reference={quote(reference, safe='')}")
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise GatewayUnavailable(f"Wompi respondió {res.status_code}")
        data = res.json().get("data") or []
        if isinstance(data, dict):
            data = [data]
        if not data:
            return None
        latest = max(data, key=lambda t: t.get("created_at") or "")
        return _to_outcome(latest)

    async def void(self, transaction_id: str) -> VoidResult:
        """
        Anula una transacción aprobada con tarjeta (Wompi no anula otros medios).
        Un rechazo de Wompi no es una excepción: quien llama decide qué hacer.
        """
        res = await self._request("POST", f"/transactions/{quote(transaction_id, safe='')}/void")
        if res.is_success:
            return VoidResult(ok=True)
        try:
            error = res.json().get("error", {})
            reason = error.get("reason") or error.get("messages") or error.get("type")
        except ValueError:
            reason = None
        return VoidResult(ok=False, reason=f"Wompi respondió {res.status_code}: {reason or 'sin detalle'}")

    def verify_event(self, event: Any) -> tuple[bool, Outcome | None]:
        """
        Verifica un evento: SHA256 de los valores listados en
        signature.properties (en orden), luego el timestamp y el secreto de
        eventos. Devuelve el resultado solo para transaction.updated.
        """
        if not isinstance(event, dict):
            return False, None
        signature = event.get("signature") or {}
        properties = signature.get("properties")
        checksum = signature.get("checksum")
        timestamp = event.get("timestamp")
        data = event.get("data") or {}
        if not isinstance(properties, list) or not isinstance(checksum, str) or not isinstance(timestamp, (int, str)):
            return False, None

        values = []
        for path in properties:
            node: Any = data
            for key in str(path).split("."):
                node = node.get(key) if isinstance(node, dict) else None
            values.append("" if node is None else str(node))
        expected = _sha256(f"{''.join(values)}{timestamp}{self._s.events_secret}")
        if not hmac.compare_digest(expected, checksum.lower()):
            return False, None

        transaction = data.get("transaction")
        if event.get("event") == "transaction.updated" and isinstance(transaction, dict):
            return True, _to_outcome(transaction)
        return True, None
