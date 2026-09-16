"""Frequent traveler — mimics T4 card-not-present fraud.

Someone abroad presents every headline fraud signal at once: a device the bank
has not seen, a foreign IP, merchant countries that do not match their home
address, and a burst of spend compressed into a few days. A velocity or
geo-mismatch rule cannot separate this from a stolen card.

What separates it, and the reason M1 built merchant loyalty at all: the
traveler keeps their own merchant preferences. They book the same airline, and
the card keeps being used afterwards at the merchants they always used. A
fraudster has no history with the card, drains it, and stops. Continuity across
the burst is the feature — which only exists because each customer has a
personal merchant set spanning the whole window.
"""

from __future__ import annotations

import numpy as np

from ..reference import JURISDICTIONS, MERCHANT_CATEGORIES
from ..typologies.base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400

#: Where people go. Real destinations, so the geo mismatch is plausible rather
#: than uniformly random across every jurisdiction in the reference table.
_DESTINATIONS = ("GB", "FR", "DE", "JP", "MX", "CA", "AU", "SG")


class FrequentTraveler:
    name = "frequent_traveler"
    entities_per_instance = 1

    def __init__(self, ctx: InjectionContext, *, mimics: str) -> None:
        self.ctx = ctx
        self.mimics = mimics

    def inject(self, count: int) -> InjectionResult:
        ctx = self.ctx
        pop = ctx.pop
        result = InjectionResult()

        # Cardholders with enough income to travel.
        with_cards = pop.card_account
        eligible = np.array(
            [
                a
                for a in np.unique(with_cards)
                if a not in ctx.claimed_accounts
                and pop.individual_income[pop.account_owner[a]] > 55_000
            ],
            dtype=np.int64,
        )
        if eligible.size == 0:
            return result

        made = 0
        for _ in range(count * 3):
            if made >= count:
                break
            instance_id = f"HN-{self.name}-{made:05d}"
            r = ctx.rng.fresh("hard_negatives", self.name, instance_id)
            built = self._build(r, instance_id, eligible)
            if built is not None:
                result.extend(built)
                made += 1
        return result

    def _build(
        self, r: np.random.Generator, instance_id: str, pool: np.ndarray
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        window = ctx.cfg.window

        available = np.array([a for a in pool if a not in ctx.claimed_accounts])
        if available.size == 0:
            return None
        account = int(r.choice(available))
        ctx.claimed_accounts.add(account)

        card_rows = np.flatnonzero(pop.card_account == account)
        if card_rows.size == 0:
            return None
        card = int(r.choice(card_rows))

        # Merchants abroad, in the categories a traveler actually uses.
        travel_mcc = {"7011", "4511", "5812", "5814", "4121", "5999"}
        mcc_codes = np.array([c.mcc for c in MERCHANT_CATEGORIES])
        merchant_countries = pop.tables["merchant"]["country"].to_numpy()
        destination = str(r.choice(np.array(_DESTINATIONS)))
        abroad = np.flatnonzero(
            (merchant_countries == destination)
            & np.isin(mcc_codes[pop.merchant_mcc_idx], list(travel_mcc))
        )
        if abroad.size == 0:
            # No foreign merchant in that category; fall back to any merchant
            # outside the domestic market rather than skipping the instance.
            abroad = np.flatnonzero(merchant_countries != "US")
        if abroad.size == 0:
            return None

        trip_start = int(r.integers(0, max(window.days - 12, 1)))
        trip_days = int(r.integers(4, 13))
        n_txn = int(r.integers(8, 30))

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        income = float(pop.individual_income[pop.account_owner[account]])
        merchants = r.choice(abroad, n_txn)
        mu = np.array([c.amount_mu for c in MERCHANT_CATEGORIES])[pop.merchant_mcc_idx[merchants]]
        amounts = np.round(np.maximum(r.lognormal(mu, 0.8) * max(income / 90_000.0, 0.6), 4.0), 2)
        days = trip_start + r.integers(0, trip_days, n_txn)
        stamps = start_epoch + days * _SECONDS_PER_DAY + r.integers(8 * 3600, 23 * 3600, n_txn)

        result = InjectionResult()
        labels = [
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.9,
                difficulty_tier="hard",
                subject_type="account",
                subject_id=str(pop.account_ids[account]),
            ),
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.9,
                difficulty_tier="hard",
                subject_type="individual",
                subject_id=str(
                    pop.tables["individual"]["individual_id"][int(pop.account_owner[account])]
                ),
            ),
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.9,
                difficulty_tier="hard",
                subject_type="transaction",
            ),
        ]
        tag = len(labels) - 1
        result.labels = labels
        result.pending.add(
            from_account=np.full(n_txn, account, dtype=np.int64),
            amount=amounts,
            ts=stamps.astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="retail_online",
            direction="debit",
            merchant=merchants,
            card=np.full(n_txn, card, dtype=np.int64),
            label_tag=np.full(n_txn, tag, dtype=np.int64),
        )
        result.rings = [
            RingSpec(
                ring_id=instance_id,
                typology=self.mimics,
                difficulty_tier="hard",
                knobs={"generator": self.name, "destination": destination, "days": trip_days},
                injected_from=window.start,
                injected_to=window.end,
            )
        ]
        return result


#: Jurisdiction codes, exposed for tests that assert destinations are real.
KNOWN_JURISDICTIONS = {j.code for j in JURISDICTIONS}
