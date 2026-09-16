"""Background behavior: the transaction streams that make up normal activity.

This is the noise the whole dataset rests on. Spec §5.4 is blunt about why it
matters — the difficulty of AML detection is almost entirely the difficulty of
separating true suspicious patterns from superficially similar legitimate ones,
so a weak background makes every typology trivially detectable no matter how
carefully the typology itself is built.

Generation is vectorized and chunked by month (PHASE1-PLAN.md D1/D2). Each
account carries its own archetype parameters — the agent-based model — but
events are sampled in bulk rather than by stepping a scheduler per entity per
day, which at the mvp preset would be ~36M Python-level activations.

Two structures do most of the realism work:

* **Recurring vs. discretionary.** Payroll, rent, utilities and subscriptions
  arrive on a cadence with low variance; card spend is a non-homogeneous
  Poisson process modulated by day of week and proximity to payday. Real
  account activity is a mixture of the two, and detectors key on the mixture.
* **Merchant loyalty.** Each customer draws a personal merchant set from the
  global popularity distribution and then spends mostly inside it. That gives
  the account-merchant graph its heavy tail, and it is what makes the
  frequent-traveler hard negative (M4) distinguishable from card fraud: the
  traveler keeps their merchant preferences, the fraudster has none.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from .config import RunConfig
from .population import Population
from .reference import (
    BUSINESS_ARCHETYPES,
    MERCHANT_CATEGORIES,
    RETAIL_ARCHETYPES,
    TXN_CLASSES,
)
from .rng import StreamRegistry

#: Relative transaction intensity by weekday (Mon=0). Card spend peaks Friday
#: and Saturday; Sunday is quiet. Flat weekly activity is one of the fastest
#: ways to make synthetic data obvious.
_DOW_WEIGHT = np.array([0.92, 0.90, 0.95, 1.02, 1.28, 1.34, 0.78])

#: Hour-of-day intensity for consumer activity. Bimodal - lunchtime and early
#: evening - with a near-dead overnight window.
_HOUR_WEIGHT = np.array(
    [
        0.15,
        0.08,
        0.05,
        0.04,
        0.04,
        0.08,
        0.28,
        0.62,
        0.95,
        1.05,
        1.12,
        1.35,
        1.42,
        1.20,
        1.10,
        1.15,
        1.32,
        1.55,
        1.48,
        1.25,
        0.95,
        0.70,
        0.45,
        0.25,
    ]
)
_HOUR_P = _HOUR_WEIGHT / _HOUR_WEIGHT.sum()

#: Business-hours intensity, for commercial accounts.
_BIZ_HOUR_WEIGHT = np.zeros(24)
_BIZ_HOUR_WEIGHT[8:19] = np.array([0.6, 1.1, 1.3, 1.25, 1.0, 1.15, 1.3, 1.2, 1.05, 0.8, 0.4])
_BIZ_HOUR_P = _BIZ_HOUR_WEIGHT / _BIZ_HOUR_WEIGHT.sum()

#: Spending lifts in the days just after payday and sags before it.
_PAYDAY_LIFT = 1.45
_PAYDAY_DECAY_DAYS = 5

_SECONDS_PER_DAY = 86_400

#: Receives one month of generated transactions and their edge tables. Keeping
#: this a plain callable rather than importing the Parquet writer here means
#: behavior generation has no opinion about where its output goes - the M2-M4
#: typology stages will feed the same sink.
TxnSink = Callable[[pl.DataFrame, dict[str, pl.DataFrame]], None]


#: Transaction classes, in a fixed order, so a class can be carried as a small
#: integer code rather than as a string. This matters at scale: numpy stores a
#: unicode array at fixed width, so ``np.full(n, "internal_transfer")`` costs 68
#: bytes per row. At the mvp preset that is ~3.7GB for one column, and there are
#: two of them. Codes cost 1 byte and are expanded once, at assembly.
_CLASS_CODES = tuple(TXN_CLASSES)
_CLASS_INDEX = {name: i for i, name in enumerate(_CLASS_CODES)}
_DIRECTION_CODES = ("debit", "credit")
_DIRECTION_INDEX = {name: i for i, name in enumerate(_DIRECTION_CODES)}


@dataclass(slots=True)
class TxnBuffer:
    """Accumulates one month of transactions as flat arrays.

    Columns are kept as separate arrays rather than as a DataFrame per stream
    because a month at the mvp preset is ~2.5M rows across a dozen streams, and
    concatenating DataFrames that many times costs more than the generation.
    """

    from_account: list[np.ndarray] = field(default_factory=list)
    to_account: list[np.ndarray] = field(default_factory=list)
    merchant: list[np.ndarray] = field(default_factory=list)
    card: list[np.ndarray] = field(default_factory=list)
    amount: list[np.ndarray] = field(default_factory=list)
    ts: list[np.ndarray] = field(default_factory=list)
    txn_class: list[np.ndarray] = field(default_factory=list)
    direction: list[np.ndarray] = field(default_factory=list)

    def add(
        self,
        *,
        from_account: np.ndarray,
        amount: np.ndarray,
        ts: np.ndarray,
        txn_class: str,
        direction: str,
        to_account: np.ndarray | None = None,
        merchant: np.ndarray | None = None,
        card: np.ndarray | None = None,
    ) -> None:
        n = len(from_account)
        if n == 0:
            return
        self.from_account.append(np.asarray(from_account, dtype=np.int64))
        self.to_account.append(
            np.asarray(to_account, dtype=np.int64)
            if to_account is not None
            else np.full(n, -1, dtype=np.int64)
        )
        self.merchant.append(
            np.asarray(merchant, dtype=np.int64)
            if merchant is not None
            else np.full(n, -1, dtype=np.int64)
        )
        self.card.append(
            np.asarray(card, dtype=np.int64) if card is not None else np.full(n, -1, dtype=np.int64)
        )
        self.amount.append(np.round(amount, 2))
        self.ts.append(ts)
        self.txn_class.append(np.full(n, _CLASS_INDEX[txn_class], dtype=np.int8))
        self.direction.append(np.full(n, _DIRECTION_INDEX[direction], dtype=np.int8))

    def concat(self) -> dict[str, np.ndarray]:
        """Concatenate the month's streams, in timestamp order.

        Sorting here rather than once over the whole window keeps the sort
        working set to one month. Months are generated in order and every event
        falls inside its own month, so per-month sorting yields a globally
        ordered log.
        """
        if not self.from_account:
            return {}
        out = {
            "from_account": np.concatenate(self.from_account),
            "to_account": np.concatenate(self.to_account),
            "merchant": np.concatenate(self.merchant),
            "card": np.concatenate(self.card),
            "amount": np.concatenate(self.amount),
            "ts": np.concatenate(self.ts),
            "txn_class": np.concatenate(self.txn_class),
            "direction": np.concatenate(self.direction),
        }
        order = np.argsort(out["ts"], kind="stable")
        return {k: v[order] for k, v in out.items()}


# ---------------------------------------------------------------------------
# Event sampling primitives
# ---------------------------------------------------------------------------


def _poisson_events(
    rng: np.random.Generator,
    daily_rate: np.ndarray,
    day_weights: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample a non-homogeneous Poisson process per account.

    Returns ``(account_row, day_offset)`` — one entry per event. The intensity
    matrix is accounts x days, which is ~5M cells for a month at the mvp
    preset: large enough to care about, small enough to hold.
    """
    if len(daily_rate) == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    lam = daily_rate[:, None] * day_weights[None, :]
    counts = rng.poisson(lam)
    flat = counts.ravel()
    total = int(flat.sum())
    if total == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    cell = np.repeat(np.arange(flat.size, dtype=np.int64), flat)
    n_days = day_weights.size
    return cell // n_days, cell % n_days


