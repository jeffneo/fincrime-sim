"""Detectability calibration (spec §9.2, PHASE1-PLAN.md §7).

The Phase 1 acceptance gate. A dataset whose typologies a baseline detector
finds perfectly is useless — it proves only that the injection is crude. One
where nothing is findable is equally useless. This measures where the dataset
actually sits.

Two baselines, because they fail differently:

* **Rules** — the crude thresholds a real AML team runs (``institution.py``).
  Reported as recall at a fixed alert budget, because an AML team can only work
  so many alerts, and recall without a budget is a number you can always make
  larger by alerting on everyone.
* **Gradient boosting** — a competent tabular model on hand-engineered
  customer features. Reported as AUC-PR, not AUC-ROC: at a 0.4% base rate ROC
  is dominated by true negatives and will look impressive for a model that is
  useless in practice.
* **The same model with graph features added** (``graph_features``). Reported
  alongside the tabular number rather than replacing it, because the gap
  between the two is the dataset's actual argument. Layering is a property of a
  path and card fraud a burst on one card; a year of per-customer averages
  cannot see either, which is why both sat under the 0.10 floor with AUC-PR
  0.013 and 0.008. Folding topology into the one baseline would have hidden
  that by making the baseline stronger - the interesting result is not a
  passing number, it is which typologies need the graph to be visible at all.

Features are computed only from the business graph. Ground truth is joined in
at the very end, purely to produce `y`. That ordering is the whole anti-leakage
design — there is no point at which a label is available to a feature.

This runs at M2 against one typology on purpose. Calibration is the schedule
risk, not the typology code: discovering at M5 that four typologies need
re-tuning is much worse than discovering it now for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from ..institution import Controls, run_rules
from .stats import Check

#: Below this many positives, recall is too coarsely quantized for the
#: acceptance band to mean anything (1/50 = 2 percentage points per entity).
#: Realistic prevalence (0.4% of entities) means the dev preset cannot reach
#: it - which is the honest answer, not a reason to inflate the base rate.
MIN_POSITIVES_FOR_CALIBRATION = 50

#: Below this many illicit rings, easy-vs-hard tier recall is not a difficulty
#: measurement. Tier recall is per entity, but every member of a ring inherits
#: that ring's tier, so the independent sample size is the RING count - and at
#: the mvp preset mule_network had five rings across three tiers while
#: reporting recall over ninety entities.
MIN_RINGS_FOR_TIER_SEPARATION = 9


#: Alert budgets to report recall at, as a percentage of customers. A single
#: operating point hides whether a miss means "unfindable" or "just outside the
#: queue" - here, structuring positives ranked from 1,145 with a 1,000-alert
#: budget, which the 1% number alone reported as a flat zero.
REPORTED_BUDGETS_PCT = (1.0, 5.0, 10.0)


@dataclass(frozen=True, slots=True)
class TypologyScore:
    typology: str
    positives: int
    hard_negatives: int
    rules_recall_at_budget: float
    recall_curve: dict[float, float]
    hard_negative_alert_share: float
    tier_recall: dict[str, float]
    gbm_auc_pr: float
    #: The same model, same folds, with `graph_features` joined on. Reported
    #: beside the tabular number: the gap is what the dataset is for.
    gbm_graph_auc_pr: float
    gbm_baseline_auc_pr: float
    #: Illicit rings behind `positives`. Tier recall is measured per entity,
    #: but a ring's members all share its tier, so a handful of rings makes the
    #: tier comparison ring-to-ring variance rather than a difficulty effect.
    rings: int = 0

    @property
    def lift(self) -> float:
        """Best AUC-PR over the no-skill baseline (the positive rate)."""
        if self.gbm_baseline_auc_pr <= 0:
            return float("nan")
        return max(self.gbm_auc_pr, self.gbm_graph_auc_pr) / self.gbm_baseline_auc_pr


def customer_features(dataset_dir: Path) -> pl.DataFrame:
    """Per-customer TABULAR features, computed from the business graph only.

    Deliberately the features a competent analyst would build from what a bank
    can see: volumes and counts by channel and direction, timing dispersion,
    counterparty diversity, account age, cash behaviour relative to the
    reporting threshold.

    No topology here, on purpose - this is the set that stands in for "what a
    tabular model can do", and it stays that way so the graph-feature
    comparison means something. ``graph_features`` is the other half.
    """
    graph = dataset_dir / "graph"
    owner = _account_owner(dataset_dir)

    base = (
        pl.scan_parquet(graph / "transaction.parquet")
        .join(
            pl.scan_parquet(graph / "txn_from.parquet").rename({"end_id": "account_id"}),
            left_on="txn_id",
            right_on="start_id",
        )
        .join(owner.lazy(), on="account_id")
    )

    cash_in = (pl.col("channel") == "cash") & (pl.col("direction") == "credit")
    peer = pl.col("txn_class") == "p2p_transfer"
    peer_in = peer & (pl.col("direction") == "credit")
    peer_out = peer & (pl.col("direction") == "debit")
    return (
        base.group_by("owner_id")
        .agg(
            pl.len().alias("txn_count"),
            pl.col("amount_usd").sum().alias("total_usd"),
            pl.col("amount_usd").mean().alias("mean_usd"),
            pl.col("amount_usd").std().fill_null(0.0).alias("sd_usd"),
            pl.col("amount_usd").max().alias("max_usd"),
            pl.col("amount_usd").filter(pl.col("direction") == "credit").sum().alias("inflow_usd"),
            pl.col("amount_usd").filter(pl.col("direction") == "debit").sum().alias("outflow_usd"),
            pl.col("amount_usd").filter(cash_in).sum().alias("cash_in_usd"),
            pl.col("amount_usd").filter(cash_in).count().alias("cash_in_count"),
            pl.col("amount_usd").filter(cash_in).mean().fill_null(0.0).alias("cash_in_mean"),
            pl.col("amount_usd").filter(cash_in).std().fill_null(0.0).alias("cash_in_sd"),
            # Distance from the reporting threshold. Structuring lives here, and
            # so does a cash business whose daily takings are simply large.
            pl.col("amount_usd")
            .filter(cash_in & (pl.col("amount_usd") >= 6_000) & (pl.col("amount_usd") < 10_000))
            .count()
            .alias("near_threshold_count"),
            pl.col("channel").n_unique().alias("channel_variety"),
            pl.col("txn_class").n_unique().alias("class_variety"),
            pl.col("account_id").n_unique().alias("account_count"),
            pl.col("booked_at").min().alias("first_seen"),
            pl.col("booked_at").max().alias("last_seen"),
            pl.col("booked_at").dt.hour().mean().alias("mean_hour"),
            pl.col("booked_at").dt.hour().std().fill_null(0.0).alias("sd_hour"),
            # Burst and concentration features. Without these the feature set
            # is a year of averages, in which a fifteen-transaction card burst
            # or a six-week mule funnel simply does not appear - the set was
            # built for structuring, whose signal survives averaging, and never
            # extended when the other three typologies arrived.
            pl.col("amount_usd").filter(peer_in).sum().alias("peer_in_usd"),
            pl.col("amount_usd").filter(peer_out).sum().alias("peer_out_usd"),
            pl.col("amount_usd")
            .filter(pl.col("channel") == "card_cnp")
            .max()
            .fill_null(0.0)
            .alias("max_cnp_usd"),
            pl.col("amount_usd").filter(pl.col("channel") == "card_cnp").count().alias("cnp_count"),
            pl.col("amount_usd").filter(pl.col("channel") == "wire").sum().alias("wire_usd"),
            pl.col("booked_at").diff().min().dt.total_seconds().alias("min_gap_s"),
            pl.col("booked_at").dt.date().n_unique().alias("active_dates"),
        )
        .with_columns(
            (pl.col("outflow_usd") / (pl.col("inflow_usd") + 1.0)).alias("pass_through_ratio"),
            (pl.col("cash_in_usd") / (pl.col("inflow_usd") + 1.0)).alias("cash_share"),
            (pl.col("near_threshold_count") / (pl.col("cash_in_count") + 1)).alias(
                "near_threshold_share"
            ),
            (pl.col("sd_usd") / (pl.col("mean_usd") + 1.0)).alias("amount_cv"),
            (pl.col("peer_out_usd") / (pl.col("peer_in_usd") + 1.0)).alias("peer_funnel_ratio"),
            (pl.col("max_cnp_usd") / (pl.col("mean_usd") + 1.0)).alias("cnp_amount_ratio"),
            (pl.col("txn_count") / pl.max_horizontal(pl.col("active_dates"), pl.lit(1))).alias(
                "txns_per_active_day"
            ),
            (pl.col("last_seen") - pl.col("first_seen")).dt.total_days().alias("active_days"),
        )
        .drop("first_seen", "last_seen")
        .collect()
    )


def graph_features(dataset_dir: Path) -> pl.DataFrame:
    """Per-customer features that need the graph, not just the ledger.

    Every one of these requires joining a customer's transactions to something
    outside their own account history - a shared device, a counterparty's
    behaviour, a path two hops out. None can be derived from the per-customer
    aggregates in ``customer_features``, which is the point: they target the
    two typologies a year of averages cannot see.

    Ground truth is not read here, exactly as in ``customer_features``.
    """
    graph = dataset_dir / "graph"
    owner = _account_owner(dataset_dir).lazy()

    txn = pl.scan_parquet(graph / "transaction.parquet").select(
        "txn_id", "amount_usd", "booked_at", "channel", "direction", "txn_class"
    )
    frm = pl.scan_parquet(graph / "txn_from.parquet").rename(
        {"start_id": "txn_id", "end_id": "from_account"}
    )
    to = pl.scan_parquet(graph / "txn_to.parquet").rename(
        {"start_id": "txn_id", "end_id": "to_account"}
    )

    # --- shared infrastructure -------------------------------------------
    #
    # Device and IP degree. A mule ring's members are connected to each other
    # ONLY through shared infrastructure - the transactions themselves go to
    # unrelated counterparties - so this is the one feature that sees the ring
    # as a ring. It necessarily also fires on households, which is why the
    # hard negatives matter.
    def _shared(edge: str, end: str) -> pl.LazyFrame:
        link = pl.scan_parquet(graph / edge).rename({"start_id": "txn_id", "end_id": end})
        per_node = (
            link.join(frm, on="txn_id")
            .join(owner, left_on="from_account", right_on="account_id")
            .group_by(end)
            .agg(
                pl.col("from_account").n_unique().alias("accounts"),
                pl.col("owner_id").n_unique().alias("owners"),
            )
        )
        return (
            link.join(frm, on="txn_id")
            .join(owner, left_on="from_account", right_on="account_id")
            .join(per_node, on=end)
            .group_by("owner_id")
            .agg(
                pl.col("accounts").max().alias(f"max_accounts_on_{end}"),
                # Distinct OWNERS on the device is the sharper signal: two
                # accounts belonging to one person is a current account and a
                # savings account, not a ring.
                pl.col("owners").max().alias(f"max_owners_on_{end}"),
            )
        )

    device = _shared("txn_via_device.parquet", "device_id")
    ip = _shared("txn_via_ip.parquet", "ip_id")

    # Distinct cards seen on a device the customer used. The T4 signal: one
    # device across cards belonging to unrelated people.
    card_link = pl.scan_parquet(graph / "txn_on_card.parquet").rename(
        {"start_id": "txn_id", "end_id": "card_id"}
    )
    dev_link = pl.scan_parquet(graph / "txn_via_device.parquet").rename(
        {"start_id": "txn_id", "end_id": "device_id"}
    )
    cards_per_device = (
        dev_link.join(card_link, on="txn_id")
        .group_by("device_id")
        .agg(pl.col("card_id").n_unique().alias("cards"))
    )
    device_cards = (
        dev_link.join(frm, on="txn_id")
        .join(owner, left_on="from_account", right_on="account_id")
        .join(cards_per_device, on="device_id")
        .group_by("owner_id")
        .agg(pl.col("cards").max().alias("max_cards_on_device"))
    )

    # --- counterparty degree and two-hop reach ---------------------------
    edges = (
        txn.join(frm, on="txn_id")
        .join(to, on="txn_id")
        .select("from_account", "to_account", "amount_usd", "channel", "booked_at")
    )
    out_deg = (
        edges.join(owner, left_on="from_account", right_on="account_id")
        .group_by("owner_id")
        .agg(pl.col("to_account").n_unique().alias("counterparties_out"))
    )
    in_deg = (
        edges.join(owner, left_on="to_account", right_on="account_id")
        .group_by("owner_id")
        .agg(pl.col("from_account").n_unique().alias("counterparties_in"))
    )

    # Two hops downstream over WIRE only, on DISTINCT account pairs. A layering
    # chain is precisely a path: the account's own statement looks like ordinary
    # trade, and only the continuation gives it away.
    #
    # Both restrictions are about cost, and neither is arbitrary. Including ACH
    # makes this join explode - 12.1M ACH edges over ~140K accounts is an
    # average degree near 90 each way, so joining raw edges on the middle
    # account is billions of rows. Collapsing to distinct pairs first bounds it
    # by the shape of the graph rather than by transaction volume, and wire is
    # where the chain hops actually are: the layering generator moves each hop
    # as a wire and uses ACH only for the decoy supplier payments.
    hop1 = edges.filter(pl.col("channel") == "wire").select("from_account", "to_account").unique()
    hop2 = hop1.join(
        hop1.rename({"from_account": "to_account", "to_account": "hop2_account"}),
        on="to_account",
    )
    two_hop = (
        hop2.filter(pl.col("hop2_account") != pl.col("from_account"))
        .join(owner, left_on="from_account", right_on="account_id")
        .group_by("owner_id")
        .agg(pl.col("hop2_account").n_unique().alias("two_hop_out"))
    )

    # --- do my counterparties behave like conduits? ----------------------
    #
    # Per-account pass-through, then averaged over the accounts paying ME.
    # A shell in the middle of a chain is fed by other shells; a real business
    # is fed by customers who keep most of what they earn.
    acct_flow = (
        txn.join(frm, on="txn_id")
        .group_by("from_account")
        .agg(
            pl.col("amount_usd").filter(pl.col("direction") == "credit").sum().alias("in_usd"),
            pl.col("amount_usd").filter(pl.col("direction") == "debit").sum().alias("out_usd"),
        )
        .select(
            pl.col("from_account").alias("account_id"),
            (pl.col("out_usd") / (pl.col("in_usd") + 1.0)).alias("acct_pass_through"),
        )
    )
    upstream = (
        edges.join(acct_flow, left_on="from_account", right_on="account_id")
        .join(owner, left_on="to_account", right_on="account_id")
        .group_by("owner_id")
        .agg(
            pl.col("acct_pass_through").mean().alias("upstream_pass_through_mean"),
            (pl.col("acct_pass_through") > 0.9).mean().alias("upstream_conduit_share"),
        )
    )

    # --- CNP burst, per card ---------------------------------------------
    #
    # The maximum number of card-not-present charges on any one card inside a
    # rolling hour. `cnp_count` in the tabular set is a year's total, in which
    # a fifteen-charge burst is invisible.
    cnp = (
        txn.filter(pl.col("channel") == "card_cnp")
        .join(card_link, on="txn_id")
        .join(frm, on="txn_id")
        .join(owner, left_on="from_account", right_on="account_id")
        .select("owner_id", "card_id", "booked_at")
        .sort("booked_at")
        .collect()
    )
    if cnp.height:
        burst = (
            cnp.rolling(index_column="booked_at", period="1h", group_by="card_id")
            .agg(pl.len().alias("in_hour"), pl.col("owner_id").first())
            .group_by("owner_id")
            .agg(pl.col("in_hour").max().alias("max_cnp_per_hour"))
            .lazy()
        )
    else:
        burst = pl.LazyFrame(
            {"owner_id": [], "max_cnp_per_hour": []},
            schema={"owner_id": pl.String, "max_cnp_per_hour": pl.UInt32},
        )

    out = out_deg
    for frame in (in_deg, two_hop, upstream, device, ip, device_cards, burst):
        out = out.join(frame, on="owner_id", how="full", coalesce=True)

    return out.collect().with_columns(
        # A customer absent from one of these frames genuinely has none of that
        # structure - no device, no counterparties, no card - so zero is the
        # right fill rather than a missing value the model has to learn around.
        pl.col(c).fill_null(0)
        for c in (
            "counterparties_out",
            "counterparties_in",
            "two_hop_out",
            "upstream_pass_through_mean",
            "upstream_conduit_share",
            "max_accounts_on_device_id",
            "max_owners_on_device_id",
            "max_accounts_on_ip_id",
            "max_owners_on_ip_id",
            "max_cards_on_device",
            "max_cnp_per_hour",
        )
    )


def _account_owner(dataset_dir: Path) -> pl.DataFrame:
    """Map every account to a single namespaced owner id.

    Namespacing matters: individual and entity ids are independent sequences,
    so joining on a bare owner index would merge unrelated customers.
    """
    graph = dataset_dir / "graph"
    individual = pl.read_parquet(graph / "individual_owns_account.parquet").select(
        pl.col("start_id").alias("owner_id"), pl.col("end_id").alias("account_id")
    )
    entity = pl.read_parquet(graph / "entity_owns_account.parquet").select(
        pl.col("start_id").alias("owner_id"), pl.col("end_id").alias("account_id")
    )
    return pl.concat([individual, entity]).unique(subset=["account_id"])


def _labels(dataset_dir: Path) -> pl.DataFrame:
    """Customer-level ground truth. Read last, and used only to build `y`.

    Both polarities are returned. Illicit subjects are the positives; hard
    negatives are legitimate and must never be scored as positives - they are
    measured separately, by how much of the alert queue they consume.
    """
    path = dataset_dir / "ground_truth" / "typology_label.parquet"
    empty = {
        "owner_id": pl.String,
        "typology": pl.String,
        "polarity": pl.String,
        "difficulty_tier": pl.String,
    }
    if not path.exists():
        return pl.DataFrame(schema=empty)
    return (
        pl.read_parquet(path)
        .filter(pl.col("subject_type").is_in(["individual", "legal_entity"]))
        .select(pl.col("subject_id").alias("owner_id"), "typology", "polarity", "difficulty_tier")
        .unique()
    )


def _auc_pr(y: np.ndarray, score: np.ndarray) -> float:
    """Average precision, computed directly to avoid a sklearn dependency here."""
    order = np.argsort(score)[::-1]
    y = y[order]
    tp = np.cumsum(y)
    precision = tp / np.arange(1, len(y) + 1)
    total = y.sum()
    if total == 0:
        return float("nan")
    return float((precision * y).sum() / total)


def _gbm_scores(features: pl.DataFrame, y: np.ndarray, seed: int) -> np.ndarray:
    """Out-of-fold scores from a gradient-boosted baseline.

    Out-of-fold rather than a single split: at a 0.4% base rate a single test
    fold may contain only a handful of positives, and the resulting AUC-PR
    would swing enough between seeds to make calibration meaningless.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import StratifiedKFold

    x = features.drop("owner_id").fill_null(0.0).to_numpy()
    scores = np.zeros(len(y))
    n_splits = min(5, max(2, int(y.sum())))
    folds = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    for train, test in folds.split(x, y):
        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.1, max_leaf_nodes=31, random_state=seed
        )
        model.fit(x[train], y[train])
        scores[test] = model.predict_proba(x[test])[:, 1]
    return scores


