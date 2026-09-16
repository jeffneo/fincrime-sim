"""The institution's control environment (spec §5.5).

Two things live here, and keeping them together is the point:

* **Controls the institution applies during processing** — CTR determination,
  KYC tier limits. These write into the dataset itself.
* **The monitoring rules the institution's own alerting runs** — the
  "bank-style rules baseline" the M5 detectability gate scores against
  (PHASE1-PLAN.md §7).

A typology is defined relative to the controls it evades: structuring is only
structuring because a CTR threshold exists at USD 10,000 and is applied to
same-day aggregates. So the generators and the rules must read the same
constants from ``config/institution.yaml`` — if the generator evades a
threshold the institution does not actually enforce, the typology is easier
than reality, and the detectability number measured at M5 is meaningless.

The rules here are deliberately the crude, high-false-positive kind a real AML
team actually runs, not a best-effort detector. They are the baseline a good
detector has to beat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl


@dataclass(frozen=True, slots=True)
class Controls:
    """Resolved control constants for one institution."""

    ctr_threshold_usd: float
    ctr_aggregation_window_hours: int
    travel_rule_threshold_usd: float
    alert_budget_pct: float
    monitoring: dict[str, Any]

    @classmethod
    def from_config(cls, institution: dict[str, Any]) -> Controls:
        c = institution["controls"]
        return cls(
            ctr_threshold_usd=float(c["ctr_threshold_usd"]),
            ctr_aggregation_window_hours=int(c["ctr_aggregation_window_hours"]),
            travel_rule_threshold_usd=float(c["travel_rule_threshold_usd"]),
            alert_budget_pct=float(c["alert_budget_pct"]),
            monitoring=dict(c["monitoring"]),
        )


# ---------------------------------------------------------------------------
# Controls applied during processing
# ---------------------------------------------------------------------------


def ctr_reportable(
    controls: Controls,
    *,
    owner_key: np.ndarray,
    day: np.ndarray,
    amount_usd: np.ndarray,
    is_cash: np.ndarray,
) -> np.ndarray:
    """Apply 31 CFR 1010.311/1010.313 to a batch of transactions.

    Cash transactions are aggregated per customer per day before the threshold
    is applied, because that is what the regulation requires and therefore what
    a structuring scheme has to work around. Testing each transaction on its own
    would let the generator evade a control the institution does not have, and
    would make T1 detectable by a rule no real bank could rely on.

    ``owner_key`` must already be namespaced across owner kinds, or individual 7
    and entity 7 aggregate together.
    """
    flags = np.zeros(len(amount_usd), dtype=bool)
    idx = np.flatnonzero(is_cash)
    if idx.size == 0:
        return flags

    agg = (
        pl.DataFrame({"owner": owner_key[idx], "day": day[idx], "amount": amount_usd[idx]})
        .group_by(["owner", "day"])
        .agg(pl.col("amount").sum().alias("total"))
        .filter(pl.col("total") > controls.ctr_threshold_usd)
    )
    if agg.height == 0:
        return flags

    reportable = set(zip(agg["owner"].to_list(), agg["day"].to_list(), strict=True))
    flags[idx] = [
        (o, d) in reportable
        for o, d in zip(owner_key[idx].tolist(), day[idx].tolist(), strict=True)
    ]
    return flags


# ---------------------------------------------------------------------------
# Monitoring rules - the baseline detector
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """One monitoring rule, as a bank would write it."""

    name: str
    description: str
    #: Why a real institution runs this, and what it will inevitably also hit.
    rationale: str


#: Cap on any single rule's contribution to alert severity, so one extreme
#: exceedance cannot monopolize the queue on its own.
SEVERITY_CAP = 3.0

RULES: tuple[Rule, ...] = (
    Rule(
        "cash_structuring_aggregate",
        "Cash credits in the lookback window sum above the aggregate threshold "
        "while every individual deposit stays under the CTR threshold.",
        "The canonical structuring rule. It also fires on every legitimate "
        "cash-intensive business, which is precisely why the hard negatives "
        "matter - without them this rule looks far better than it is.",
    ),
    Rule(
        "sub_threshold_clustering",
        "An unusual share of a customer's cash deposits land just below the CTR threshold.",
        "Proximity to the threshold rather than volume. Catches careful "
        "structuring that stays under the aggregate rule, and misfires on "
        "businesses whose natural daily takings sit near the threshold.",
    ),
    Rule(
        "rapid_pass_through",
        "Funds in and out within the movement window, with outflow close to "
        "inflow and little retained.",
        "Layering and mule behaviour. Also describes payment processors, "
        "payroll bureaux and treasury sweep accounts.",
    ),
    Rule(
        "new_account_high_velocity",
        "Account opened recently and already transacting at high volume.",
        "Mule accounts are recruited and used quickly. So are the accounts of "
        "customers who just switched banks.",
    ),
    Rule(
        "high_risk_jurisdiction_wire",
        "Wire above the threshold to a jurisdiction rated high risk.",
        "Sanctions and layering exposure. Fires on any customer with genuine "
        "business in those markets.",
    ),
    Rule(
        "peer_transfer_funnel",
        "A personal account receiving peer transfers above a modest threshold "
        "in the lookback window and passing most of the value straight on.",
        "Rapid movement of funds through personal accounts - a standard mule "
        "rule, and deliberately set at retail scale. The commercial "
        "pass-through rule carries a six-figure throughput floor to stop it "
        "firing on everyone, which makes it blind to a mule moving a few "
        "thousand pounds at a time. It also fires on anyone splitting rent or "
        "settling up after a holiday.",
    ),
    Rule(
        "card_velocity_burst",
        "A single card exceeding the per-hour velocity limit.",
        "Card testing and stolen-PAN bursts. Measured per card, which is how "
        "an issuer monitors: the cards in one fraud batch belong to unrelated "
        "customers, so a per-customer velocity rule cannot see the batch at "
        "all. Also fires on a customer working through a checkout.",
    ),
    Rule(
        "cnp_amount_anomaly",
        "A card-not-present charge far above that card's own usual ticket.",
        "Amount relative to the card's history rather than an absolute floor, "
        "because a large charge is only odd for a cardholder who never makes "
        "them. Fires on anyone buying a flight for the first time.",
    ),
)


def _rolling_cash_peaks(base: pl.LazyFrame, threshold: float, lookback_days: int) -> pl.DataFrame:
    """Peak cash activity in any rolling window, per customer.

    This is how a monitoring system actually evaluates a lookback rule: it runs
    daily and asks what the customer did in the preceding N days. The peak
    across all such windows is what would have alerted at some point during the
    period.

    Returns the largest windowed cash-credit total and the largest count of
    near-threshold deposits in any one window.
    """
    cash = (
        base.filter((pl.col("channel") == "cash") & (pl.col("direction") == "credit"))
        .select("owner_id", "booked_at", "amount_usd")
        .sort("booked_at")
        .collect()
    )
    if cash.height == 0:
        return pl.DataFrame(
            schema={
                "owner_id": pl.String,
                "peak_cash_window_usd": pl.Float64,
                "peak_near_threshold_count": pl.UInt32,
            }
        )

    near = (pl.col("amount_usd") >= threshold * 0.6) & (pl.col("amount_usd") < threshold)
    return (
        cash.rolling(index_column="booked_at", period=f"{lookback_days}d", group_by="owner_id")
        .agg(
            pl.col("amount_usd").sum().alias("window_usd"),
            pl.col("amount_usd").filter(near).count().alias("window_near"),
        )
        .group_by("owner_id")
        .agg(
            pl.col("window_usd").max().alias("peak_cash_window_usd"),
            pl.col("window_near").max().alias("peak_near_threshold_count"),
        )
    )


def _rolling_peer_peaks(base: pl.LazyFrame, lookback_days: int) -> pl.DataFrame:
    """Peak peer-transfer credits in any rolling window, per customer.

    Same reasoning as the cash version: mule activity is a burst, and a
    whole-window total averages it into invisibility.
    """
    peer = (
        base.filter(pl.col("txn_class") == "p2p_transfer")
        .select("owner_id", "booked_at", "amount_usd", "direction")
        .sort("booked_at")
        .collect()
    )
    if peer.height == 0:
        return pl.DataFrame(
            schema={
                "owner_id": pl.String,
                "peak_peer_in_usd": pl.Float64,
                "peak_peer_in_count": pl.UInt32,
                "peer_funnel_ratio": pl.Float64,
            }
        )
    credit = pl.col("direction") == "credit"
    return (
        peer.rolling(index_column="booked_at", period=f"{lookback_days}d", group_by="owner_id")
        .agg(
            pl.col("amount_usd").filter(credit).sum().alias("window_in"),
            pl.col("amount_usd").filter(~credit).sum().alias("window_out"),
            pl.col("amount_usd").filter(credit).count().alias("window_n"),
        )
        .group_by("owner_id")
        .agg(
            pl.col("window_in").max().alias("peak_peer_in_usd"),
            pl.col("window_n").max().alias("peak_peer_in_count"),
            # Peer money out against peer money in, inside the same window.
            # Comparing peer inflow to the account's TOTAL outflow instead
            # matched nothing: a mule also draws a salary and pays rent, so the
            # whole-account ratio is dominated by ordinary life and the funnel
            # disappears into it. Fired on 0 of 12 mules before this.
            (
                pl.col("window_out").max()
                / pl.max_horizontal(pl.col("window_in").max(), pl.lit(1.0))
            ).alias("peer_funnel_ratio"),
        )
    )


def run_rules(
    controls: Controls,
    *,
    txn: pl.LazyFrame,
    txn_from: pl.LazyFrame,
    accounts: pl.DataFrame,
    account_owner: pl.DataFrame,
    card_account: pl.DataFrame,
) -> pl.DataFrame:
    """Score every customer against the monitoring rules.

    Returns one row per customer with a column per rule and a ``rule_score``
    total. Scoring per customer rather than per transaction is deliberate: an
    AML team works customer-level alerts, so recall at a fixed alert budget
    only means anything if the budget is counted in the same unit.
    """
    m = controls.monitoring

    # Customer-level transaction view. Joined once and reused - at the mvp
    # preset this is 55M rows, and each rule re-joining it would dominate.
    base = (
        txn.join(txn_from.rename({"end_id": "account_id"}), left_on="txn_id", right_on="start_id")
        .join(account_owner.lazy(), on="account_id")
        .select(
            "owner_id",
            "account_id",
            "amount_usd",
            "channel",
            "direction",
            "txn_class",
            "booked_at",
        )
    )

    threshold = controls.ctr_threshold_usd
    cash_lookback = int(m["cash_structuring_lookback_days"])

    per_customer = (
        base.group_by("owner_id")
        .agg(
            pl.len().alias("txn_count"),
            pl.col("amount_usd").sum().alias("total_usd"),
            # Cash credits: the structuring surface.
            pl.col("amount_usd")
            .filter((pl.col("channel") == "cash") & (pl.col("direction") == "credit"))
            .sum()
            .alias("cash_in_usd"),
            pl.col("amount_usd")
            .filter((pl.col("channel") == "cash") & (pl.col("direction") == "credit"))
            .count()
            .alias("cash_in_count"),
            pl.col("amount_usd").filter(pl.col("direction") == "credit").sum().alias("inflow_usd"),
            pl.col("amount_usd").filter(pl.col("direction") == "debit").sum().alias("outflow_usd"),
            pl.col("amount_usd")
            .filter((pl.col("channel") == "wire") & (pl.col("direction") == "debit"))
            .max()
            .alias("max_wire_out_usd"),
            pl.col("booked_at").min().alias("first_seen"),
            pl.col("booked_at").max().alias("last_seen"),
        )
        .collect()
    )

    owner_open = (
        account_owner.join(accounts.select("account_id", "opened_on"), on="account_id")
        .group_by("owner_id")
        .agg(pl.col("opened_on").min().alias("opened_on"))
    )
    per_customer = per_customer.join(owner_open, on="owner_id", how="left")

    per_customer = (
        per_customer.join(
            _rolling_cash_peaks(base, threshold, cash_lookback), on="owner_id", how="left"
        )
        .join(_rolling_peer_peaks(base, cash_lookback), on="owner_id", how="left")
        .with_columns(
            pl.col("peak_cash_window_usd").fill_null(0.0),
            pl.col("peak_near_threshold_count").fill_null(0),
            pl.col("peak_peer_in_usd").fill_null(0.0),
            pl.col("peak_peer_in_count").fill_null(0),
            pl.col("peer_funnel_ratio").fill_null(0.0),
        )
    )

    scored = per_customer.with_columns(
        # The canonical structuring rule, over a real rolling window.
        #
        # An earlier version approximated this by scaling a customer's
        # whole-window cash total by lookback/active_days. That silently
        # destroys the signal: structuring is a concentrated burst, and
        # averaging it across a year of ordinary activity turned a USD 42,000
        # placement into USD 3,771 and dropped recall to zero. The averaging
        # is not a shortcut for a rolling window, it is the opposite of one.
        (
            (pl.col("peak_cash_window_usd") >= float(m["cash_structuring_aggregate_usd"]))
            & (pl.col("cash_in_count") >= 3)
        ).alias("cash_structuring_aggregate"),
        (pl.col("peak_near_threshold_count") >= 3).alias("sub_threshold_clustering"),
        (
            (pl.col("peak_peer_in_usd") >= float(m["peer_funnel_min_usd"]))
            & (pl.col("peak_peer_in_count") >= 3)
            & (pl.col("peer_funnel_ratio") >= float(m["peer_funnel_out_ratio"]))
        ).alias("peer_transfer_funnel"),
        # Pass-through needs a throughput floor as well as a ratio. Most people
        # spend roughly what they earn, so the ratio alone fired on 79% of all
        # customers - not a rule any institution would run, and it made the
        # alert queue pure noise.
        (
            (
                pl.col("outflow_usd")
                >= float(m["rapid_movement_pass_through_ratio"]) * pl.col("inflow_usd")
            )
            & (pl.col("inflow_usd") >= float(m["rapid_movement_min_inflow_usd"]))
            & (pl.col("txn_count") >= 5)
        ).alias("rapid_pass_through"),
        (
            (
                (pl.col("first_seen").dt.date() - pl.col("opened_on")).dt.total_days()
                <= int(m["new_account_high_velocity_days"])
            )
            & (pl.col("txn_count") >= 40)
        ).alias("new_account_high_velocity"),
        (
            pl.col("max_wire_out_usd").fill_null(0.0)
            >= float(m["wire_to_high_risk_jurisdiction_usd"])
        ).alias("high_risk_jurisdiction_wire"),
    )

    card = (
        base.filter(pl.col("channel").is_in(["card_present", "card_cnp"]))
        .join(card_account.lazy(), on="account_id", how="inner")
        .select("owner_id", "card_id", "amount_usd", "channel", "booked_at")
        .collect()
    )
    if card.height:
        velocity = (
            card.with_columns(pl.col("booked_at").dt.truncate("1h").alias("hour"))
            .group_by(["owner_id", "card_id", "hour"])
            .len()
            .group_by("owner_id")
            .agg(pl.col("len").max().alias("peak_card_per_hour"))
        )
        # Largest CNP charge as a multiple of that card's own median ticket.
        anomaly = (
            card.with_columns(pl.col("amount_usd").median().over("card_id").alias("card_median"))
            .filter(pl.col("channel") == "card_cnp")
            .with_columns(
                (
                    pl.col("amount_usd") / pl.max_horizontal(pl.col("card_median"), pl.lit(1.0))
                ).alias("ratio")
            )
            .group_by("owner_id")
            .agg(pl.col("ratio").max().alias("peak_cnp_ratio"))
        )
        scored = scored.join(velocity, on="owner_id", how="left").join(
            anomaly, on="owner_id", how="left"
        )
    else:
        scored = scored.with_columns(
            pl.lit(0).alias("peak_card_per_hour"), pl.lit(0.0).alias("peak_cnp_ratio")
        )

    scored = scored.with_columns(
        (pl.col("peak_card_per_hour").fill_null(0) > int(m["card_velocity_txn_per_hour"])).alias(
            "card_velocity_burst"
        ),
        (pl.col("peak_cnp_ratio").fill_null(0.0) >= float(m["cnp_amount_ratio"])).alias(
            "cnp_amount_anomaly"
        ),
    )

    rule_names = [r.name for r in RULES]

    # Alert severity, not a rule count.
    #
    # Ranking by how many rules fired and breaking ties on raw volume put every
    # high-turnover business ahead of a retail customer who placed USD 42,000
    # in cash - 64 of 112 structuring positives landed on the same rule count
    # as 1,027 other customers, against a 1,000-alert budget, and recall came
    # out at 0.018. A risk engine scores by how far each rule was exceeded, so
    # each fired rule contributes its own normalized exceedance, capped so one
    # extreme rule cannot swamp the rest.
    def _severity(fired: str, observed: pl.Expr, threshold: float) -> pl.Expr:
        # Log-scaled exceedance: a wire 25x over the reporting threshold is not
        # 25 times more suspicious than one just over it, and on a linear scale
        # a single extreme rule swamps every other signal a customer shows.
        # Doubling the exceedance adds one point, capped at SEVERITY_CAP.
        ratio = (observed / pl.lit(threshold)).clip(lower_bound=1.0)
        return (
            pl.when(pl.col(fired))
            .then((1.0 + ratio.log(2)).clip(upper_bound=SEVERITY_CAP))
            .otherwise(0.0)
        )

    scored = scored.with_columns(
        pl.sum_horizontal(
            _severity(
                "cash_structuring_aggregate",
                pl.col("peak_cash_window_usd"),
                float(m["cash_structuring_aggregate_usd"]),
            ),
            _severity("sub_threshold_clustering", pl.col("peak_near_threshold_count"), 3.0),
            _severity(
                "rapid_pass_through", pl.col("outflow_usd") / (pl.col("inflow_usd") + 1.0), 1.0
            ),
            _severity(
                "peer_transfer_funnel",
                pl.col("peak_peer_in_usd"),
                float(m["peer_funnel_min_usd"]),
            ),
            _severity("new_account_high_velocity", pl.col("txn_count"), 40.0),
            _severity(
                "high_risk_jurisdiction_wire",
                pl.col("max_wire_out_usd").fill_null(0.0),
                float(m["wire_to_high_risk_jurisdiction_usd"]),
            ),
            _severity(
                "card_velocity_burst",
                pl.col("peak_card_per_hour").fill_null(0),
                float(m["card_velocity_txn_per_hour"]),
            ),
            _severity(
                "cnp_amount_anomaly",
                pl.col("peak_cnp_ratio").fill_null(0.0),
                float(m["cnp_amount_ratio"]),
            ),
        ).alias("alert_severity"),
        pl.sum_horizontal([pl.col(n).cast(pl.Int32) for n in rule_names]).alias("rule_score"),
    )
    return scored.select(["owner_id", "rule_score", "alert_severity", *rule_names])