def _periodic_days(
    cadence: str, n_days: int, phase: np.ndarray, month_index: int
) -> tuple[np.ndarray, np.ndarray]:
    """Day offsets for a recurring payment, per account.

    ``phase`` staggers accounts so not every customer in the bank is paid on
    the same day — a synchronized payroll spike across 100K customers would be
    an obvious artifact and would also swamp the day-of-week seasonality.
    """
    n = len(phase)
    if n == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    if cadence == "weekly":
        period = 7
    elif cadence == "biweekly":
        period = 14
    else:  # monthly
        period = 0

    if period == 0:
        day = np.minimum(phase % 28, n_days - 1)
        return np.arange(n, dtype=np.int64), day

    # Offset by the month index so a biweekly cadence stays continuous across
    # month boundaries instead of restarting each month.
    starts = (phase + month_index * n_days) % period
    occurrences = (n_days - starts + period - 1) // period
    rows = np.repeat(np.arange(n, dtype=np.int64), occurrences)
    within = np.concatenate([np.arange(k) for k in occurrences]) if n else np.empty(0, np.int64)
    days = starts[rows] + within * period
    keep = days < n_days
    return rows[keep], days[keep]


def _timestamps(
    rng: np.random.Generator,
    month_start: date,
    day_offset: np.ndarray,
    *,
    business_hours: bool = False,
) -> np.ndarray:
    """Turn day offsets into microsecond UTC timestamps."""
    n = len(day_offset)
    p = _BIZ_HOUR_P if business_hours else _HOUR_P
    hour = rng.choice(24, n, p=p)
    second = rng.integers(0, 3600, n)
    base = np.datetime64(month_start, "s").astype("int64")
    epoch = base + day_offset * _SECONDS_PER_DAY + hour * 3600 + second
    return epoch.astype("datetime64[s]").astype("datetime64[us]")


