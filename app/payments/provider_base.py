from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PaymentResult:
    provider_payment_id: str
    payment_url: str | None = None        # None for manual/stars
    display_data: dict = field(default_factory=dict)  # extra info for display


@dataclass
class IncomingPayment:
    our_payment_id: int           # our DB payment ID
    provider_payment_id: str
    amount: float
    currency: str


class PaymentProviderBase(ABC):
    """Abstract base class for payment providers."""

    @abstractmethod
    async def create_payment(
        self,
        our_payment_id: int,
        amount: float,
        currency: str,
        description: str,
        user_id: int,
    ) -> PaymentResult:
        """Create a payment and return a PaymentResult."""

    @abstractmethod
    async def verify_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
    ) -> IncomingPayment | None:
        """Verify an incoming webhook. Returns IncomingPayment or None if invalid."""
