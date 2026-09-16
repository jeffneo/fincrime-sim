"""Typology generators and the injection engine (spec §5.3)."""

from __future__ import annotations

from .base import InjectionResult, PendingTransactions, Typology, claimed, inject
from .structuring import Structuring

#: Registry. M3 adds shell_layering, mule_network and cnp_fraud here.
TYPOLOGIES: tuple[type[Typology], ...] = (Structuring,)

__all__ = [
    "TYPOLOGIES",
    "InjectionResult",
    "PendingTransactions",
    "Structuring",
    "Typology",
    "claimed",
    "inject",
]