def _day_weights(month_start: date, n_days: int, payday_phase: float = 0.0) -> np.ndarray:
    """Weekday seasonality for a month, optionally lifted around payday."""
    weekday = (np.datetime64(month_start, "D").astype(int) + np.arange(n_days)) % 7
    # numpy epoch 1970-01-01 was a Thursday; shift so Monday is 0.
    weekday = (weekday + 3) % 7
    w = _DOW_WEIGHT[weekday]
    if payday_phase:
        since = np.arange(n_days) % 14
        w = w * (1.0 + (_PAYDAY_LIFT - 1.0) * np.exp(-since / _PAYDAY_DECAY_DAYS))
    return w


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def month_windows(start: date, end: date) -> list[tuple[date, int]]:
    """Split the simulation window into ``(month_start, n_days)`` chunks."""
    out: list[tuple[date, int]] = []
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            nxt = date(cursor.year + 1, 1, 1)
        else:
            nxt = date(cursor.year, cursor.month + 1, 1)
        stop = min(nxt, date(end.year, end.month, end.day))
        days = (stop - cursor).days + (1 if stop == end else 0)
        if days <= 0:
            break
        out.append((cursor, days))
        cursor = nxt
    return out


@dataclass(slots=True)
class BehaviorModel:
    """Per-account parameters, derived once and reused across months."""

    cfg: RunConfig
    pop: Population

    # Retail
    retail_rows: np.ndarray = field(init=False)
    retail_income: np.ndarray = field(init=False)
    retail_arch: np.ndarray = field(init=False)
    payroll_phase: np.ndarray = field(init=False)
    merchant_sets: np.ndarray = field(init=False)
    merchant_set_offsets: np.ndarray = field(init=False)
    card_of_account: np.ndarray = field(init=False)

    # Business
    biz_rows: np.ndarray = field(init=False)
    biz_revenue: np.ndarray = field(init=False)
    biz_cash_ratio: np.ndarray = field(init=False)
    biz_arch: np.ndarray = field(init=False)


def _build_model(cfg: RunConfig, rng: StreamRegistry, pop: Population) -> BehaviorModel:
    m = BehaviorModel(cfg=cfg, pop=pop)
    is_checking = pop.account_type == "checking"

    m.retail_rows = np.flatnonzero(is_checking & ~pop.account_owner_is_entity)
    m.retail_income = pop.individual_income[pop.account_owner[m.retail_rows]]
    m.retail_arch = pop.account_archetype[m.retail_rows]

    r = rng.get("behavior", "phase")
    m.payroll_phase = r.integers(0, 28, len(m.retail_rows))

    m.biz_rows = np.flatnonzero(is_checking & pop.account_owner_is_entity)
    owner = pop.account_owner[m.biz_rows]
    m.biz_revenue = pop.entity_revenue[owner]
    m.biz_cash_ratio = pop.entity_cash_ratio[owner]
    m.biz_arch = pop.account_archetype[m.biz_rows]

    m.merchant_sets, m.merchant_set_offsets = _personal_merchant_sets(
        rng.get("behavior", "merchant_sets"), pop, len(m.retail_rows)
    )

    # Reverse index from account row to its debit card, for card transactions.
    card_of_account = np.full(len(pop.account_ids), -1, dtype=np.int64)
    card_of_account[pop.card_account] = np.arange(len(pop.card_account))
    m.card_of_account = card_of_account
    return m


def _personal_merchant_sets(
    rng: np.random.Generator, pop: Population, n_customers: int
) -> tuple[np.ndarray, np.ndarray]:
    """Draw each customer a personal merchant set from global popularity.

    Real people shop at the same two dozen places. Sampling every transaction
    from the global distribution instead would give every customer the same
    merchant profile, destroy the repeat-merchant signal, and make the
    account-merchant graph a near-complete bipartite blob rather than the
    heavy-tailed structure community detection can work on.
    """
    sizes = np.maximum(6, rng.poisson(24, n_customers))
    offsets = np.concatenate([[0], np.cumsum(sizes)])
    p = pop.merchant_weight / pop.merchant_weight.sum()
    picks = rng.choice(len(pop.merchant_ids), size=int(sizes.sum()), p=p)
    return picks, offsets


