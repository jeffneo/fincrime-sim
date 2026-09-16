"""T3 — money mule network.

Recruited individuals receive funds from unrelated payers and forward them to a
small set of collectors, keeping a cut. The accounts are often recently opened
and the ring frequently operates from shared infrastructure.

**Topology** — fan-in to many mules, fan-out to few collectors, with the mule
layer connected to each other only through that shared infrastructure. The
community structure is what GDS finds, and it is the reason this typology is
the best demonstration of why a graph matters: no mule looks remarkable alone.

**What makes it hard.** The easy tier shares one device across the whole ring,
opens every account days before use, and forwards within hours retaining almost
nothing. The hard tier shares no device at all, uses accounts a year old,
forwards over weeks, and keeps a third of the money — at which point a mule is
a person who received some transfers and sent some on. The device-sharing
signal, which is the headline one, is deliberately absent from the hard tier.

The background has to carry legitimate device sharing for this to be a real
problem, and it does: households share devices, which produces hundreds of
small shared-device clusters that any community-detection demo will surface
alongside the rings.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..reference import RETAIL_ARCHETYPES
from .base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400

#: Recruitment targets people for whom the cut matters.
_MULE_ARCHETYPES = ("student", "hourly_worker", "self_employed")


class MuleNetwork:
    name = "mule_network"

    def __init__(self, ctx: InjectionContext) -> None:
        self.ctx = ctx
        self.spec: dict[str, Any] = ctx.cfg.typologies["typologies"][self.name]

    def _candidates(self) -> np.ndarray:
        pop = self.ctx.pop
        names = [a.name for a in RETAIL_ARCHETYPES]
        preferred = {names.index(n) for n in _MULE_ARCHETYPES if n in names}
        retail = np.flatnonzero((pop.account_type == "checking") & ~pop.account_owner_is_entity)
        return retail[np.array([pop.account_archetype[i] in preferred for i in retail])]

    def inject(self, n_rings: int, tier_counts: dict[str, int]) -> InjectionResult:
        ctx = self.ctx
        result = InjectionResult()
        pool = self._candidates()
        if pool.size < 5:
            return result

        made = 0
        for tier, count in tier_counts.items():
            knobs = self.spec["tiers"][tier]
            for _ in range(count):
                ring_id = f"RING-{self.name}-{made:05d}"
                r = ctx.rng.fresh("typologies", self.name, ring_id)
                built = self._build_ring(r, ring_id, tier, knobs, pool)
                if built is not None:
                    result.extend(built)
                    made += 1
        return result

    def _build_ring(
        self,
        r: np.random.Generator,
        ring_id: str,
        tier: str,
        knobs: dict[str, Any],
        pool: np.ndarray,
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        window = ctx.cfg.window

        ring_size = int(r.integers(knobs["ring_size"][0], knobs["ring_size"][1] + 1))
        n_collectors = int(r.integers(knobs["collectors"][0], knobs["collectors"][1] + 1))
        available = np.array([a for a in pool if a not in ctx.claimed_accounts])
        if available.size < ring_size + n_collectors:
            return None

        picked = r.choice(available, ring_size + n_collectors, replace=False)
        mules, collectors = picked[:ring_size], picked[ring_size:]
        ctx.claimed_accounts.update(int(a) for a in picked)

        # --- shared infrastructure, the headline mule signal ---
        #
        # Applied by repointing the ring members at one device in the
        # population, which is exactly how a household comes to share one. That
        # matters: if the ring's device link were attached by some separate
        # path, it would be distinguishable by its mechanism rather than by
        # what it says, and the hundreds of legitimate shared-device clusters
        # the background already contains would stop being competition.
        sharing_rate = float(r.uniform(*knobs["device_sharing_rate"]))
        owners = np.array([pop.account_owner[a] for a in mules], dtype=np.int64)
        shares = r.random(len(owners)) < sharing_rate
        if shares.any():
            ring_device = int(pop.individual_device[owners[0]])
            pop.individual_device[owners[shares]] = ring_device

        retention = knobs["retention_pct"]
        latency_hours = knobs["forward_latency_hours"]
        ramp_days = int(r.integers(*knobs["recruitment_ramp_days"]))

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        max_span = max(window.days - 2, 1)
        ring_start = int(r.integers(0, max_span))

        in_from: list[int] = []
        in_to: list[int] = []
        in_amount: list[float] = []
        in_ts: list[int] = []
        out_from: list[int] = []
        out_to: list[int] = []
        out_amount: list[float] = []
        out_ts: list[int] = []

        for position, mule in enumerate(mules):
            # Mules come online across the recruitment ramp, not all at once,
            # and each one is then used hard for a few weeks rather than
            # trickling across the whole window. That short active life is how
            # mule accounts actually behave - they get frozen - and spreading a
            # handful of receipts over a year instead made them invisible to
            # any lookback rule as well as unrealistic.
            active_from = ring_start + int(position / max(ring_size, 1) * ramp_days)
            active_days = int(r.integers(10, 61))
            n_receipts = int(r.integers(4, 13))
            for _ in range(n_receipts):
                day = active_from + int(r.integers(0, active_days))
                if day >= max_span:
                    continue
                amount = float(r.lognormal(7.9, 0.7)) + 400.0
                second = int(r.integers(7 * 3600, 23 * 3600))
                # Inbound comes from outside the institution: victims of the
                # fraud that generated the funds are not modelled as customers
                # in Phase 1, so the bank sees a credit with no internal payer.
                in_from.append(int(mule))
                in_to.append(-1)
                in_amount.append(round(amount, 2))
                in_ts.append(int(start_epoch + day * _SECONDS_PER_DAY + second))

                kept = amount * r.uniform(*retention)
                forward_at = (
                    start_epoch
                    + day * _SECONDS_PER_DAY
                    + second
                    + int(r.uniform(*latency_hours) * 3600)
                )
                if forward_at >= start_epoch + max_span * _SECONDS_PER_DAY:
                    continue
                out_from.append(int(mule))
                out_to.append(int(r.choice(collectors)))
                out_amount.append(round(max(amount - kept, 20.0), 2))
                out_ts.append(int(forward_at))

        if not out_from:
            return None

        result = InjectionResult()
        confidence = {"easy": 1.0, "medium": 0.95, "hard": 0.8}[tier]
        labels: list[LabelSpec] = []

        def _label(role: str, subject_type: str, subject_id: str | None) -> LabelSpec:
            return LabelSpec(
                ring_id=ring_id,
                typology=self.name,
                role=role,
                polarity="illicit",
                confidence=confidence,
                difficulty_tier=tier,
                subject_type=subject_type,
                subject_id=subject_id,
            )

        for role, accounts in (("mule", mules), ("collector", collectors)):
            for account in accounts:
                labels.append(_label(role, "account", str(pop.account_ids[account])))
                labels.append(
                    _label(
                        role,
                        "individual",
                        str(
                            pop.tables["individual"]["individual_id"][
                                int(pop.account_owner[account])
                            ]
                        ),
                    )
                )

        labels.append(_label("mule_receipt", "transaction", None))
        labels.append(_label("mule_forward", "transaction", None))
        receipt_tag = len(labels) - 2
        forward_tag = len(labels) - 1

        result.labels = labels
        result.pending.add(
            from_account=np.array(in_from, dtype=np.int64),
            amount=np.array(in_amount),
            ts=np.array(in_ts, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="p2p_transfer",
            direction="credit",
            label_tag=np.full(len(in_from), receipt_tag, dtype=np.int64),
        )
        result.pending.add(
            from_account=np.array(out_from, dtype=np.int64),
            to_account=np.array(out_to, dtype=np.int64),
            amount=np.array(out_amount),
            ts=np.array(out_ts, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="p2p_transfer",
            direction="debit",
            label_tag=np.full(len(out_from), forward_tag, dtype=np.int64),
        )
        result.rings = [
            RingSpec(
                ring_id=ring_id,
                typology=self.name,
                difficulty_tier=tier,
                knobs=dict(knobs),
                injected_from=window.start,
                injected_to=window.end,
            )
        ]
        return result
