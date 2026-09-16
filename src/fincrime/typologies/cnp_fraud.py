"""T4 — card-not-present fraud.

Compromised card numbers used online. The operator tests each card with a small
authorization first, then escalates if it clears, and works through a batch in
a burst before the issuer catches up.

**Topology** — a fraudster's device touching many cards that have nothing else
in common. That cross-card device reuse is the strongest available signal and
is exactly what the graph makes visible: each card in isolation shows a spend
burst, which thousands of legitimate travellers also show.

**What makes it hard.** The easy tier puts forty cards on one device, tests
every one with a sub-dollar auth, and drains them in an afternoon at the same
few merchant categories. The hard tier uses one or two cards per device, skips
the test auth entirely, spends inside the cardholder's own normal range, and
spreads over weeks. At that point the only thing left is that the merchants are
ones the cardholder never uses — and the frequent-traveler hard negative (M4)
looks exactly like that for a fortnight every year.

Declines matter here. The background declines ~1.2% of card traffic, which is
what gives a card-testing burst somewhere to hide: a run of small failed
authorizations is unremarkable in a log that already contains them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..reference import CNP_FRAUD_PREFERRED_MCC, MERCHANT_CATEGORIES
from .base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400


class CnpFraud:
    name = "cnp_fraud"

    def __init__(self, ctx: InjectionContext) -> None:
        self.ctx = ctx
        self.spec: dict[str, Any] = ctx.cfg.typologies["typologies"][self.name]

    def inject(self, n_rings: int, tier_counts: dict[str, int]) -> InjectionResult:
        ctx = self.ctx
        result = InjectionResult()
        if len(ctx.pop.card_ids) == 0:
            return result

        made = 0
        for tier, count in tier_counts.items():
            knobs = self.spec["tiers"][tier]
            for _ in range(count):
                ring_id = f"RING-{self.name}-{made:05d}"
                r = ctx.rng.fresh("typologies", self.name, ring_id)
                built = self._build_ring(r, ring_id, tier, knobs)
                if built is not None:
                    result.extend(built)
                    made += 1
        return result

    def _build_ring(
        self, r: np.random.Generator, ring_id: str, tier: str, knobs: dict[str, Any]
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        window = ctx.cfg.window

        n_cards = int(r.integers(knobs["cards_per_device"][0], knobs["cards_per_device"][1] + 1))
        candidates = np.array(
            [
                i
                for i in range(len(pop.card_ids))
                if int(pop.card_account[i]) not in ctx.claimed_accounts
            ],
            dtype=np.int64,
        )
        if candidates.size < n_cards:
            return None
        cards = r.choice(candidates, n_cards, replace=False)
        accounts = pop.card_account[cards]
        ctx.claimed_accounts.update(int(a) for a in accounts)

        # Merchant pool. Weighted to the categories CNP fraud concentrates in,
        # drawn from the same taxonomy the background uses - a fraud ring that
        # only ever touched merchants no legitimate customer visits would be
        # separable on the merchant alone.
        mcc_codes = np.array([c.mcc for c in MERCHANT_CATEGORIES])[pop.merchant_mcc_idx]
        preferred = np.flatnonzero(np.isin(mcc_codes, list(CNP_FRAUD_PREFERRED_MCC)))
        concentration = float(knobs["mcc_concentration"])
        if preferred.size == 0:
            preferred = np.arange(len(pop.merchant_ids))

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        max_span = max(window.days - 2, 1)
        burst_start = int(r.integers(0, max_span))
        burst_hours = float(r.uniform(*knobs["burst_window_hours"]))

        from_accounts: list[int] = []
        card_rows: list[int] = []
        merchants: list[int] = []
        amounts: list[float] = []
        stamps: list[int] = []

        for card, account in zip(cards, accounts, strict=True):
            normal = self._typical_spend(pop, int(account))
            ratio_lo, ratio_hi = knobs["amount_ratio_vs_normal"]

            if knobs.get("test_auth"):
                # Card testing: a sub-dollar-to-few-dollar authorization to see
                # whether the number is live.
                lo, hi = knobs["test_auth_amount_usd"]
                from_accounts.append(int(account))
                card_rows.append(int(card))
                merchants.append(int(self._pick_merchant(r, pop, preferred, concentration)))
                amounts.append(round(float(r.uniform(lo, hi)), 2))
                stamps.append(
                    int(
                        start_epoch
                        + burst_start * _SECONDS_PER_DAY
                        + int(r.uniform(0, burst_hours) * 3600)
                    )
                )

            for _ in range(
                int(r.integers(knobs["txns_per_card"][0], knobs["txns_per_card"][1] + 1))
            ):
                offset_hours = r.uniform(0, burst_hours)
                when = (
                    start_epoch
                    + burst_start * _SECONDS_PER_DAY
                    + int(offset_hours * 3600)
                    + int(r.integers(0, 3600))
                )
                if when >= start_epoch + max_span * _SECONDS_PER_DAY:
                    continue
                from_accounts.append(int(account))
                card_rows.append(int(card))
                merchants.append(int(self._pick_merchant(r, pop, preferred, concentration)))
                amounts.append(round(max(normal * r.uniform(ratio_lo, ratio_hi), 1.0), 2))
                stamps.append(int(when))

        if not amounts:
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

        for card, account in zip(cards, accounts, strict=True):
            labels.append(_label("compromised_card", "card", str(pop.card_ids[card])))
            # The cardholder is a VICTIM, not a perpetrator. Labelled so a
            # scorer can tell the two apart - flagging the victim's account is
            # the correct outcome for the bank and the wrong one for an
            # investigator, and a dataset that conflated them would score both
            # the same.
            labels.append(_label("victim", "account", str(pop.account_ids[account])))
            # And the person. Scoring is customer-level, so labelling only the
            # account and the card left this typology with no positives at all
            # and silently absent from the calibration report.
            labels.append(
                _label(
                    "victim",
                    "individual",
                    str(pop.tables["individual"]["individual_id"][int(pop.account_owner[account])]),
                )
            )

        labels.append(_label("fraudulent_charge", "transaction", None))
        charge_tag = len(labels) - 1

        result.labels = labels
        result.pending.add(
            from_account=np.array(from_accounts, dtype=np.int64),
            amount=np.array(amounts),
            ts=np.array(stamps, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="retail_online",
            direction="debit",
            merchant=np.array(merchants, dtype=np.int64),
            card=np.array(card_rows, dtype=np.int64),
            label_tag=np.full(len(amounts), charge_tag, dtype=np.int64),
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
    def _typical_spend(pop, account: int) -> float:
        """A rough sense of the cardholder's normal ticket size.

        Derived from income rather than from their actual transactions: the
        typology is planned before the background stream exists, and the point
        of ``amount_ratio_vs_normal`` is to place the fraud relative to what
        this particular cardholder usually spends. A flat amount would make the
        hard tier's "inside their own range" knob meaningless.
        """
        income = float(pop.individual_income[pop.account_owner[account]])
        return max(income / 1_400.0, 12.0)

    @staticmethod
    def _pick_merchant(
        r: np.random.Generator, pop, preferred: np.ndarray, concentration: float
    ) -> int:
        """Pick a merchant, concentrating on the fraud-preferred categories."""
        if r.random() < concentration:
            return int(r.choice(preferred))
        return int(r.integers(0, len(pop.merchant_ids)))