def simulate(cfg: RunConfig, rng: StreamRegistry, pop: Population, sink: TxnSink) -> int:
    """Generate the full background transaction stream into ``sink``.

    Each month is generated, assembled and handed to the sink before the next
    one starts, so peak memory is set by one month rather than by the window
    length. Accumulating the whole window first peaked at 15.6GB on the mvp
    preset - past physical RAM on a 16GB machine.

    Months are generated in order and every event falls inside its own month,
    so the concatenated output is chronological without a global sort.
    """
    model = _build_model(cfg, rng, pop)
    total = 0
    for month_index, (month_start, n_days) in enumerate(
        month_windows(cfg.window.start, cfg.window.end)
    ):
        buf = TxnBuffer()
        _retail_month(cfg, rng, pop, model, buf, month_start, n_days, month_index)
        _business_month(cfg, rng, pop, model, buf, month_start, n_days, month_index)
        data = buf.concat()
        if not data:
            continue
        txn, edges = _assemble(cfg, rng, pop, data, id_offset=total)
        sink(txn, edges)
        total += txn.height
    return total


def simulate_to_frames(
    cfg: RunConfig, rng: StreamRegistry, pop: Population
) -> tuple[pl.DataFrame, dict[str, pl.DataFrame]]:
    """Collect the whole stream in memory. For tests and small presets only."""
    txns: list[pl.DataFrame] = []
    edge_chunks: dict[str, list[pl.DataFrame]] = {}

    def sink(txn: pl.DataFrame, edges: dict[str, pl.DataFrame]) -> None:
        txns.append(txn)
        for name, df in edges.items():
            edge_chunks.setdefault(name, []).append(df)

    simulate(cfg, rng, pop, sink)
    if not txns:
        return pl.DataFrame(), {}
    return pl.concat(txns), {k: pl.concat(v) for k, v in edge_chunks.items()}


# ---------------------------------------------------------------------------
# Retail streams
# ---------------------------------------------------------------------------


