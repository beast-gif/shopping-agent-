"""After an order is created in 'pending payment' state: show you the UPI QR, then wait
for the payment and close the order out the way each app documents."""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional, Union

from app.adapters.base import FoodAdapter, GroceryAdapter
from app.config import AppConfig, resolve
from app.mcp_client import describe_error
from app.schemas import PaymentRequest, PaymentState

Adapter = Union[GroceryAdapter, FoodAdapter]
TERMINAL: tuple[PaymentState, ...] = ("PAID", "FAILED", "REFUND")


def _open(path_or_url: str) -> None:
    try:
        if sys.platform == "win32" and not path_or_url.startswith("http"):
            os.startfile(path_or_url)  # noqa: S606  opens the PNG in the Photos app
        else:
            webbrowser.open(path_or_url if path_or_url.startswith("http") else Path(path_or_url).as_uri())
    except Exception:  # noqa: BLE001  showing the QR is best-effort; the path is printed anyway
        pass


def save_qr(payment: PaymentRequest) -> Optional[Path]:
    """Writes the QR to .state/pay_<app>.png. Uses the app's own QR image if it sent one,
    otherwise draws one from the upi:// link."""
    path = resolve(f".state/pay_{payment.platform}.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    if payment.qr_png_base64:
        try:
            path.write_bytes(base64.b64decode(payment.qr_png_base64))
            return path
        except (ValueError, TypeError):
            pass
    if payment.upi_link:
        import qrcode  # imported here so the rest of the app works without it

        qrcode.make(payment.upi_link).save(path)
        return path
    return None


def show_payment(payment: PaymentRequest, log: Callable[[str], None] = print) -> bool:
    """Shows how to pay. Returns False (and shows nothing to pay) if the app asks for more
    than you approved."""
    amount = payment.amount if payment.amount is not None else payment.approved_amount
    if payment.amount_mismatch:
        log(f"STOP: {payment.platform} wants ₹{payment.amount:g} but you approved ₹{payment.approved_amount:g}. "
            "Don't pay; the order will be left to lapse.")
        return False

    log(f"Pay ₹{amount:g} on {payment.platform.title()} (order {payment.order_id or '?'}) with any UPI app.")
    shown = False
    qr_path = save_qr(payment)
    if qr_path:
        log(f"  QR code: {qr_path}  (scan it with your phone)")
        _open(str(qr_path))
        shown = True
    if payment.payment_url:
        log(f"  Payment page: {payment.payment_url}")
        _open(payment.payment_url)
        shown = True
    if payment.upi_link:
        log(f"  UPI link: {payment.upi_link}")
        shown = True
    if not shown:
        log("  The app didn't send a QR or link. Open the app's orders screen to pay, "
            "or just don't pay and the order will lapse.")
    return True


async def wait_for_payment(adapter: Adapter, payment: PaymentRequest, cfg: AppConfig,
                           log: Callable[[str], None] = print) -> tuple[PaymentState, bool]:
    """Polls every payment_poll_seconds until paid/failed or payment_timeout_seconds.
    Then calls the app's finalize step (Swiggy: confirm_order, which also closes an
    unpaid order as failed). Returns (last state, timed_out)."""
    poll = max(5, cfg.ordering.payment_poll_seconds)
    deadline = time.monotonic() + cfg.ordering.payment_timeout_seconds
    state: PaymentState = "UNKNOWN"
    errors = 0

    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        try:
            state = await adapter.payment_status(payment)
            errors = 0
        except Exception as e:  # noqa: BLE001  a flaky status call shouldn't abandon the order
            errors += 1
            log(f"  (status check failed: {describe_error(e)})")
            if errors >= 5:
                break
            continue
        remaining = int(deadline - time.monotonic())
        log(f"  payment: {state}  ({max(0, remaining)} s left)")
        if state in TERMINAL:
            try:
                await adapter.finalize(payment, state)
            except Exception as e:  # noqa: BLE001
                log(f"  (finalizing the order failed: {describe_error(e)}; check the app)")
            return state, False

    try:
        await adapter.finalize(payment, state)
    except Exception as e:  # noqa: BLE001
        log(f"  (closing the order failed: {describe_error(e)})")
    return state, True