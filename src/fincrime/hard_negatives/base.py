"""Hard-negative injection engine.

Shares the typology framework (``typologies.base``) rather than duplicating it:
a hard negative is structurally the same kind of thing — select hosts, build a
subgraph, emit transactions and labels — and the only differences that matter
are the label polarity and the fact that nothing illegal happened.

Labels carry ``typology`` = the typology being mimicked and ``role`` = the
generator that produced them. That pairing is what lets a scorer ask, of one
typology, both "was the crime found" and "was the look-alike avoided" — which
is the question the whole hard-negative design exists to make askable.
"""

from __future__ import annotations

import numpy as np

from ..config import RunConfig
from ..institution import Controls
from ..population import Population
from ..rng import StreamRegistry
from ..typologies.base import InjectionContext, InjectionResult
from .cash_business import CashIntensiveBusiness
from .holding_structure import HoldingStructure
from .travel_card import FrequentTraveler
from .treasury_hub import TreasuryHub

#: Registry, keyed by the config block in ``config/typologies.yaml``.
HARD_NEGATIVES = (
    CashIntensiveBusiness,
    TreasuryHub,
    FrequentTraveler,
    HoldingStructure,
)


def inject(
    cfg: RunConfig,
    rng: StreamRegistry,
    pop: Population,
    controls: Controls,
    claimed_accounts: set[int],
) -> InjectionResult:
    """Run every hard-negative generator at its configured share.

    ``claimed_accounts`` is the set already taken by typology injection, passed
    through so a hard negative never lands on an account that is actually
    committing a crime. A subject that was both would be unscoreable: there
    would be no correct answer for a detector that flagged it.
    """
    spec = cfg.typologies.get("hard_negatives", {})
    prevalence = cfg.typologies["prevalence"]
    budget = int(
        round(pop.tables["individual"].height * prevalence["hard_negative_entity_fraction"])
    )
    result = InjectionResult()
    if budget <= 0:
        return result

    for cls in HARD_NEGATIVES:
        block = spec.get(cls.name)
        if not block:
            continue
        share = float(block.get("share", 0.0))
        if share <= 0:
            continue

        ctx = InjectionContext(
            cfg=cfg,
            pop=pop,
            controls=controls,
            rng=rng,
            claimed_accounts=claimed_accounts,
            label_offset=len(result.labels),
        )
        generator = cls(ctx, mimics=block["mimics"])
        count = max(1, int(round(budget * share / generator.entities_per_instance)))
        result.extend(generator.inject(count))

    return result


def window_seconds(cfg: RunConfig) -> tuple[int, int]:
    """Start and end of the simulation window, as epoch seconds."""
    start = np.datetime64(cfg.window.start, "s").astype("int64")
    return start, start + cfg.window.days * 86_400
