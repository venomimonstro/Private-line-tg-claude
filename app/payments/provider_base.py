from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentProviderBase(ABC):
    """Abstract base class for payment providers.

    Implement this interface to add a new payment provider
    (e.g., YooMoney, Tinkoff, Stripe) without modifying bot logic.
    """

    @abstractmethod
    async def create_payment(
        self,
        amount: float,
        currency: str,
        metadata: dict,
    ) -> str:
        """Create a payment and return the provider's payment ID."""

    @abstractmethod
    async def verify_payment(self, provider_payment_id: str) -> bool:
        """Return True if the payment has been successfully completed."""
