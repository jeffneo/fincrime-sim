"""Legitimate holding structure — mimics T2 shell-company layering.

A group with a holding company and operating subsidiaries moves money between
its own entities constantly: management charges, intra-group loans, cash
pooling. Ownership runs through several layers. On the ownership graph alone
that is indistinguishable from a layering chain, and a query looking for
chained ``BENEFICIAL_OWNER_OF`` hops finds both.

What separates it: the subsidiaries actually trade. They have employees, real
customer receipts, supplier payments and payroll — the background gives them
all of that because they are ordinary population members. A shell has an
ownership chain and nothing underneath it. Ownership is also disclosed here,
which the layering typology (M3) will vary.
"""

from __future__ import annotations

import numpy as np

from ..typologies.base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400


class HoldingStructure:
    name = "holding_structure"
    entities_per_instance = 3

    def __init__(self, ctx: InjectionContext, *, mimics: str) -> None:
        self.ctx = ctx
        self.mimics = mimics

    def inject(self, count: int) -> InjectionResult:
        ctx = self.ctx
        pop = ctx.pop
        result = InjectionResult()

        parents = pop.tables["entity_bo_of_entity"]
        if parents.height == 0:
            return result

        entity_ids = pop.tables["legal_entity"]["entity_id"].to_list()
        index_of = {eid: i for i, eid in enumerate(entity_ids)}
        groups: dict[int, list[int]] = {}
        for parent, child in zip(
            parents["start_id"].to_list(), parents["end_id"].to_list(), strict=True
        ):
            groups.setdefault(index_of[parent], []).append(index_of[child])
        usable = [(p, c) for p, c in groups.items() if len(c) >= 1]
        if not usable:
            return result

        made = 0
        for parent, children in usable:
            if made >= count:
                break
            instance_id = f"HN-{self.name}-{made:05d}"
            r = ctx.rng.fresh("hard_negatives", self.name, instance_id)
            built = self._build(r, instance_id, parent, children)
            if built is not None:
                result.extend(built)
                made += 1
        return result

    def _build(
        self, r: np.random.Generator, instance_id: str, parent: int, children: list[int]
    ) -> InjectionResult | None:
        ctx = self.ctx
        pop = ctx.pop
        window = ctx.cfg.window

        parent_account = int(pop.entity_primary_account[parent])
        if parent_account < 0 or parent_account in ctx.claimed_accounts:
            return None
        child_accounts = np.array(
            [
                pop.entity_primary_account[c]
                for c in children
                if pop.entity_primary_account[c] >= 0
                and pop.entity_primary_account[c] not in ctx.claimed_accounts
            ],
            dtype=np.int64,
        )
        if child_accounts.size == 0:
            return None

        ctx.claimed_accounts.add(parent_account)
        ctx.claimed_accounts.update(int(a) for a in child_accounts)

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        accounts: list[int] = []
        targets: list[int] = []
        amounts: list[float] = []
        stamps: list[int] = []

        # Monthly management charge from each subsidiary up to the parent, plus
        # occasional intra-group funding back down. Regular and documented -
        # the opposite of a layering chain's rapid one-way pass-through.
        for month_start in range(5, window.days, 30):
            for child_account in child_accounts:
                revenue = float(pop.entity_revenue[pop.account_owner[child_account]])
                charge = max(revenue / 12.0 * r.uniform(0.03, 0.09), 250.0)
                accounts.append(int(child_account))
                targets.append(parent_account)
                amounts.append(round(charge, 2))
                stamps.append(
                    int(
                        start_epoch
                        + month_start * _SECONDS_PER_DAY
                        + r.integers(9 * 3600, 17 * 3600)
                    )
                )
            if r.random() < 0.35:
                child_account = int(r.choice(child_accounts))
                revenue = float(pop.entity_revenue[pop.account_owner[child_account]])
                accounts.append(parent_account)
                targets.append(child_account)
                amounts.append(round(max(revenue / 12.0 * r.uniform(0.1, 0.4), 500.0), 2))
                stamps.append(
                    int(
                        start_epoch
                        + (month_start + 3) * _SECONDS_PER_DAY
                        + r.integers(9 * 3600, 17 * 3600)
                    )
                )

        if not accounts:
            return None

        result = InjectionResult()
        labels: list[LabelSpec] = []

        def _label(subject_type: str, subject_id: str | None) -> LabelSpec:
            return LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=self.name,
                polarity="hard_negative",
                confidence=0.92,
                difficulty_tier="medium",
                subject_type=subject_type,
                subject_id=subject_id,
            )

        entity_ids = pop.tables["legal_entity"]["entity_id"]
        labels.append(_label("legal_entity", str(entity_ids[parent])))
        labels.append(_label("account", str(pop.account_ids[parent_account])))
        for child_account in child_accounts:
            labels.append(_label("account", str(pop.account_ids[child_account])))
            labels.append(
                _label("legal_entity", str(entity_ids[int(pop.account_owner[child_account])]))
            )
        labels.append(_label("transaction", None))
        tag = len(labels) - 1

        result.labels = labels
        result.pending.add(
            from_account=np.array(accounts, dtype=np.int64),
            to_account=np.array(targets, dtype=np.int64),
            amount=np.array(amounts),
            ts=np.array(stamps, dtype="int64").astype("datetime64[s]").astype("datetime64[us]"),
            txn_class="internal_transfer",
            direction="debit",
            label_tag=np.full(len(accounts), tag, dtype=np.int64),
        )
        result.rings = [
            RingSpec(
                ring_id=instance_id,
                typology=self.mimics,
                polarity="hard_negative",
                difficulty_tier="medium",
                knobs={"generator": self.name, "subsidiaries": int(child_accounts.size)},
                injected_from=window.start,
                injected_to=window.end,
            )
        ]
        return result