def _retail_month(
    cfg: RunConfig,
    rng: StreamRegistry,
    pop: Population,
    m: BehaviorModel,
    buf: TxnBuffer,
    month_start: date,
    n_days: int,
    month_index: int,
) -> None:
    rows = m.retail_rows
    if len(rows) == 0:
        return
    arch = [RETAIL_ARCHETYPES[i] for i in range(len(RETAIL_ARCHETYPES))]
    monthly_income = m.retail_income / 12.0

    # --- payroll ---
    cadence = np.array([a.payroll_cadence for a in arch])[m.retail_arch]
    r = rng.get("behavior", "payroll")
    for cad, per_year in (("weekly", 52), ("biweekly", 26), ("monthly", 12)):
        sel = np.flatnonzero(cadence == cad)
        if len(sel) == 0:
            continue
        who, day = _periodic_days(cad, n_days, m.payroll_phase[sel], month_index)
        if len(who) == 0:
            continue
        gross = m.retail_income[sel][who] / per_year
        # Net pay varies slightly period to period (hours, deductions).
        amount = gross * r.normal(0.76, 0.03, len(who))
        buf.add(
            from_account=rows[sel][who],
            amount=np.maximum(amount, 1.0),
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="payroll",
            direction="credit",
        )

    # --- rent and utilities, monthly ---
    r = rng.get("behavior", "housing")
    who = np.arange(len(rows))
    day = (m.payroll_phase + 2) % min(28, n_days)
    rent = monthly_income * r.normal(0.30, 0.06, len(rows))
    buf.add(
        from_account=rows,
        amount=np.maximum(rent, 50.0),
        ts=_timestamps(r, month_start, day, business_hours=True),
        txn_class="rent",
        direction="debit",
    )
    util_day = (day + 7) % min(28, n_days)
    buf.add(
        from_account=rows,
        amount=np.maximum(r.lognormal(4.9, 0.45, len(rows)), 15.0),
        ts=_timestamps(r, month_start, util_day, business_hours=True),
        txn_class="utilities",
        direction="debit",
    )

    # --- discretionary card spend ---
    r = rng.get("behavior", "card")
    rate = np.array([a.card_txn_per_month for a in arch])[m.retail_arch] / 30.0
    weights = _day_weights(month_start, n_days, payday_phase=1.0)
    who, day = _poisson_events(r, rate, weights)
    if len(who):
        merchant = _pick_merchants(r, m, who)
        mcc_idx = pop.merchant_mcc_idx[merchant]
        mu = np.array([c.amount_mu for c in MERCHANT_CATEGORIES])[mcc_idx]
        sigma = np.array([c.amount_sigma for c in MERCHANT_CATEGORIES])[mcc_idx]
        scale = np.array([a.spend_scale for a in arch])[m.retail_arch][who]
        amount = r.lognormal(mu, sigma) * scale
        cnp = r.random(len(who)) < np.array([c.cnp_rate for c in MERCHANT_CATEGORIES])[mcc_idx]
        acct_rows = rows[who]
        for label, mask in (("retail_online", cnp), ("retail", ~cnp)):
            if not mask.any():
                continue
            buf.add(
                from_account=acct_rows[mask],
                amount=amount[mask],
                ts=_timestamps(r, month_start, day[mask]),
                txn_class=label,
                direction="debit",
                merchant=merchant[mask],
                card=m.card_of_account[acct_rows[mask]],
            )

    # --- cash withdrawals ---
    r = rng.get("behavior", "cash_retail")
    rate = np.array([a.cash_withdrawals_per_month for a in arch])[m.retail_arch] / 30.0
    who, day = _poisson_events(r, rate, _day_weights(month_start, n_days))
    if len(who):
        # ATMs dispense in $20 notes, so withdrawals are multiples of 20. This
        # matters: it puts a legitimate round-number mode in the cash channel,
        # which is where the structuring typology also lives.
        raw = r.lognormal(4.6, 0.7, len(who))
        amount = np.clip(np.round(raw / 20.0) * 20.0, 20.0, 800.0)
        buf.add(
            from_account=rows[who],
            amount=amount,
            ts=_timestamps(r, month_start, day),
            txn_class="cash_withdrawal",
            direction="debit",
        )

    # --- peer-to-peer between customers ---
    r = rng.get("behavior", "p2p")
    who, day = _poisson_events(r, np.full(len(rows), 1.6 / 30.0), _day_weights(month_start, n_days))
    if len(who):
        counterparty = rows[r.integers(0, len(rows), len(who))]
        keep = counterparty != rows[who]
        buf.add(
            from_account=rows[who][keep],
            to_account=counterparty[keep],
            amount=np.maximum(r.lognormal(3.6, 1.1, int(keep.sum())), 1.0),
            ts=_timestamps(r, month_start, day[keep]),
            txn_class="p2p_transfer",
            direction="debit",
        )

    # --- sweep to savings ---
    r = rng.get("behavior", "savings")
    savings_of_owner = _savings_lookup(pop)
    has_savings = savings_of_owner[pop.account_owner[rows]] >= 0
    sel = np.flatnonzero(has_savings)
    if len(sel):
        sweep_day = (m.payroll_phase[sel] + 3) % min(28, n_days)
        buf.add(
            from_account=rows[sel],
            to_account=savings_of_owner[pop.account_owner[rows[sel]]],
            amount=np.maximum(monthly_income[sel] * r.lognormal(-2.3, 0.8, len(sel)), 10.0),
            ts=_timestamps(r, month_start, sweep_day, business_hours=True),
            txn_class="internal_transfer",
            direction="debit",
        )


def _internal_counterparty(
    rng: np.random.Generator, rows: np.ndarray, who: np.ndarray, *, share: float
) -> np.ndarray:
    """Pick an in-bank counterparty for a share of events, -1 for the rest.

    A self-match falls back to external rather than being resampled: an account
    paying itself is not a transaction any real ledger contains, and it would
    show up as a self-loop that inflates every centrality measure computed over
    the payment graph. Resampling would bias the counterparty distribution
    slightly towards whoever is not the payer; dropping to external does not.
    """
    picked = np.full(len(who), -1, dtype=np.int64)
    internal = rng.random(len(who)) < share
    n = int(internal.sum())
    if n:
        candidate = rows[rng.integers(0, len(rows), n)]
        candidate[candidate == rows[who][internal]] = -1
        picked[internal] = candidate
    return picked


def _savings_lookup(pop: Population) -> np.ndarray:
    """Map individual index -> their savings account row, or -1."""
    out = np.full(len(pop.individual_income), -1, dtype=np.int64)
    sav = np.flatnonzero((pop.account_type == "savings") & ~pop.account_owner_is_entity)
    out[pop.account_owner[sav]] = sav
    return out


