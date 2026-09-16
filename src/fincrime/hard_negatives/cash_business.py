"""Cash-intensive business whose staff bank the takings — mimics T1 structuring.

**Why this is the hard case.** A restaurant that sends three staff members to
the bank with the day's cash produces exactly the subgraph a smurfing ring
produces: several individuals making sub-threshold cash deposits, each
forwarding to one business account. Same star, same channel, same
under-threshold amounts, same convergence. The Cypher pattern that recovers a
structuring ring recovers this too, and it should — that is what makes the
pattern alone insufficient.

**What separates them, all of it visible in the graph.** The depositors are
employees of the business (``EMPLOYED_BY``). The cash arriving is proportional
to card settlement from the same trading days, because a real restaurant takes
both. Deposits follow the week — heavy Monday after a weekend, light midweek.
Suppliers get paid on a cadence. The business has been trading since before the
window opened. A smurf collector has none of that.

Sub-threshold deposits here are *not* evasion. A restaurant's daily take
genuinely is a few thousand dollars, and a manager who banks each day rather
than weekly is doing the ordinary thing, not structuring. The amounts look the
same; the reason differs.
"""

from __future__ import annotations

import numpy as np

from ..typologies.base import InjectionContext, InjectionResult, LabelSpec, RingSpec

_SECONDS_PER_DAY = 86_400

#: Weekly rhythm of a hospitality business's cash takings, Monday first. The
#: Monday peak is the weekend's trade being banked, and that regularity is one
#: of the things a detector can use to tell this from a ring.
_WEEKDAY_TAKINGS = np.array([1.55, 0.80, 0.75, 0.85, 1.20, 1.45, 0.60])


