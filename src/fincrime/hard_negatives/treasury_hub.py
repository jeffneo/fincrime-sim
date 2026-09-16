"""Payroll / treasury sweep account — mimics T3 mule networks.

A company that runs payroll for its staff and sweeps cash between its own
accounts has extreme fan-in and fan-out and retains almost nothing: money
arrives and leaves within days, over and over. That is the textbook mule
signature, and the ``rapid_pass_through`` rule cannot tell the difference.

What separates it: the counterparty set is *stable*. The same people are paid
every fortnight, and they are linked by ``EMPLOYED_BY``. A mule ring's inbound
counterparties are unrelated to each other and change constantly, and its
members share devices. Stability over time is the feature, and it only exists
because the background runs for a full window.
"""

from __future__ import annotations

import numpy as np

from ..typologies.base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400


class TreasuryHub:
    name = "treasury_hub"

    #: The divisor that turns an entity budget into an instance count, so it
    #: has to be the number of entities this generator LABELS - not the number
    #: it touches. Only the hub company is a look-alike; the staff it pays are
    #: ordinary employees receiving a salary and carry no label. This said 6
    #: (hub plus five staff), which spent six times the budget per instance and
    #: produced 75 look-alikes where the configured prevalence asks for 450.
    #: With 113 mule positives at the mvp preset, 75 could not reach the
    #: look-alike-to-positive ratio of 2 the gate asks for even if every single
    #: one landed in the alert queue.
    entities_per_instance = 1

    def __init__(self, ctx: InjectionContext, *, mimics: str) -> None:
        self.ctx = ctx
        self.mimics = mimics

    def inject(self, count: int) -> InjectionResult:
        ctx = self.ctx
        pop = ctx.pop
        result = InjectionResult()

        # Employers with enough staff banking here to run a visible payroll.
        employers, staff_counts = np.unique(pop.employer[pop.employer >= 0], return_counts=True)
        eligible = employers[staff_counts >= 4]
        if eligible.size == 0:
            return result

        made = 0
        for attempt in range(count * 3):
            if made >= count:
                break
            instance_id = f"HN-{self.name}-{made:05d}"
            # The stream keys on the ATTEMPT, not on `made`. Keying it on
            # `made` meant a failed attempt left `made` unchanged, so the next
            # iteration rebuilt the same instance_id, drew the same stream, made
            # the same choice of employer and failed identically - one failure
            # silently consumed every remaining retry. This generator produced
            # 5 instances of an intended 75 at the mvp preset, and the pool was
            # never the problem: 2,685 employers have four or more staff here.
            r = ctx.rng.fresh("hard_negatives", self.name, instance_id, f"attempt-{attempt}")
            built = self._build(r, instance_id, eligible)
            if built is not None:
                result.extend(built)
                made += 1
        return result

    def _build(
        self, r: np.random.Generator, instance_id: str, eligible: np.ndarray
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        window = ctx.cfg.window

        entity_index = int(r.choice(eligible))
        hub = int(pop.entity_primary_account[entity_index])
        if hub < 0 or hub in ctx.claimed_accounts:
            return None

        staff = np.flatnonzero(pop.employer == entity_index)
        staff_accounts = np.array(
            [
                pop.individual_primary_account[s]
                for s in staff
                if pop.individual_primary_account[s] >= 0
                and pop.individual_primary_account[s] not in ctx.claimed_accounts
            ],
            dtype=np.int64,
        )
        if staff_accounts.size < 4:
            return None

        ctx.claimed_accounts.add(hub)
        # Staff accounts are NOT claimed: being paid by a treasury hub does not
        # stop someone also being recruited as a mule, and in reality the two
        # overlap. Only the hub itself is reserved.

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        salaries = (
            pop.individual_income[np.array([pop.account_owner[a] for a in staff_accounts])] / 26.0
        )

        out_account: list[int] = []
        out_to: list[int] = []
        out_amount: list[float] = []
        out_ts: list[int] = []
        # Fortnightly, on the same day each cycle - the stability that
        # distinguishes this from a ring. The recipient is recorded per row
        # rather than reconstructed by tiling, so the pairing cannot drift if
        # a future change skips a staff member.
        for cycle_start in range(3, window.days, 14):
            for account, salary in zip(staff_accounts, salaries, strict=True):
                out_account.append(hub)
                out_to.append(int(account))
                out_amount.append(round(float(salary) * r.normal(1.0, 0.02), 2))
                out_ts.append(
                    int(
                        start_epoch
                        + cycle_start * _SECONDS_PER_DAY
                        + r.integers(6 * 3600, 10 * 3600)
                    )
                )

        if not out_account:
            return None

        result = InjectionResult()
        labels = [
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.95,
                difficulty_tier="medium",
                subject_type="account",
                subject_id=str(pop.account_ids[hub]),
            ),
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.95,
                difficulty_tier="medium",
                subject_type="legal_entity",
                subject_id=str(pop.tables["legal_entity"]["entity_id"][entity_index]),
            ),
            LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.95,
                difficulty_tier="medium",
                subject_type="transaction",
            ),
        ]
        tag = len(labels) - 1
        result.labels = labels
        result.pending.add(
            from_account=np.array(out_account, dtype=np.int64),
            to_account=np.array(out_to, dtype=np.int64),
            amount=np.array(out_amount),
            ts=np.array(out_ts, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="payroll",
            direction="debit",
            label_tag=np.full(len(out_account), tag, dtype=np.int64),
        )
        result.rings = [
            RingSpec(
                ring_id=instance_id,
                typology=self.mimics,
                polarity="hard_negative",
                difficulty_tier="medium",
                knobs={"generator": self.name, "staff": int(staff_accounts.size)},
                injected_from=window.start,
                injected_to=window.end,
            )
        ]
        return result
