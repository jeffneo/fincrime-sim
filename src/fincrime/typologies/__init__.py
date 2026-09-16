"""Typology generators and the injection engine (spec §5.3)."""

from __future__ import annotations

from .base import InjectionResult, PendingTransactions, Typology, claimed, inject
from .cnp_fraud import CnpFraud
from .mule_network import MuleNetwork
from .shell_layering import ShellLayering
from .structuring import Structuring

#: The Phase 1 typology library (spec §10.1).
TYPOLOGIES: tuple[type[Typology], ...] = (
    Structuring,
    ShellLayering,
    MuleNetwork,
    CnpFraud,
)

__all__ = [
    "TYPOLOGIES",
    "CnpFraud",
    "InjectionResult",
    "MuleNetwork",
    "PendingTransactions",
    "ShellLayering",
    "Structuring",
    "Typology",
    "claimed",
    "inject",
]