class CashIntensiveBusiness:
    name = "cash_intensive_business"

    #: Depositors plus the business itself, for converting an entity budget
    #: into an instance count.
    entities_per_instance = 4

    def __init__(self, ctx: InjectionContext, *, mimics: str) -> None:
        self.ctx = ctx
        self.mimics = mimics

    def _candidates(self) -> np.ndarray:
        """Cash-intensive businesses that have staff to do the banking.

        Pre-filtered on staff count rather than checked per attempt. Without
        it the generator picks a business at random, finds it has nobody to
        send to the bank, and abandons the instance - which produced 1 of a
        requested 16 at the dev preset and left the structuring typology with
        almost no look-alikes to hide among.
        """
        pop = self.ctx.pop
        cash_intensive = pop.tables["legal_entity"]["is_cash_intensive"].to_numpy()
        business_accounts = np.flatnonzero(
            (pop.account_type == "checking") & pop.account_owner_is_entity
        )
        owner = pop.account_owner[business_accounts]

        # Staff with their own account here, counted per employer.
        has_account = pop.individual_primary_account >= 0
        employers = pop.employer[has_account & (pop.employer >= 0)]
        staffed = np.bincount(employers, minlength=len(cash_intensive)) >= 2

        # Only businesses whose legitimate cash handling is large enough to
        # trip the structuring rule on its own.
        #
        # A neighbourhood restaurant's takings split across three staff is
        # ~USD 7k each a month - comfortably under the USD 25k aggregate
        # threshold, so it never reaches the alert queue and never competes
        # with a smurf ring for attention. The hard negative that matters is
        # the supermarket or filling station banking USD 60k of cash a month
        # through two employees: entirely lawful, and indistinguishable from
        # placement on volume alone.
        monthly_cash = pop.entity_revenue * pop.entity_cash_ratio / 12.0
        threshold = float(self.ctx.controls.monitoring["cash_structuring_aggregate_usd"])
        substantial = monthly_cash >= threshold

        return business_accounts[cash_intensive[owner] & staffed[owner] & substantial[owner]]

    def inject(self, count: int) -> InjectionResult:
        ctx = self.ctx
        result = InjectionResult()
        pool = self._candidates()
        if pool.size == 0:
            return result

        made = 0
        for attempt in range(count * 3):  # allow for skips
            if made >= count:
                break
            instance_id = f"HN-{self.name}-{made:05d}"
            # Keyed on the attempt, not on `made` - see the note in
            # treasury_hub.py. A failed attempt otherwise redraws the identical
            # stream and fails the same way until the retries run out.
            r = ctx.rng.fresh("hard_negatives", self.name, instance_id, f"attempt-{attempt}")
            built = self._build(r, instance_id, pool)
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
        threshold = ctx.controls.ctr_threshold_usd

        available = np.array([a for a in pool if a not in ctx.claimed_accounts])
        if available.size == 0:
            return None
        business_account = int(r.choice(available))
        entity_index = int(pop.account_owner[business_account])

        # Depositors: staff, where the business has any. The employment edge is
        # the distinguishing signal, so using real employees is the point - a
        # generator that picked strangers would be building a harder negative
        # than a legitimate business actually is.
        staff = np.flatnonzero(pop.employer == entity_index)
        staff = np.array([s for s in staff if pop.individual_primary_account[s] >= 0])
        # Two or three, not four or five. A business sends the same couple of
        # trusted people to the bank, and concentrating the takings is what
        # puts each depositor's volume in the same range as a smurf's.
        n_depositors = int(r.integers(2, 4))
        if staff.size >= 2:
            chosen = r.choice(staff, min(n_depositors, staff.size), replace=False)
            depositor_accounts = pop.individual_primary_account[chosen]
        else:
            # Owner-operated: a single proprietor banking their own takings.
            # Still a star, just a small one.
            return None

        depositor_accounts = np.array(
            [a for a in depositor_accounts if a not in ctx.claimed_accounts], dtype=np.int64
        )
        if depositor_accounts.size < 2:
            return None
        ctx.claimed_accounts.update(int(a) for a in depositor_accounts)
        ctx.claimed_accounts.add(business_account)

        # --- takings ---
        revenue = float(pop.entity_revenue[entity_index])
        cash_ratio = float(pop.entity_cash_ratio[entity_index])
        daily_cash = max(revenue * cash_ratio / 365.0, 150.0)

        accounts: list[int] = []
        amounts: list[float] = []
        stamps: list[int] = []

        start_epoch = np.datetime64(window.start, "s").astype("int64")
        for day in range(window.days):
            weekday = (np.datetime64(window.start, "D").astype(int) + day + 3) % 7
            takings = daily_cash * _WEEKDAY_TAKINGS[weekday] * r.lognormal(0.0, 0.22)
            if takings < 100.0:
                continue
            # A big day gets banked by two staff rather than one, which is how
            # the star gains its breadth.
            runs = 1 if takings < 4_000 else int(r.integers(1, 3))
            per_run = takings / runs
            for _ in range(runs):
                # A real deposit can exceed the threshold; the business simply
                # gets a CTR filed and thinks nothing of it. Capping here would
                # make "never above 10k" a free feature separating this from a
                # ring that genuinely must stay under.
                accounts.append(int(r.choice(depositor_accounts)))
                amounts.append(round(per_run * r.lognormal(0.0, 0.08), 2))
                stamps.append(
                    int(start_epoch + day * _SECONDS_PER_DAY + r.integers(9 * 3600, 19 * 3600))
                )

        if len(amounts) < 10:
            return None

        deposit_ts = (
            np.array(stamps, dtype="int64").astype("datetime64[s]").astype("datetime64[us]")
        )
        deposit_account = np.array(accounts, dtype=np.int64)
        deposit_amount = np.array(amounts)

        # --- staff forward the takings to the business account ---
        forward_account: list[int] = []
        forward_amount: list[float] = []
        forward_ts: list[int] = []
        day_index = deposit_ts.astype("datetime64[D]").astype("int64")
        for account in np.unique(deposit_account):
            mask = deposit_account == account
            # Forwarded in weekly batches, the way a manager actually sweeps.
            for week in np.unique(day_index[mask] // 7):
                batch = mask & (day_index // 7 == week)
                total = float(deposit_amount[batch].sum())
                if total <= 0:
                    continue
                last = int(day_index[batch].max())
                forward_account.append(int(account))
                forward_amount.append(round(total, 2))
                forward_ts.append(
                    int(
                        start_epoch
                        + (last + 1) * _SECONDS_PER_DAY
                        + r.integers(9 * 3600, 17 * 3600)
                    )
                )

        result = InjectionResult()
        labels: list[LabelSpec] = []

        def _label(role: str, subject_type: str, subject_id: str) -> LabelSpec:
            return LabelSpec(
                ring_id=instance_id,
                typology=self.mimics,
                role=role,
                polarity="hard_negative",
                # Below 1.0: these are genuinely ambiguous on the face of the
                # transaction data, which is the whole point of including them.
                confidence=0.9,
                difficulty_tier="hard",
                subject_type=subject_type,
                subject_id=subject_id,
            )

        for account in np.unique(deposit_account):
            labels.append(_label(self.name, "account", str(pop.account_ids[account])))
            owner = int(pop.account_owner[account])
            labels.append(
                _label(
                    self.name,
                    "individual",
                    str(pop.tables["individual"]["individual_id"][owner]),
                )
            )
        labels.append(_label(self.name, "account", str(pop.account_ids[business_account])))
        labels.append(
            _label(
                self.name,
                "legal_entity",
                str(pop.tables["legal_entity"]["entity_id"][entity_index]),
            )
        )

        deposit_label = _label(self.name, "transaction", "")
        deposit_label.subject_id = None
        forward_label = _label(self.name, "transaction", "")
        forward_label.subject_id = None
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
        if forward_account:
            result.pending.add(
                from_account=np.array(forward_account, dtype=np.int64),
                to_account=np.full(len(forward_account), business_account, dtype=np.int64),
                amount=np.array(forward_amount),
                ts=np.array(forward_ts, dtype="int64")
                .astype("datetime64[s]")
                .astype("datetime64[us]"),
                txn_class="p2p_transfer",
                direction="debit",
                label_tag=np.full(len(forward_account), forward_tag, dtype=np.int64),
            )

        result.rings = [
            RingSpec(
                ring_id=instance_id,
                typology=self.mimics,
                polarity="hard_negative",
                difficulty_tier="hard",
                knobs={"generator": self.name, "threshold_usd": threshold},
                injected_from=window.start,
                injected_to=window.end,
            )
        ]
        return result