def _pick_merchants(rng: np.random.Generator, m: BehaviorModel, who: np.ndarray) -> np.ndarray:
    """Pick a merchant per event, mostly from the customer's personal set.

    A small share of spend goes outside the personal set — people do try new
    places — which keeps the merchant graph connected rather than fragmenting
    into disjoint per-customer stars.
    """
    sizes = np.diff(m.merchant_set_offsets)
    within = (rng.random(len(who)) * sizes[who]).astype(np.int64)
    picked = m.merchant_sets[m.merchant_set_offsets[who] + within]
    explore = rng.random(len(who)) < 0.12
    if explore.any():
        p = m.pop.merchant_weight / m.pop.merchant_weight.sum()
        picked = picked.copy()
        picked[explore] = rng.choice(len(m.pop.merchant_ids), int(explore.sum()), p=p)
    return picked


# ---------------------------------------------------------------------------
# Business streams
# ---------------------------------------------------------------------------


def _business_month(
    cfg: RunConfig,
    rng: StreamRegistry,
    pop: Population,
    m: BehaviorModel,
    buf: TxnBuffer,
    month_start: date,
    n_days: int,
    month_index: int,
) -> None:
    rows = m.biz_rows
    if len(rows) == 0:
        return
    arch = list(BUSINESS_ARCHETYPES)
    monthly_revenue = m.biz_revenue / 12.0
    weights = _day_weights(month_start, n_days)

    # --- customer receipts (non-cash) ---
    r = rng.get("behavior", "receipts")
    rate = np.array([a.receipts_per_month for a in arch])[m.biz_arch] / 30.0
    non_cash_share = 1.0 - m.biz_cash_ratio
    who, day = _poisson_events(r, rate * np.maximum(non_cash_share, 0.05), weights)
    if len(who):
        expected = monthly_revenue[who] * np.maximum(non_cash_share[who], 0.05)
        per_txn = expected / np.maximum(rate[who] * 30.0, 1.0)
        amount = np.maximum(per_txn * r.lognormal(-0.2, 0.7, len(who)), 5.0)
        # A share of receipts come from other customers of this bank, giving
        # the graph genuine business-to-business edges rather than everything
        # terminating at an external counterparty.
        counterparty = _internal_counterparty(r, rows, who, share=0.30)
        buf.add(
            from_account=rows[who],
            to_account=counterparty,
            amount=amount,
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="customer_receipt",
            direction="credit",
        )

    # --- cash deposits, driven by the industry's cash ratio ---
    r = rng.get("behavior", "cash_business")
    cash_rate = np.where(m.biz_cash_ratio > 0.01, 18.0, 0.0) / 30.0
    who, day = _poisson_events(r, cash_rate, weights)
    if len(who):
        expected = monthly_revenue[who] * m.biz_cash_ratio[who]
        per_deposit = expected / 18.0
        amount = np.maximum(per_deposit * r.lognormal(-0.15, 0.55, len(who)), 20.0)
        buf.add(
            from_account=rows[who],
            amount=amount,
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="cash_deposit",
            direction="credit",
        )

    # --- card settlement for merchant-facing businesses ---
    r = rng.get("behavior", "settlement")
    retail_like = np.isin(
        m.biz_arch,
        [i for i, a in enumerate(arch) if a.name in ("retail_smb", "professional_services")],
    )
    sel = np.flatnonzero(retail_like)
    if len(sel):
        # Daily batch settlement, the way an acquirer actually pays out.
        who = np.repeat(sel, n_days)
        day = np.tile(np.arange(n_days), len(sel))
        card_share = np.maximum(1.0 - m.biz_cash_ratio[who], 0.1)
        amount = np.maximum(
            (monthly_revenue[who] / n_days) * card_share * r.lognormal(-0.05, 0.35, len(who)), 5.0
        )
        buf.add(
            from_account=rows[who],
            amount=amount,
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="card_settlement",
            direction="credit",
        )

    # --- supplier payments ---
    r = rng.get("behavior", "suppliers")
    rate = np.array([a.supplier_payments_per_month for a in arch])[m.biz_arch] / 30.0
    who, day = _poisson_events(r, rate, weights)
    if len(who):
        amount = np.maximum(
            (monthly_revenue[who] * 0.55)
            / np.maximum(rate[who] * 30.0, 1.0)
            * r.lognormal(-0.1, 0.8, len(who)),
            10.0,
        )
        counterparty = _internal_counterparty(r, rows, who, share=0.35)
        buf.add(
            from_account=rows[who],
            to_account=counterparty,
            amount=amount,
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="supplier_payment",
            direction="debit",
        )

    # --- payroll out ---
    r = rng.get("behavior", "payroll_out")
    runs = np.array([a.runs_payroll for a in arch])[m.biz_arch]
    sel = np.flatnonzero(runs)
    if len(sel):
        phase = (sel * 7) % 14
        who, day = _periodic_days("biweekly", n_days, phase, month_index)
        if len(who):
            headcount = np.maximum(monthly_revenue[sel][who] / 9_000.0, 1.0)
            amount = headcount * r.lognormal(7.9, 0.35, len(who))
            buf.add(
                from_account=rows[sel][who],
                amount=amount,
                ts=_timestamps(r, month_start, day, business_hours=True),
                txn_class="payroll",
                direction="debit",
            )

    # --- cross-border wires ---
    r = rng.get("behavior", "wires")
    cross_rate = np.array([a.cross_border_rate for a in arch])[m.biz_arch]
    who, day = _poisson_events(r, cross_rate * 2.0 / 30.0, weights)
    if len(who):
        amount = np.maximum(monthly_revenue[who] * r.lognormal(-1.9, 0.9, len(who)), 250.0)
        buf.add(
            from_account=rows[who],
            amount=amount,
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="wire_out",
            direction="debit",
        )

    # --- quarterly tax ---
    if month_index % 3 == 2:
        r = rng.get("behavior", "tax")
        day = np.full(len(rows), min(14, n_days - 1))
        buf.add(
            from_account=rows,
            amount=np.maximum(monthly_revenue * 3 * 0.07 * r.lognormal(0, 0.3, len(rows)), 25.0),
            ts=_timestamps(r, month_start, day, business_hours=True),
            txn_class="tax_payment",
            direction="debit",
        )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _assemble(
    cfg: RunConfig,
    rng: StreamRegistry,
    pop: Population,
    data: dict[str, np.ndarray],
    *,
    id_offset: int = 0,
) -> tuple[pl.DataFrame, dict[str, pl.DataFrame]]:
    n = len(data["amount"])
    # Ids continue across months, so a streamed dataset has the same ids a
    # single-pass one would.
    txn_ids = np.char.add("TXN-", np.char.zfill((np.arange(n) + id_offset).astype("U12"), 12))

    # Expand the integer codes once, here. Indexing a small lookup array is a
    # single vectorized gather; a per-row dict lookup would be 30M Python calls
    # at the mvp preset.
    class_names = np.array(_CLASS_CODES)
    classes = class_names[data["txn_class"]]
    channels = np.array([TXN_CLASSES[c] for c in _CLASS_CODES])[data["txn_class"]]
    directions = np.array(_DIRECTION_CODES)[data["direction"]]

    r = rng.get("behavior", "assembly")
    ctr = _ctr_flags(cfg, pop, data, channels)

    txn = pl.DataFrame(
        {
            "txn_id": txn_ids,
            "amount": data["amount"],
            "currency": np.full(n, "USD"),
            "amount_usd": data["amount"],
            "booked_at": data["ts"],
            "value_date": data["ts"].astype("datetime64[D]"),
            "channel": channels,
            "direction": directions,
            "memo": _memos(classes, pop, data["merchant"]),
            "txn_class": classes,
            "ctr_reportable": ctr,
            # Declines are a real feature of card traffic and give the CNP
            # fraud typology (M3) somewhere to hide its card-testing bursts.
            "declined": (r.random(n) < 0.012) & np.isin(channels, ["card_present", "card_cnp"]),
        }
    )

    edges: dict[str, pl.DataFrame] = {}
    has_from = data["from_account"] >= 0
    edges["txn_from"] = pl.DataFrame(
        {
            "start_id": txn_ids[has_from],
            "end_id": pop.account_ids[data["from_account"][has_from]],
        }
    )
    has_to = data["to_account"] >= 0
    edges["txn_to"] = pl.DataFrame(
        {"start_id": txn_ids[has_to], "end_id": pop.account_ids[data["to_account"][has_to]]}
    )
    has_merchant = data["merchant"] >= 0
    edges["txn_at_merchant"] = pl.DataFrame(
        {
            "start_id": txn_ids[has_merchant],
            "end_id": pop.merchant_ids[data["merchant"][has_merchant]],
        }
    )
    has_card = data["card"] >= 0
    edges["txn_on_card"] = pl.DataFrame(
        {"start_id": txn_ids[has_card], "end_id": pop.card_ids[data["card"][has_card]]}
    )
    edges["txn_via_device"], edges["txn_via_ip"] = _sessions(
        rng.get("behavior", "session"), pop, data, txn_ids, channels
    )
    return txn, edges


