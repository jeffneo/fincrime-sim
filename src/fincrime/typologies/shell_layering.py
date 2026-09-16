"""T2 — layering through shell companies.

Placed funds are moved through a chain of corporate entities to break the audit
trail between origin and destination. Each hop looks like an ordinary
commercial payment; the chain as a whole does not.

**Topology** — a directed path, with fan-in at the source and fan-out at the
sink. That is the shape FATF and Egmont describe and what
``neo4j/typology_checks.cypher`` looks for: money entering one end and leaving
the other within days, through entities that trade with nobody else.

**What makes it hard.** The easy tier is a chain of empty shells sharing
directors and a registered address, passing round numbers along within hours
and retaining nothing. The hard tier is short, slow, retains a real fraction at
each hop, uses entities with their own genuine trading activity, and shares
almost nothing between them. At that point the only thing left is that the
money entering the first entity and leaving the last are the same money — and
a legitimate holding group (the M4 hard negative) moves funds between its own
entities constantly for entirely ordinary reasons.

**Single-institution constraint.** Phase 1 simulates one bank, so a hop that
would leave it terminates at an external counterparty rather than being
dropped: the transaction exists with no `TO` edge, exactly as the bank would
see it. Phase 2's second institution replaces the placeholder.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..reference import JURISDICTIONS
from .base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400

#: Destinations for the final hop out. Weighted to secrecy jurisdictions, which
#: is the point of the exercise - but not exclusively, or "paid a high-risk
#: jurisdiction" would be a free feature.
_SINK_JURISDICTIONS = tuple(j.code for j in JURISDICTIONS if j.is_secrecy_haven)


class ShellLayering:
    name = "shell_layering"

    def __init__(self, ctx: InjectionContext) -> None:
        self.ctx = ctx
        self.spec: dict[str, Any] = ctx.cfg.typologies["typologies"][self.name]

    def _candidates(self) -> np.ndarray:
        """Business accounts usable as chain links.

        Low-employee, low-revenue companies preferentially: a shell is a
        company with a bank account and little else. Not exclusively, though -
        the hard tier deliberately uses entities with real trading activity,
        and restricting the pool to obvious shells would make "small company"
        the whole signal.
        """
        pop = self.ctx.pop
        business = np.flatnonzero((pop.account_type == "checking") & pop.account_owner_is_entity)
        return business

    def inject(self, n_rings: int, tier_counts: dict[str, int]) -> InjectionResult:
        ctx = self.ctx
        result = InjectionResult()
        pool = self._candidates()
        if pool.size < 3:
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

        chain_length = int(r.integers(knobs["chain_length"][0], knobs["chain_length"][1] + 1))
        available = np.array([a for a in pool if a not in ctx.claimed_accounts])
        if available.size < chain_length:
            return None

        # Prefer small companies, without restricting to them: weighting rather
        # than filtering keeps "is a tiny company" from being the whole signal.
        revenue = pop.entity_revenue[pop.account_owner[available]]
        weight = 1.0 / (1.0 + np.log1p(np.maximum(revenue, 1.0)))
        weight = weight / weight.sum()
        chain = r.choice(available, chain_length, replace=False, p=weight)
        ctx.claimed_accounts.update(int(a) for a in chain)

        pass_hours = knobs["pass_through_hours"]

        retention = knobs["retention_pct"]
        round_bias = float(knobs["round_amount_bias"])

        # The sum entering the chain. Large enough to be worth layering.
        amount = float(r.lognormal(11.6, 0.8)) + 25_000.0

        from_accounts: list[int] = []
        to_accounts: list[int] = []
        amounts: list[float] = []
        stamps: list[int] = []

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        max_span = max(window.days - 2, 1)

        # Leave room for the whole chain. Picking a start day uniformly meant a
        # chain begun late simply ran out of window and was abandoned - which
        # hit the slow tiers hardest, because their hop latency is longest, and
        # those are precisely the hard rings the dataset needs most. At the mvp
        # preset it produced six easy chains, two medium and no hard ones at
        # all.
        expected_days = chain_length * float(np.mean(pass_hours)) / 24.0
        latest_start = max(int(max_span - expected_days), 0)
        cursor = int(r.integers(0, latest_start + 1)) * _SECONDS_PER_DAY + int(
            r.integers(9 * 3600, 16 * 3600)
        )

        carried = amount
        for hop in range(chain_length):
            kept = carried * r.uniform(*retention)
            moving = max(carried - kept, 500.0)
            if r.random() < round_bias:
                # Round numbers are a layering tell, and only the easy tier
                # leans on them.
                moving = float(round(moving, -3))
            moving = max(moving, 500.0)

            # Last hop leaves the institution: no TO account, the way the bank
            # actually sees a payment to a correspondent.
            target = int(chain[hop + 1]) if hop + 1 < chain_length else -1
            from_accounts.append(int(chain[hop]))
            to_accounts.append(target)
            amounts.append(round(moving, 2))
            stamps.append(int(start_epoch + cursor))

            cursor += int(r.uniform(*pass_hours) * 3600)
            if cursor >= max_span * _SECONDS_PER_DAY:
                break
            carried = moving

        if len(amounts) < 2:
            return None

        # Decoy trading activity, so the shells are not empty vessels whose
        # only transactions are the chain itself.
        decoy_lo, decoy_hi = knobs["decoy_activity_per_shell"]
        decoy_from: list[int] = []
        decoy_amount: list[float] = []
        decoy_ts: list[int] = []
        for link in chain:
            for _ in range(int(r.integers(decoy_lo, decoy_hi + 1))):
                decoy_from.append(int(link))
                decoy_amount.append(round(float(r.lognormal(7.4, 1.1)) + 50.0, 2))
                decoy_ts.append(
                    int(
                        start_epoch
                        + int(r.integers(0, max_span)) * _SECONDS_PER_DAY
                        + int(r.integers(8 * 3600, 18 * 3600))
                    )
                )

        result = InjectionResult()
        confidence = {"easy": 1.0, "medium": 0.95, "hard": 0.85}[tier]
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

        for position, link in enumerate(chain):
            role = (
                "shell_source"
                if position == 0
                else "shell_sink"
                if position == len(chain) - 1
                else f"shell_tier{position}"
            )
            labels.append(_label(role, "account", str(pop.account_ids[link])))
            labels.append(
                _label(
                    role,
                    "legal_entity",
                    str(pop.tables["legal_entity"]["entity_id"][int(pop.account_owner[link])]),
                )
            )

        labels.append(_label("layering_hop", "transaction", None))
        hop_tag = len(labels) - 1

        result.labels = labels
        result.pending.add(
            from_account=np.array(from_accounts, dtype=np.int64),
            to_account=np.array(to_accounts, dtype=np.int64),
            amount=np.array(amounts),
            ts=np.array(stamps, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            # Domestic hops move as wires between corporates, which is what the
            # background uses for the same purpose.
            txn_class="wire_out",
            direction="debit",
            label_tag=np.full(len(amounts), hop_tag, dtype=np.int64),
        )
        if decoy_from:
            # Decoys are NOT labelled illicit: they are ordinary business
            # payments the shells make to look alive. Labelling them would
            # teach a detector that everything a shell touches is criminal.
            result.pending.add(
                from_account=np.array(decoy_from, dtype=np.int64),
                amount=np.array(decoy_amount),
                ts=np.array(decoy_ts, dtype="int64")
                .astype("datetime64[s]")
                .astype("datetime64[us]"),
                txn_class="supplier_payment",
                direction="debit",
                label_tag=np.full(len(decoy_from), -1, dtype=np.int64),
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
