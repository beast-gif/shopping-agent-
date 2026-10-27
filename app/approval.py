"""The only way to authorise an order: you type 'confirm <total>' for a specific bill.

The approval is a single-use token bound to one app and one amount, valid for a few
minutes. Nothing an LLM produces can create one; only approve_order() can, and it
needs the exact text you typed.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field

from app.config import AppConfig


class ApprovalError(PermissionError):
    pass


@dataclass
class OrderApproval:
    server: str                 # 'zepto' | 'swiggy_food' | 'zomato'
    amount: float               # rupees, the bill you approved
    issued_at: float = field(default_factory=time.time)
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    used: bool = False

    def check(self, cfg: AppConfig, server: str) -> None:
        if self.used:
            raise ApprovalError("This confirmation was already used; confirm again for a new order")
        if self.server != server:
            raise ApprovalError(f"Confirmation was for {self.server}, not {server}")
        age = time.time() - self.issued_at
        if age > cfg.ordering.approval_valid_seconds:
            raise ApprovalError(f"Confirmation expired ({int(age)} s old); confirm again")
        if self.amount > cfg.ordering.max_order_total:
            raise ApprovalError(f"₹{self.amount:g} is above your cap of ₹{cfg.ordering.max_order_total:g}")


def confirm_phrase(amount: float) -> str:
    """₹279.50 -> 'confirm 280' (rounded up, so you never approve less than the bill)."""
    return f"confirm {math.ceil(amount)}"


def approve_order(cfg: AppConfig, server: str, amount: float, typed: str) -> OrderApproval:
    if not cfg.ordering.enabled:
        raise ApprovalError("Ordering is disabled in config.yaml (ordering.enabled)")
    if amount is None or amount <= 0:
        raise ApprovalError("No bill total to approve")
    if amount > cfg.ordering.max_order_total:
        raise ApprovalError(f"₹{amount:g} is above your cap of ₹{cfg.ordering.max_order_total:g}")
    expected = confirm_phrase(amount)
    if " ".join(typed.strip().lower().split()) != expected:
        raise ApprovalError(f"Not confirmed. To place this order type exactly: {expected}")
    return OrderApproval(server=server, amount=amount)