def _ctr_flags(
    cfg: RunConfig, pop: Population, data: dict[str, np.ndarray], channels: np.ndarray
) -> np.ndarray:
    """Apply the institution's CTR rule, with same-day aggregation.

    31 CFR 1010.313 aggregates multiple same-day cash transactions by or for
    the same person before the threshold is applied. Implementing the
    per-transaction test alone would let a structuring generator evade a
    control the real institution does not actually have, which would make the
    typology easier than reality.
    """
    controls = cfg.institution["controls"]
    threshold = float(controls["ctr_threshold_usd"])
    is_cash = channels == "cash"
    flags = np.zeros(len(channels), dtype=bool)
    if not is_cash.any():
        return flags

    idx = np.flatnonzero(is_cash)
    owner = pop.account_owner[data["from_account"][idx]]
    # Owner id has to be namespaced by kind, or individual 7 and entity 7
    # aggregate together.
    owner_key = np.where(
        pop.account_owner_is_entity[data["from_account"][idx]], owner + 10**9, owner
    )
    day = data["ts"][idx].astype("datetime64[D]").astype("int64")

    agg = (
        pl.DataFrame({"owner": owner_key, "day": day, "amount": data["amount"][idx]})
        .group_by(["owner", "day"])
        .agg(pl.col("amount").sum().alias("total"))
    )
    lookup = agg.filter(pl.col("total") > threshold)
    if lookup.height:
        reportable = set(zip(lookup["owner"].to_list(), lookup["day"].to_list(), strict=True))
        flags[idx] = [
            (o, d) in reportable for o, d in zip(owner_key.tolist(), day.tolist(), strict=True)
        ]
    return flags


