"""
PaymentGateway — abstract + two mock implementations.

In real life this would talk to Stripe / Razorpay / Adyen. For the project
we use mocks so tests are deterministic and the demo never hits the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import random
from typing import Optional

from billing_engine.models import Invoice


@dataclass(frozen=True)
class PaymentResult:
    success: bool
    failure_reason: Optional[str] = None


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, invoice: Invoice) -> PaymentResult:
        raise NotImplementedError


# ----------------------------------------------------------------
# Scripted — for deterministic tests
# ----------------------------------------------------------------
class ScriptedGateway(PaymentGateway):
    """Returns pre-set results from a queue. Used in tests.

    Example:
        gateway = ScriptedGateway([
            PaymentResult(False, "INSUFFICIENT_FUNDS"),
            PaymentResult(False, "INSUFFICIENT_FUNDS"),
            PaymentResult(True),
        ])
    """

    def __init__(self, results: list[PaymentResult]) -> None:
        self._results = list(results)
        self._index = 0

    def charge(self, invoice: Invoice) -> PaymentResult:
        if self._index >= len(self._results):
            return PaymentResult(False, "NO_MORE_RESULTS")
        result = self._results[self._index]
        self._index += 1
        return result


# ----------------------------------------------------------------
# Fake-random — for the CLI demo
# ----------------------------------------------------------------
class FakeRandomGateway(PaymentGateway):
    """Succeeds at a configurable rate; seeded for reproducibility."""

    def __init__(self, success_rate: float = 0.7, seed: Optional[int] = None) -> None:
        if success_rate < 0.0 or success_rate > 1.0:
            raise ValueError("success_rate must be between 0 and 1")
        self.success_rate = success_rate
        self._rng = random.Random(seed)

    def charge(self, invoice: Invoice) -> PaymentResult:
        if self._rng.random() < self.success_rate:
            return PaymentResult(success=True)
        return PaymentResult(success=False, failure_reason="PROCESSOR_DECLINED")
