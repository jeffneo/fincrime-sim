"""Typology injection framework.

Every typology implements one interface — select hosts, build whatever
subgraph it needs, emit transactions, emit labels — and the engine here handles
prevalence, difficulty-tier assignment, per-ring seeding, and the merge back
into the background stream.

Three design points carry most of the weight:

**Illicit transactions are interleaved, not appended.** A typology does not
produce its own transaction log. It hands the engine transactions bucketed by
month, and the behavior model folds them into that month's buffer before
sorting. They get their ids from the same sequence as everything else and go
through the same assembly path, so there is no ordering, id, memo or channel
artifact that distinguishes them (spec §5.3).

**Hosts keep their normal activity.** A typology selects existing population
members, who continue to emit their background streams for the whole window.
A mule still has a salary and a grocery habit. Selecting fresh entities instead
would make "has only suspicious activity" a perfect feature.

**Labels for transactions are emitted after assembly.** Transaction ids do not
exist until the month is assembled, so a typology tags its rows with a label
spec index that the assembler resolves once ids are known. The tag never
reaches a written column.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np

from ..config import RunConfig
from ..institution import Controls
from ..population import Population
from ..rng import StreamRegistry


@dataclass(slots=True)
class LabelSpec:
    """One ground-truth label, before subject ids are known."""

    ring_id: str
    typology: str
    role: str
    polarity: str
    confidence: float
    difficulty_tier: str
    subject_type: str
    #: Resolved for entity/account/card subjects at injection time; left None
    #: for transaction subjects, which are filled in after assembly.
    subject_id: str | None = None


@dataclass(slots=True)
class RingSpec:
    """One injected typology instance."""

    ring_id: str
    typology: str
    difficulty_tier: str
    knobs: dict[str, Any]
    injected_from: date
    injected_to: date
    #: "illicit" | "hard_negative". Defaults to illicit because the typology
    #: generators are the common case; every hard-negative generator sets it
    #: explicitly. A look-alike is stamped with the typology it mimics, so
    #: without this the two are indistinguishable on the Ring node itself.
    polarity: str = "illicit"

    @property
    def knob_digest(self) -> str:
        """Hash of the exact knob values used.

        Recorded per ring so a calibration run can tell which parameter bundle
        produced a ring that turned out too easy or too hard, without having to
        re-derive it from the tier name and the config.
        """
        blob = json.dumps(self.knobs, sort_keys=True, default=str).encode()
        return hashlib.blake2b(blob, digest_size=8).hexdigest()


@dataclass(slots=True)
class PendingTransactions:
    """Typology transactions, bucketed by the month they fall in.

    Keyed by ``(year, month)`` so the behavior model can pick up exactly the
    rows belonging to the month it is currently generating and fold them into
    the same buffer, which is what makes them indistinguishable downstream.
    """

    by_month: dict[tuple[int, int], list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(
        self,
        *,
        from_account: np.ndarray,
        amount: np.ndarray,
        ts: np.ndarray,
        txn_class: str,
        direction: str,
        label_tag: np.ndarray,
        to_account: np.ndarray | None = None,
        merchant: np.ndarray | None = None,
        card: np.ndarray | None = None,
    ) -> None:
        """Queue transactions, split by the month each falls in.

        ``merchant`` and ``card`` are as necessary as the amount for a card
        transaction: the background attaches both to every card row, so an
        injected one without them would be identifiable by their absence alone.
        """
        if len(from_account) == 0:
            return
        months = ts.astype("datetime64[M]")
        for month in np.unique(months):
            mask = months == month
            as_date = month.astype(date)
            self.by_month[(as_date.year, as_date.month)].append(
                {
                    "from_account": from_account[mask],
                    "to_account": None if to_account is None else to_account[mask],
                    "merchant": None if merchant is None else merchant[mask],
                    "card": None if card is None else card[mask],
                    "amount": amount[mask],
                    "ts": ts[mask],
                    "txn_class": txn_class,
                    "direction": direction,
                    "label_tag": label_tag[mask],
                }
            )

    def for_month(self, year: int, month: int) -> list[dict[str, Any]]:
        return self.by_month.get((year, month), [])

    @property
    def total(self) -> int:
        return sum(len(chunk["amount"]) for chunks in self.by_month.values() for chunk in chunks)


@dataclass(slots=True)
class InjectionResult:
    """One or more injected instances: their rings, labels and transactions.

    **Tag numbering contract.** A generator numbers its transaction tags
    relative to its *own* label list, starting at zero. :meth:`extend` rebases
    them as it merges. Every generator producing absolute indices instead would
    be wrong the moment it built a second instance — each instance appends
    labels, so the second one's tags would point at the first one's labels and
    silently attach its transactions to the wrong ring.
    """

    rings: list[RingSpec] = field(default_factory=list)
    labels: list[LabelSpec] = field(default_factory=list)
    pending: PendingTransactions = field(default_factory=PendingTransactions)

    def extend(self, other: InjectionResult) -> None:
        offset = len(self.labels)
        self.rings.extend(other.rings)
        self.labels.extend(other.labels)
        for key, chunks in other.pending.by_month.items():
            for chunk in chunks:
                if offset:
                    tags = chunk["label_tag"]
                    chunk = {**chunk, "label_tag": np.where(tags >= 0, tags + offset, tags)}
                self.pending.by_month[key].append(chunk)


@dataclass(slots=True)
class InjectionContext:
    cfg: RunConfig
    pop: Population
    controls: Controls
    rng: StreamRegistry
    #: Accounts already claimed by another typology, so two rings never fight
    #: over the same host. Mutated by the engine as each typology runs.
    claimed_accounts: set[int]
    #: Index into `labels` of the result being built, so a typology can tag
    #: transactions before the label list is final.
    label_offset: int


class Typology(Protocol):
    """One typology generator."""

    #: Key in config/typologies.yaml.
    name: str

    def __init__(self, ctx: InjectionContext) -> None: ...

    def inject(self, n_rings: int, tier_counts: dict[str, int]) -> InjectionResult:
        """Build ``n_rings`` rings, split across difficulty tiers."""
        ...


def _tier_counts(rng: np.random.Generator, n_rings: int, tiers: dict[str, float]) -> dict[str, int]:
    """Split a ring budget across difficulty tiers, preserving the total."""
    names = list(tiers)
    weights = np.array([tiers[t] for t in names], dtype=float)
    weights /= weights.sum()
    counts = np.floor(weights * n_rings).astype(int)
    # Hand out the remainder by largest fractional part, so a small budget does
    # not silently drop a tier entirely.
    remainder = n_rings - int(counts.sum())
    if remainder > 0:
        fractions = weights * n_rings - counts
        for i in np.argsort(fractions)[::-1][:remainder]:
            counts[i] += 1
    return dict(zip(names, counts.tolist(), strict=True))


def inject(
    cfg: RunConfig, rng: StreamRegistry, pop: Population, controls: Controls
) -> InjectionResult:
    """Run every enabled typology at its configured prevalence.

    Prevalence is expressed as a fraction of entities, not of transactions
    (spec §3), because class imbalance at the entity level is what makes the
    detection problem realistic — a handful of rings inside a population of
    100,000 customers.
    """
    from . import TYPOLOGIES

    tcfg = cfg.typologies
    prevalence = tcfg["prevalence"]
    mix = tcfg["mix"]

    total_illicit = int(
        round(pop.tables["individual"].height * prevalence["illicit_entity_fraction"])
    )
    result = InjectionResult()
    claimed: set[int] = set()

    for cls in TYPOLOGIES:
        name = cls.name
        spec = tcfg["typologies"].get(name, {})
        if not spec.get("enabled", False):
            continue
        share = mix.get(name, {}).get("share", 0.0)
        if share <= 0:
            continue

        budget = int(round(total_illicit * share))
        if budget <= 0:
            continue

        r = rng.fresh("typologies", name, "planning")
        tiers = _tier_counts(r, _ring_budget(name, budget, spec), mix[name]["tiers"])

        ctx = InjectionContext(
            cfg=cfg,
            pop=pop,
            controls=controls,
            rng=rng,
            claimed_accounts=claimed,
            label_offset=len(result.labels),
        )
        result.extend(cls(ctx).inject(sum(tiers.values()), tiers))

    return result


def _ring_budget(name: str, entity_budget: int, spec: dict[str, Any]) -> int:
    """Convert an entity budget into a ring count.

    Prevalence is specified per entity but rings contain several entities, so
    the ring count has to be derived from the typology's own average ring size.
    Doing it the other way round - specifying ring counts directly - would make
    the labeled-entity fraction drift every time a tier's ring size changed,
    and the whole point of the prevalence setting is that it stays fixed.
    """
    sizes: list[float] = []
    for tier in spec.get("tiers", {}).values():
        for key in ("deposit_points", "ring_size", "chain_length", "cards_per_device"):
            if key in tier:
                low, high = tier[key]
                sizes.append((low + high) / 2.0 + 1.0)  # +1 for the collector/hub
                break
    mean_size = float(np.mean(sizes)) if sizes else 5.0
    return max(1, int(round(entity_budget / mean_size)))


def claimed(result: InjectionResult, pop: Population) -> set[int]:
    """Account indices already used by an injection run.

    Recovered from the labels rather than threaded through, so the hard-negative
    engine can be handed the set without the two engines sharing mutable state.
    """
    account_index = {str(a): i for i, a in enumerate(pop.account_ids)}
    return {
        account_index[label.subject_id]
        for label in result.labels
        if label.subject_type == "account" and label.subject_id in account_index
    }