def run(
    dataset_dir: Path, controls: Controls, *, seed: int = 0
) -> tuple[list[Check], list[TypologyScore]]:
    """Score both baselines against the dataset, per typology."""
    graph = dataset_dir / "graph"
    features = customer_features(dataset_dir)
    truth = _labels(dataset_dir)

    checks: list[Check] = []
    scores: list[TypologyScore] = []
    if truth.height == 0:
        return (
            [
                Check(
                    "typologies_injected",
                    0.0,
                    1.0,
                    None,
                    "No labelled entities found. Detectability cannot be "
                    "calibrated against a dataset with nothing injected.",
                )
            ],
            scores,
        )

    owner = _account_owner(dataset_dir)
    rules = run_rules(
        controls,
        txn=pl.scan_parquet(graph / "transaction.parquet"),
        txn_from=pl.scan_parquet(graph / "txn_from.parquet"),
        accounts=pl.read_parquet(graph / "account.parquet"),
        account_owner=owner,
        card_account=pl.read_parquet(graph / "card.parquet").select("card_id", "account_id"),
    )

    joined = features.join(rules, on="owner_id", how="left").with_columns(
        pl.col("rule_score").fill_null(0), pl.col("alert_severity").fill_null(0.0)
    )
    # Row order must match `joined` exactly: `y` is built from one and applied
    # to both, so a join that reorders rows would silently mislabel every row
    # of the graph-augmented model.
    joined_graph = joined.join(graph_features(dataset_dir), on="owner_id", how="left")
    joined_graph = joined_graph.fill_null(0)
    assert joined_graph["owner_id"].to_list() == joined["owner_id"].to_list()
    budget = max(1, int(round(joined.height * controls.alert_budget_pct / 100.0)))

    # Illicit rings per typology, for the tier-power note below.
    ring_path = dataset_dir / "ground_truth" / "ring.parquet"
    ring_counts: dict[str, int] = {}
    if ring_path.exists():
        rings_tbl = pl.read_parquet(ring_path)
        if "polarity" in rings_tbl.columns:
            rings_tbl = rings_tbl.filter(pl.col("polarity") == "illicit")
        else:
            # Datasets generated before Ring.polarity existed: the id prefix is
            # the only discriminator, and a look-alike carries the typology it
            # mimics, so counting without this inflates every typology.
            rings_tbl = rings_tbl.filter(pl.col("ring_id").str.starts_with("RING-"))
        ring_counts = dict(
            rings_tbl.group_by("typology").agg(pl.len().alias("n")).iter_rows()  # type: ignore[arg-type]
        )

    for typology in sorted(truth["typology"].unique().to_list()):
        rows = truth.filter(pl.col("typology") == typology)
        positives = set(rows.filter(pl.col("polarity") == "illicit")["owner_id"].to_list())
        # Legitimate look-alikes. Never scored as positives - they are measured
        # by how much of the alert queue they consume instead.
        look_alikes = set(rows.filter(pl.col("polarity") == "hard_negative")["owner_id"].to_list())
        y = joined["owner_id"].is_in(positives).to_numpy().astype(int)
        if y.sum() == 0:
            continue

        # Rules baseline: rank by rule score, tie-break by total volume the way
        # an alert queue would, take the budget, measure recall.
        # Ranked by severity, the way a risk engine feeds an alert queue -
        # not by rule count with a volume tie-break, which buried every retail
        # positive behind high-turnover businesses.
        queue = (
            joined.with_columns(pl.Series("y", y))
            .sort(["alert_severity", "rule_score"], descending=True)["y"]
            .to_numpy()
        )
        curve = {
            pct: float(queue[: max(1, int(round(len(queue) * pct / 100.0)))].sum() / y.sum())
            for pct in REPORTED_BUDGETS_PCT
        }
        rules_recall = float(queue[:budget].sum() / y.sum())

        # How much of the queue the look-alikes consume. This is the number
        # that says whether the hard negatives are doing their job: if a
        # legitimate cash business never reaches the alert queue, it is not
        # competing with the smurf ring for an analyst's attention, and the
        # precision the dataset reports is flattering.
        hn_flag = (
            joined.with_columns(pl.col("owner_id").is_in(list(look_alikes)).alias("hn"))
            .sort(["alert_severity", "rule_score"], descending=True)["hn"]
            .to_numpy()
        )
        hn_share = float(hn_flag[:budget].mean()) if budget else 0.0

        # Recall per difficulty tier. This is the check that actually tests D6:
        # a tier is supposed to mean "generated with less signal", so an easy
        # ring must be caught more often than a hard one. Overall recall cannot
        # show that - it is just the tier mix, and a dataset whose tiers were
        # pure decoration would report exactly the same number.
        order = np.argsort(
            -(joined["alert_severity"].to_numpy() + 1e-9 * joined["rule_score"].to_numpy()),
            kind="stable",
        )
        alerted = set(joined["owner_id"].to_numpy()[order][:budget].tolist())
        tier_recall: dict[str, float] = {}
        for tier in ("easy", "medium", "hard"):
            members = set(
                rows.filter(
                    (pl.col("polarity") == "illicit") & (pl.col("difficulty_tier") == tier)
                )["owner_id"].to_list()
            )
            if members:
                tier_recall[tier] = len(members & alerted) / len(members)

        tabular = joined.drop([c for c in joined.columns if c.startswith("rule")])
        gbm = _gbm_scores(tabular, y, seed)
        auc_pr = _auc_pr(y, gbm)
        # Same model, same folds, same labels - the only difference is the
        # topology columns. Scored separately rather than folded in so the
        # tabular number stays interpretable as "what a tabular model can do".
        with_graph = joined_graph.drop([c for c in joined_graph.columns if c.startswith("rule")])
        auc_pr_graph = _auc_pr(y, _gbm_scores(with_graph, y, seed))
        no_skill = float(y.mean())

        scores.append(
            TypologyScore(
                typology=typology,
                positives=int(y.sum()),
                hard_negatives=len(look_alikes),
                rules_recall_at_budget=rules_recall,
                recall_curve=curve,
                hard_negative_alert_share=hn_share,
                tier_recall=tier_recall,
                gbm_auc_pr=auc_pr,
                gbm_graph_auc_pr=auc_pr_graph,
                gbm_baseline_auc_pr=no_skill,
                rings=ring_counts.get(typology, 0),
            )
        )

        # Recall is quantized to 1/positives. Below ~50 positives a single
        # entity moves it by more than the width of a calibration decision, so
        # reporting a pass/fail against the band would be reading noise. Say so
        # instead of producing a number that looks authoritative.
        if look_alikes:
            true_in_queue = int(queue[:budget].sum())
            hn_in_queue = int(hn_flag[:budget].sum())
            checks.append(
                Check(
                    f"look_alike_to_positive_ratio_{typology}",
                    hn_in_queue / max(true_in_queue, 1),
                    2.0,
                    None,
                    "Legitimate look-alikes per true positive inside the alert "
                    "queue. A ratio, not a share of the queue: share is bounded "
                    "by how many hard negatives exist relative to the budget "
                    "(55 look-alikes cannot fill a quarter of a 500-alert queue "
                    "however well they are built), whereas the ratio expresses "
                    "what the check is actually for - whether a detector has to "
                    "work to tell them apart. Real AML queues run 10:1 or worse.",
                    detail=f"{hn_in_queue} look-alikes vs {true_in_queue} true, "
                    f"{hn_share:.1%} of queue",
                )
            )

        if int(y.sum()) < MIN_POSITIVES_FOR_CALIBRATION:
            checks.append(
                Check(
                    f"calibration_sample_{typology}",
                    float(y.sum()),
                    float(MIN_POSITIVES_FOR_CALIBRATION),
                    None,
                    "Positives available for calibration. Recall is quantized to "
                    f"1/positives — at {int(y.sum())} that is "
                    f"{1 / max(int(y.sum()), 1):.1%} per entity, wider than the "
                    "band this gate is trying to resolve. Calibrate on a preset "
                    "large enough to carry the configured prevalence, not by "
                    "raising prevalence to suit the preset.",
                    detail=f"rules recall {rules_recall:.3f}, GBM AUC-PR {auc_pr:.3f}",
                )
            )
            continue

        # Tier separation needs rings, not just positives. Recall is measured
        # per entity, but every member of a ring inherits that ring's tier, so
        # with three rings per tier the comparison is three draws against
        # three - ring-to-ring variance wearing a tier label. Reported as
        # unresolved rather than passed or failed, because a number here looks
        # authoritative and would be read as evidence about D6.
        n_rings = ring_counts.get(typology, 0)
        if {"easy", "hard"} <= tier_recall.keys():
            if n_rings >= MIN_RINGS_FOR_TIER_SEPARATION:
                checks.append(
                    Check(
                        f"tier_separation_{typology}",
                        tier_recall["easy"] - tier_recall["hard"],
                        0.15,
                        None,
                        "Easy-tier recall minus hard-tier recall. This is the "
                        "check that tests D6 - that a difficulty tier means the "
                        "ring was generated with less signal, not that it was "
                        "labelled differently. Overall recall cannot show it: a "
                        "dataset whose tiers were pure decoration would report "
                        "the same aggregate number and the same curriculum "
                        "would be meaningless.",
                        detail=" ".join(f"{t}={v:.2f}" for t, v in tier_recall.items()),
                    )
                )
            else:
                checks.append(
                    Check(
                        f"tier_separation_power_{typology}",
                        float(n_rings),
                        float(MIN_RINGS_FOR_TIER_SEPARATION),
                        None,
                        "Illicit rings available to compare tiers. Tier recall "
                        "is per entity, but a ring's members all carry its "
                        f"tier, so {n_rings} rings across three tiers gives "
                        "one or two independent draws each. The separation "
                        "figure is reported below for information and is NOT a "
                        "gate at this ring count. A typology whose rings are "
                        "large buys few of them from a fixed entity budget - "
                        "that is what to change, not the band.",
                        detail=" ".join(f"{t}={v:.2f}" for t, v in tier_recall.items()),
                    )
                )

        checks.append(
            Check(
                f"rules_recall_{typology}",
                rules_recall,
                0.10,
                0.60,
                "Recall of the bank-style rules baseline at a "
                f"{controls.alert_budget_pct:g}% alert budget. A wide band on "
                "purpose: the aggregate is mostly the tier mix, so it only "
                "catches gross failure - unfindable at one end, handed over at "
                "the other. Tier separation above is the informative gate.",
                detail=(
                    f"{int(y.sum())} positives, {budget:,} alerts · curve "
                    + " ".join(f"@{p:g}%={v:.2f}" for p, v in curve.items())
                ),
            )
        )
        # The gate is the graph-augmented model. A typology that is invisible
        # to per-customer averages but recoverable with topology is a correct
        # dataset, not a broken one - layering is a property of a path and card
        # fraud a burst on one card, and no amount of yearly aggregation will
        # show either. The tabular number stays in the detail, because the gap
        # between the two is the dataset's actual argument.
        best = max(auc_pr, auc_pr_graph)
        checks.append(
            Check(
                f"gbm_auc_pr_{typology}",
                best,
                0.10,
                0.85,
                "Average precision of a gradient-boosted baseline. Scored twice "
                "on identical folds - tabular customer features, then the same "
                "features plus graph topology - and gated on the better of the "
                "two. Above 0.85 the dataset is trivially separable and proves "
                "nothing; below 0.10 there is no learnable signal by either "
                "route and it proves nothing either.",
                detail=(
                    f"tabular {auc_pr:.3f}, +graph {auc_pr_graph:.3f} "
                    f"({auc_pr_graph / max(auc_pr, 1e-9):.1f}x) · "
                    f"no-skill {no_skill:.4f}, lift {best / max(no_skill, 1e-9):.0f}x"
                ),
            )
        )

    return checks, scores