def _memos(txn_class: np.ndarray, pop: Population, merchant: np.ndarray) -> np.ndarray:
    """Payment references.

    Card transactions carry the merchant name the way a real statement line
    does; everything else gets a class-appropriate reference.
    """
    templates = {
        "payroll": "PAYROLL",
        "rent": "RENT PAYMENT",
        "utilities": "UTILITY BILL",
        "subscription": "SUBSCRIPTION",
        "cash_withdrawal": "ATM WITHDRAWAL",
        "cash_deposit": "CASH DEPOSIT",
        "p2p_transfer": "TRANSFER",
        "internal_transfer": "TRANSFER TO SAVINGS",
        "supplier_payment": "SUPPLIER PAYMENT",
        "customer_receipt": "CUSTOMER PAYMENT",
        "card_settlement": "CARD SETTLEMENT",
        "wire_out": "INTERNATIONAL WIRE",
        "tax_payment": "TAX PAYMENT",
    }
    # Built per distinct class rather than per row: at the mvp preset this is
    # 30M dict lookups otherwise.
    out = np.empty(len(txn_class), dtype=object)
    for cls in np.unique(txn_class):
        out[txn_class == cls] = templates.get(cls, cls.upper())
    card_like = np.isin(txn_class, ["retail", "retail_online"])
    if card_like.any():
        names = pop.tables["merchant"]["name"].to_numpy()
        out[card_like] = np.char.upper(names[merchant[card_like]].astype(str))
    return out.astype(str)


def _sessions(
    rng: np.random.Generator,
    pop: Population,
    data: dict[str, np.ndarray],
    txn_ids: np.ndarray,
    channels: np.ndarray,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Attach device and IP to the channels that have a session.

    Cash and card-present transactions happen at a terminal, not in an app, so
    they carry no device — attaching one to everything would hand detectors a
    device signal on transactions that in reality have none.
    """
    online = np.isin(channels, ["card_cnp", "p2p", "internal", "ach", "wire"])
    idx = np.flatnonzero(online & (data["from_account"] >= 0))
    if len(idx) == 0:
        empty = pl.DataFrame({"start_id": [], "end_id": []})
        return empty, empty

    owner = pop.account_owner[data["from_account"][idx]]
    is_entity = pop.account_owner_is_entity[data["from_account"][idx]]
    device = np.where(
        is_entity,
        rng.integers(0, len(pop.device_ids), len(idx)),
        pop.individual_device[np.clip(owner, 0, len(pop.individual_device) - 1)],
    )
    # Customers mostly reconnect from the same IP; a minority roam.
    home_ip = owner % len(pop.ip_ids)
    roam = rng.random(len(idx)) < 0.15
    ip = np.where(roam, rng.integers(0, len(pop.ip_ids), len(idx)), home_ip)

    return (
        pl.DataFrame({"start_id": txn_ids[idx], "end_id": pop.device_ids[device]}),
        pl.DataFrame({"start_id": txn_ids[idx], "end_id": pop.ip_ids[ip]}),
    )
