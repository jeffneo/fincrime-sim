"""T1 — structuring / smurfing.

The typology: cash that cannot be deposited in one piece without triggering a
currency transaction report is broken into pieces that each stay under the
threshold, deposited by several people across several points, and consolidated
into one account.

**Topology** — a star. N smurf accounts feed one collector, which moves the
consolidated total out. This is the shape FATF and FinCEN describe, and it is
what ``neo4j/typology_checks.cypher`` recovers.

**What makes it hard.** The obvious signal — deposits clustered just under
USD 10,000 — is only present in the easy tier. The hard tier deposits well
under the threshold, disperses over months, varies amounts widely, and blends
a majority of genuine activity into the same accounts. At that point the only
thing separating it from a legitimate cash business is the *structure*: money
converging on an account whose owner has no commercial reason to receive it.
That is deliberate, and it is why the graph matters more than the amounts.

**What it must not do.** The generator reads the CTR threshold and the
same-day aggregation rule from the institution config, so it evades the control
the institution actually enforces. Depositing 9,900 twice in one day would be
aggregated to 19,800 and reported — a generator that ignored aggregation would
produce a pattern the bank catches for free, making the typology look harder to
evade than it is.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..reference import RETAIL_ARCHETYPES
from .base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400

#: Archetypes recruited as smurfs, in preference order. Recruitment targets
#: people for whom the fee matters; a high earner making repeated sub-threshold
#: cash deposits would be incongruous in a way that is too easy to spot.
_SMURF_ARCHETYPES = ("student", "hourly_worker", "retired", "self_employed")


class Structuring:
    name = "structuring"

    def __init__(self, ctx: InjectionContext) -> None:
        self.ctx = ctx
        self.spec: dict[str, Any] = ctx.cfg.typologies["typologies"][self.name]

    # -- host selection ----------------------------------------------------

    def _candidate_smurfs(self) -> np.ndarray:
        """Retail checking accounts whose owner fits the recruitment profile."""
        pop = self.ctx.pop
        arch_names = [a.name for a in RETAIL_ARCHETYPES]
        preferred = {arch_names.index(n) for n in _SMURF_ARCHETYPES if n in arch_names}
        retail = np.flatnonzero((pop.account_type == "checking") & ~pop.account_owner_is_entity)
        fits = np.array([pop.account_archetype[i] in preferred for i in retail])
        return retail[fits]

    def _candidate_collectors(self) -> np.ndarray:
        """Accounts plausible as a consolidation point.

        Business accounts are preferred: a company receiving many inbound
        transfers is unremarkable on its face, which is exactly why real
        schemes use one. A retail collector would stand out on fan-in alone and
        make the typology too easy.
        """
        pop = self.ctx.pop
        business = np.flatnonzero((pop.account_type == "checking") & pop.account_owner_is_entity)
        return business

    # -- generation --------------------------------------------------------

    def inject(self, n_rings: int, tier_counts: dict[str, int]) -> InjectionResult:
        ctx = self.ctx
        result = InjectionResult()
        smurf_pool = self._candidate_smurfs()
        collector_pool = self._candidate_collectors()
        if smurf_pool.size == 0 or collector_pool.size == 0:
            return result

        ring_index = 0
        for tier, count in tier_counts.items():
            knobs = self.spec["tiers"][tier]
            for _ in range(count):
                ring_id = f"RING-{self.name}-{ring_index:05d}"
                # Per-ring stream keyed by ring id alone, so adding or removing
                # a ring never shifts any other ring's draws (rng.py).
                r = ctx.rng.fresh("typologies", self.name, ring_id)
                built = self._build_ring(r, ring_id, tier, knobs, smurf_pool, collector_pool)
                if built is not None:
                    result.extend(built)
                    ring_index += 1
        return result

    def _build_ring(
        self,
        r: np.random.Generator,
        ring_id: str,
        tier: str,
        knobs: dict[str, Any],
        smurf_pool: np.ndarray,
        collector_pool: np.ndarray,
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        controls = ctx.controls
        window = ctx.cfg.window

        n_points = int(r.integers(knobs["deposit_points"][0], knobs["deposit_points"][1] + 1))
        available = np.array([a for a in smurf_pool if a not in ctx.claimed_accounts])
        if available.size < n_points:
            return None

        smurfs = r.choice(available, n_points, replace=False)
        collector_options = np.array([a for a in collector_pool if a not in ctx.claimed_accounts])
        if collector_options.size == 0:
            return None
        collector = int(r.choice(collector_options))

        ctx.claimed_accounts.update(int(a) for a in smurfs)
        ctx.claimed_accounts.add(collector)

        # --- timing ---
        span_days = int(
            r.integers(knobs["aggregation_window_days"][0], knobs["aggregation_window_days"][1] + 1)
        )
        span_days = min(span_days, max(window.days - 2, 1))
        start_day = int(r.integers(0, max(window.days - span_days, 1)))

        # --- amounts ---
        headroom = r.uniform(*knobs["threshold_headroom_pct"]) / 100.0
        base_amount = controls.ctr_threshold_usd * (1.0 - headroom)
        jitter_cv = float(knobs["amount_jitter_cv"])

        # Deposits per smurf. Enough to be a pattern, few enough that no smurf
        # trips the aggregate rule on their own.
        deposits_per_smurf = int(r.integers(2, 6))

        rows_smurf: list[int] = []
        rows_amount: list[float] = []
        rows_ts: list[np.datetime64] = []

        for smurf in smurfs:
            for _ in range(deposits_per_smurf):
                amount = base_amount * max(0.15, r.normal(1.0, jitter_cv))
                # Hard ceiling at the threshold. A deposit at or above it would
                # be reported outright and the scheme would not place it.
                amount = min(amount, controls.ctr_threshold_usd - 25.0)
                day = start_day + int(r.integers(0, span_days))
                # Banking hours - cash goes over a counter or into an ATM.
                second = int(r.integers(9 * 3600, 17 * 3600))
                ts = (
                    np.datetime64(window.start, "s").astype("int64")
                    + day * _SECONDS_PER_DAY
                    + second
                )
                rows_smurf.append(int(smurf))
                rows_amount.append(round(float(amount), 2))
                rows_ts.append(ts)

        deposit_ts = (
            np.array(rows_ts, dtype="int64").astype("datetime64[s]").astype("datetime64[us]")
        )
        deposit_amount = np.array(rows_amount)
        deposit_account = np.array(rows_smurf, dtype=np.int64)

        # Aggregation is per customer, so key on the owner rather than the
        # account. Smurfs are individuals here, but keying on the account would
        # quietly break the moment a typology gives one host two accounts.
        owner = pop.account_owner[deposit_account]
        owner_key = np.where(pop.account_owner_is_entity[deposit_account], owner + 10**9, owner)
        deposit_ts = self._respect_ctr_aggregation(
            owner_key, deposit_amount, deposit_ts, controls.ctr_threshold_usd
        )

        # --- consolidation: each smurf forwards to the collector ---
        forward_account: list[int] = []
        forward_amount: list[float] = []
        forward_ts: list[int] = []
        for smurf in np.unique(deposit_account):
            mask = deposit_account == smurf
            total = float(deposit_amount[mask].sum())
            # Smurfs keep a cut. That is how recruitment works, and it also
            # stops inflow and outflow matching to the cent, which would be a
            # free feature for any pass-through rule.
            kept = total * r.uniform(0.02, 0.08)
            last = deposit_ts[mask].max().astype("datetime64[s]").astype("int64")
            forward_account.append(int(smurf))
            forward_amount.append(round(total - kept, 2))
            forward_ts.append(int(last + int(r.integers(4 * 3600, 3 * _SECONDS_PER_DAY))))

        result = InjectionResult()

        # --- labels for the standing entities ---
        labels: list[LabelSpec] = []
        confidence = {"easy": 1.0, "medium": 0.95, "hard": 0.85}[tier]
        for smurf in np.unique(deposit_account):
            labels.append(
                LabelSpec(
                    ring_id=ring_id,
                    typology=self.name,
                    role="smurf",
                    polarity="illicit",
                    confidence=confidence,
                    difficulty_tier=tier,
                    subject_type="account",
                    subject_id=str(pop.account_ids[smurf]),
                )
            )
            owner = pop.account_owner[smurf]
            labels.append(
                LabelSpec(
                    ring_id=ring_id,
                    typology=self.name,
                    role="smurf",
                    polarity="illicit",
                    confidence=confidence,
                    difficulty_tier=tier,
                    subject_type="individual",
                    subject_id=str(pop.tables["individual"]["individual_id"][int(owner)]),
                )
            )
        labels.append(
            LabelSpec(
                ring_id=ring_id,
                typology=self.name,
                role="collector",
                polarity="illicit",
                confidence=confidence,
                difficulty_tier=tier,
                subject_type="account",
                subject_id=str(pop.account_ids[collector]),
            )
        )
        labels.append(
            LabelSpec(
                ring_id=ring_id,
                typology=self.name,
                role="collector",
                polarity="illicit",
                confidence=confidence,
                difficulty_tier=tier,
                subject_type="legal_entity",
                subject_id=str(
                    pop.tables["legal_entity"]["entity_id"][int(pop.account_owner[collector])]
                ),
            )
        )

        # --- transaction labels, tagged for post-assembly resolution ---
        deposit_label = LabelSpec(
            ring_id=ring_id,
            typology=self.name,
            role="structured_deposit",
            polarity="illicit",
            confidence=confidence,
            difficulty_tier=tier,
            subject_type="transaction",
        )
        forward_label = LabelSpec(
            ring_id=ring_id,
            typology=self.name,
            role="consolidation_transfer",
            polarity="illicit",
            confidence=confidence,
            difficulty_tier=tier,
            subject_type="transaction",
        )
        labels.append(deposit_label)
        labels.append(forward_label)
        deposit_tag = len(labels) - 2
        forward_tag = len(labels) - 1

        result.labels = labels
        result.pending.add(
            from_account=deposit_account,
            amount=deposit_amount,
            ts=deposit_ts,
            txn_class="cash_deposit",
            direction="credit",
            label_tag=np.full(len(deposit_amount), deposit_tag, dtype=np.int64),
        )
        fwd_ts = (
            np.array(forward_ts, dtype="int64").astype("datetime64[s]").astype("datetime64[us]")
        )
        result.pending.add(
            from_account=np.array(forward_account, dtype=np.int64),
            to_account=np.full(len(forward_account), collector, dtype=np.int64),
            amount=np.array(forward_amount),
            ts=fwd_ts,
            txn_class="p2p_transfer",
            direction="debit",
            label_tag=np.full(len(forward_account), forward_tag, dtype=np.int64),
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

    @staticmethod
    def _respect_ctr_aggregation(
        owner_key: np.ndarray, amount: np.ndarray, ts: np.ndarray, threshold: float
    ) -> np.ndarray:
        """Push deposits apart so no customer's same-day cash total is reported.

        The institution aggregates same-day cash per customer before applying
        the threshold (31 CFR 1010.313), so two 9,000 deposits on one day are
        reported as 18,000. A scheme that let that happen would be caught by the
        bank for free; a generator that let it happen would be modelling a
        control the institution does not have, and would make T1 look harder to
        evade than it really is.

        Colliding deposits are moved to a later day rather than resized, which
        keeps the amount distribution the tier's knobs asked for.

        Keyed on the customer, not the account, because that is the unit the
        regulation aggregates over — and a single running tally is not enough:
        a deposit pushed forward can land on a day another deposit is later
        assigned to, so every day's running total has to stay addressable.
        """
        day = ts.astype("datetime64[D]").astype("int64")
        shifted = day.copy()
        ledger: dict[tuple[int, int], float] = {}

        # Process in (customer, day) order so shifts always move forward into
        # days not yet considered for that customer.
        for i in np.lexsort((day, owner_key)):
            key_owner, d = int(owner_key[i]), int(day[i])
            while ledger.get((key_owner, d), 0.0) + amount[i] > threshold:
                d += 1
            ledger[(key_owner, d)] = ledger.get((key_owner, d), 0.0) + amount[i]
            shifted[i] = d

        if not (shifted != day).any():
            return ts
        time_of_day = ts.astype("datetime64[s]").astype("int64") % _SECONDS_PER_DAY
        return (
            (shifted * _SECONDS_PER_DAY + time_of_day)
            .astype("datetime64[s]")
            .astype("datetime64[us]")
        )
