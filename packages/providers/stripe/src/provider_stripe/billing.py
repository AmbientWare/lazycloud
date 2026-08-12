from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from shared.errors import (
    InvalidInputError,
    PaymentDeclinedError,
    UpstreamUnavailableError,
)
from shared.payments import (
    HostedPaymentSession,
    InvoiceLine,
    PaymentCustomer,
    ProviderInvoice,
)

LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://api.stripe.com/v1"

_PERIOD_KEY = "lazycloud_period"


@dataclass(frozen=True, slots=True)
class StripeBilling:
    """Stripe customers and invoices for one account.

    Stripe is the payment rail here, not the pricing engine: this platform
    computes what is owed and hands over finished amounts. Holding rates on both
    sides would decide the money twice from two catalogs that drift.
    """

    client: httpx.Client

    def create_customer(self, *, email: str, workspace_id: str) -> PaymentCustomer:
        result = self._request(
            "POST",
            "/customers",
            data={
                "email": email,
                # Stripe is the system of record for who pays; this repository is
                # the system of record for what they used. The workspace is
                # stamped here so a payment reaching us out of band—a webhook, a
                # dispute—can be traced back without a second lookup table.
                "metadata[workspace_id]": workspace_id,
            },
        )
        return PaymentCustomer(provider_customer_id=str(result.get("id") or ""))

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        result = self._request(
            "POST",
            "/checkout/sessions",
            data={
                "mode": "setup",
                "customer": provider_customer_id,
                # Required even in setup mode, where nothing is charged: Stripe
                # refuses the session without it.
                "currency": currency.lower(),
                # Pinned. Left open, Stripe offers whatever wallets the account
                # has enabled, and several of them cannot be charged off-session
                # later — which is the entire purpose of saving one here.
                "payment_method_types[0]": "card",
                "success_url": success_url,
                "cancel_url": cancel_url,
            },
        )
        return HostedPaymentSession(url=str(result.get("url") or ""))

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        result = self._request(
            "POST",
            "/billing_portal/sessions",
            data={"customer": provider_customer_id, "return_url": return_url},
        )
        return HostedPaymentSession(url=str(result.get("url") or ""))

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        result = self._request("GET", f"/payment_methods/{provider_payment_method_id}")
        return str(result.get("customer") or "")

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        self._request(
            "POST",
            f"/customers/{provider_customer_id}",
            data={"invoice_settings[default_payment_method]": provider_payment_method_id},
        )

    def draft_invoice(self, *, provider_customer_id: str, period_key: str) -> ProviderInvoice:
        # Every state, not just drafts. A run that finalized and then died before
        # recording it would otherwise find nothing here and issue a second
        # invoice for the same month — and a finalized one cannot be deleted.
        existing = self._request(
            "GET",
            "/invoices",
            params={"customer": provider_customer_id, "limit": 100},
        )
        held: list[dict[str, Any]] = existing.get("data") or []
        if existing.get("has_more"):
            raise UpstreamUnavailableError(
                f"customer {provider_customer_id} has more invoices than one page; "
                "refusing to decide whether this period already has one"
            )
        for invoice in held:
            metadata: dict[str, Any] = invoice.get("metadata") or {}
            if metadata.get(_PERIOD_KEY) == period_key and invoice.get("status") != "void":
                return _invoice(invoice)
        return _invoice(
            self._request(
                "POST",
                "/invoices",
                data={
                    "customer": provider_customer_id,
                    "collection_method": "charge_automatically",
                    # Never advance on its own: a draft this platform has not
                    # finished writing must not become a charge on a timer.
                    "auto_advance": "false",
                    f"metadata[{_PERIOD_KEY}]": period_key,
                },
                # The read above cannot close the window on its own: two callers
                # can both find nothing and both create. Stripe collapses the
                # second create into the first when they carry the same key, so
                # one period is one invoice however the two interleave.
                idempotency_key=f"invoice:{period_key}",
            )
        )

    def replace_invoice_lines(
        self,
        *,
        provider_invoice_id: str,
        provider_customer_id: str,
        currency: str,
        lines: tuple[InvoiceLine, ...],
    ) -> None:
        held = self._request(
            "GET", "/invoiceitems", params={"invoice": provider_invoice_id, "limit": 100}
        )
        items: list[dict[str, Any]] = held.get("data") or []
        if held.get("has_more"):
            # Stale items past the page would survive and ride on the invoice,
            # and the platform's own total check cannot see them.
            raise UpstreamUnavailableError(
                f"invoice {provider_invoice_id} holds more items than one page"
            )
        for item in items:
            self._request("DELETE", f"/invoiceitems/{item['id']}")
        for line in lines:
            self._request(
                "POST",
                "/invoiceitems",
                data={
                    "customer": provider_customer_id,
                    "invoice": provider_invoice_id,
                    "currency": currency.lower(),
                    "amount": str(line.amount_cents),
                    "description": line.description,
                },
            )

    def finalize_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        return _invoice(self._request("POST", f"/invoices/{provider_invoice_id}/finalize"))

    def pay_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        try:
            return _invoice(self._request("POST", f"/invoices/{provider_invoice_id}/pay"))
        except (PaymentDeclinedError, InvalidInputError):
            # Neither refusal body carries an invoice — a declined card is a 402
            # and a customer with no card on file is a plain 400 — and Stripe has
            # already recorded the attempt against the invoice either way. So the
            # invoice itself decides: read it back, and if it says nothing was
            # attempted then this was a real fault and it is raised.
            attempted = self.fetch_invoice(provider_invoice_id=provider_invoice_id)
            if not attempted.attempted:
                raise
            return attempted

    def fetch_invoice(self, *, provider_invoice_id: str) -> ProviderInvoice:
        return _invoice(self._request("GET", f"/invoices/{provider_invoice_id}"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else None
        try:
            response = self.client.request(method, path, data=data, params=params, headers=headers)
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"Stripe request failed: {exc!s}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(
                f"Stripe returned a non-JSON response ({response.status_code})"
            ) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            self._raise_api_error(body, status_code=response.status_code)
        return body

    @staticmethod
    def _raise_api_error(body: dict[str, Any], *, status_code: int) -> None:
        error: dict[str, Any] = body.get("error") or {}
        message = str(error.get("message") or f"Stripe rejected the request ({status_code})")
        # A rejected request is ours to fix; anything else is worth retrying.
        # Two 4xx codes are not the caller's fault and must not be terminal: a
        # rate limit is transient, and a rejected credential is an operator
        # problem a rotation fixes. Treating either as terminal would abandon an
        # invoice a customer still owes.
        if status_code in {
            httpx.codes.TOO_MANY_REQUESTS,
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        }:
            raise UpstreamUnavailableError(message)
        # The bank refusing the card. Its own thing rather than folded into the
        # malformed-request branch below, because the caller records it instead of
        # retrying a request that was correct the first time. Narrow on purpose:
        # `code` alone is shared with errors from calls that move no money, and
        # classifying those as declines would turn a real fault into a silent
        # nothing-happened.
        if status_code == httpx.codes.PAYMENT_REQUIRED and error.get("type") == "card_error":
            raise PaymentDeclinedError(message)
        if httpx.codes.BAD_REQUEST <= status_code < httpx.codes.INTERNAL_SERVER_ERROR:
            raise InvalidInputError(message)
        raise UpstreamUnavailableError(message)


def _invoice(item: dict[str, Any]) -> ProviderInvoice:
    status = str(item.get("status") or "")
    return ProviderInvoice(
        provider_invoice_id=str(item.get("id") or ""),
        total_cents=int(item.get("total") or 0),
        status=status,
        # From the status, not from a `paid` flag: the Invoice object carries no
        # such field in the API version this account is on, so reading one would
        # be reading an absent key — every payment would land as unpaid and every
        # account would stay in arrears having paid. A part-payment keeps the
        # invoice `open` with a remaining balance, which is not settled.
        paid=status == "paid",
        attempted=bool(item.get("attempted")),
    )


def build_client(*, api_key: str, timeout_seconds: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=timeout_seconds,
    )


__all__ = ["API_BASE_URL", "StripeBilling", "build_client"]
