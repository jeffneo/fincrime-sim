"""Statistical fidelity checks (spec §9.1).

Each check returns a :class:`Check` with a measured value, the band it is
expected to fall in, and a one-line reason the band is what it is. The bands
are the interesting part: a number with no expectation attached tells you
nothing, and "looks plausible" is not a test you can run in CI.

Bands at M1 cover the background only. Checks that need injected typologies
(Benford deviation on structured deposits, per-typology detectability) arrive
with the generators that produce them.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

#: Expected first-digit frequencies under Benford's law.
BENFORD = np.array([math.log10(1 + 1 / d) for d in range(1, 10)])


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    value: float
    low: float | None
    high: float | None
    why: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        if self.low is not None and self.value < self.low:
            return False
        return not (self.high is not None and self.value > self.high)

    @property
    def band(self) -> str:
        if self.low is not None and self.high is not None:
            return f"{self.low:g} – {self.high:g}"
        if self.low is not None:
            return f"≥ {self.low:g}"
        if self.high is not None:
            return f"≤ {self.high:g}"
        return "—"


def _window_months(dataset_dir: Path) -> float:
    """Simulated window length in months, from the dataset manifest."""
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    return max(manifest["window"]["days"] / 30.44, 1e-9)


def benford_mad(amounts: np.ndarray) -> float:
    """Mean absolute deviation of first-digit frequencies from Benford.

    The conventional reading of MAD for first digits: < 0.006 conformant,
    0.006–0.012 acceptable, > 0.015 non-conformant. Legitimate transaction
    amounts spanning several orders of magnitude should conform; deliberately
    structured amounts clustered under a threshold should not, which is what
    makes this the cheapest available signal for T1.
    """
    positive = amounts[amounts > 0]
    if positive.size == 0:
        return float("nan")
    lead = (positive / np.power(10.0, np.floor(np.log10(positive)))).astype(int)
    lead = np.clip(lead, 1, 9)
    observed = np.bincount(lead, minlength=10)[1:10].astype(float)
    observed /= observed.sum()
    return float(np.abs(observed - BENFORD).mean())


def _hill(tail: np.ndarray, xmin: float) -> float:
    return float(1.0 + tail.size / np.sum(np.log(tail / (xmin - 0.5))))


def _powerlaw_alpha(degrees: np.ndarray) -> float:
    """Tail exponent of a degree distribution, with xmin chosen by KS distance.

    Hill is only valid inside the power-law regime and is badly biased when
    xmin falls in the body of the distribution. A fixed xmin cannot work here
    because degree scales with the simulation window: a cutoff that sits in the
    tail of a 3-month dataset sits in the body of a 12-month one, and the same
    generator then reports two different exponents.

    So xmin is selected the standard way (Clauset, Shalizi & Newman): fit alpha
    at each candidate and keep the one minimizing the Kolmogorov-Smirnov
    distance between the empirical tail and the fitted Pareto. That makes the
    measurement a property of the distribution's shape rather than of how long
    the simulation ran.

    Real transaction graphs are heavy-tailed. Empirical social and financial
    networks land around alpha 2-3; far outside that means the counterparty
    model is either too uniform (high alpha) or has runaway hubs (low alpha).
    """
    degrees = degrees[degrees > 0]
    if degrees.size < 200:
        return float("nan")

    candidates = np.unique(np.quantile(degrees, np.linspace(0.30, 0.95, 24)).astype(int))
    candidates = candidates[candidates >= 2]
    best_alpha, best_ks = float("nan"), np.inf
    for xmin in candidates:
        tail = np.sort(degrees[degrees >= xmin])
        if tail.size < 100:
            continue
        alpha = _hill(tail, float(xmin))
        if not np.isfinite(alpha) or alpha <= 1.0:
            continue
        empirical = np.arange(1, tail.size + 1) / tail.size
        fitted = 1.0 - np.power(tail / xmin, 1.0 - alpha)
        ks = float(np.max(np.abs(empirical - fitted)))
        if ks < best_ks:
            best_alpha, best_ks = alpha, ks
    return best_alpha


def run(dataset_dir: Path, *, months: float | None = None) -> list[Check]:
    """Run every M1 statistical check against a generated dataset.

    Counting metrics are normalized per month using ``months`` (read from the
    manifest when not given). Without that, a check calibrated on the 3-month
    dev preset fails on the 12-month mvp preset for no reason other than the
    window being longer, which makes the gate useless for the release build.

    Columns are read lazily and projected: the mvp transaction table is 55M
    rows, and loading it whole cost more memory than generating it did.
    """
    graph = dataset_dir / "graph"
    txn_path = graph / "transaction.parquet"
    checks: list[Check] = []

    if months is None:
        months = _window_months(dataset_dir)

    lazy = pl.scan_parquet(txn_path)
    n_txn = int(lazy.select(pl.len()).collect().item())
    if n_txn == 0:
        return [Check("dataset_non_empty", 0, 1, None, "Nothing to validate.")]

    amounts = lazy.select("amount_usd").collect()["amount_usd"].to_numpy()

    # --- amounts ---
    checks.append(
        Check(
            "benford_mad_all_transactions",
            benford_mad(amounts),
            None,
            0.012,
            "Legitimate amounts spanning orders of magnitude should conform to "
            "Benford. Above 0.015 is the conventional non-conformance line, so "
            "a clean background must sit well under it — otherwise the "
            "structuring signal has nothing to stand out against.",
        )
    )
    card_median = (
        lazy.filter(pl.col("txn_class").is_in(["retail", "retail_online"]))
        .select(pl.col("amount_usd").median())
        .collect()
        .item()
    )
    if card_median is not None:
        checks.append(
            Check(
                "median_card_amount_usd",
                float(card_median),
                15.0,
                80.0,
                "US card-present median ticket sits in the tens of dollars. "
                "Far outside this and the merchant amount model is wrong.",
            )
        )

    # --- timing ---
    clock = lazy.select(
        pl.col("booked_at").dt.hour().alias("hour"),
        pl.col("booked_at").dt.weekday().alias("dow"),
    ).collect()
    hours = clock["hour"].to_numpy()
    night = float(((hours >= 1) & (hours <= 5)).mean())
    checks.append(
        Check(
            "overnight_transaction_share",
            night,
            0.005,
            0.06,
            "Consumer activity should be near-dead 01:00–05:00. A flat clock is "
            "one of the fastest tells of synthetic data, and it would also let "
            "any typology with unusual timing hide in plain sight.",
        )
    )

    dow = clock["dow"].to_numpy()
    counts = np.bincount(dow, minlength=8)[1:8].astype(float)
    checks.append(
        Check(
            "weekday_seasonality_ratio",
            float(counts.max() / max(counts.min(), 1.0)),
            1.15,
            2.5,
            "Busiest day over quietest day. Below 1.15 there is effectively no "
            "weekly rhythm; above 2.5 the seasonality is caricatured.",
        )
    )

    # --- structure ---
    per_account = (
        pl.scan_parquet(graph / "txn_from.parquet")
        .group_by("end_id")
        .len()
        .select("len")
        .collect()["len"]
        .to_numpy()
    )
    if per_account.size:
        checks.append(
            Check(
                "monthly_txns_per_account",
                float(np.median(per_account)) / months,
                4.0,
                90.0,
                "Transactions per account per month. Normalized by window "
                "length: the raw count scales with how long the simulation ran, "
                "so an un-normalized band calibrated on the 3-month dev preset "
                "would fail the 12-month release build for no real reason.",
                detail=f"p90={np.percentile(per_account, 90) / months:.0f}/mo",
            )
        )

    degree = (
        pl.scan_parquet(graph / "txn_at_merchant.parquet")
        .group_by("end_id")
        .len()
        .select("len")
        .collect()["len"]
        .to_numpy()
    )
    if degree.size:
        checks.append(
            Check(
                "merchant_degree_powerlaw_alpha",
                _powerlaw_alpha(degree),
                1.8,
                3.2,
                "Merchant popularity must be heavy-tailed — a few chains taking "
                "most volume — or the account-merchant graph degenerates into a "
                "uniform blob that community detection cannot work on. Empirical "
                "social and financial networks sit at 2–3; the band is widened "
                "slightly on each side rather than tightened around the value "
                "this model happens to produce.",
            )
        )
        top_share = float(np.sort(degree)[::-1][:1].sum() / degree.sum())
        checks.append(
            Check(
                "largest_merchant_volume_share",
                top_share,
                None,
                0.04,
                "No single merchant should dominate. The largest US retailer is "
                "a few percent of retail spend; a synthetic hub above that "
                "distorts every centrality and community result computed over "
                "the merchant graph.",
            )
        )
        checks.append(
            Check(
                "low_volume_merchant_share",
                float((degree < 3 * months).mean()),
                0.05,
                None,
                "Share of merchants seen less than three times a month. A real "
                "bank sees a long tail of merchants appearing a handful of "
                "times; without it the pool is uniformly busy and an unfamiliar "
                "low-volume merchant stops being a usable signal. The threshold "
                "scales with the window so the same generator scores the same "
                "on a 3-month and a 12-month run.",
            )
        )
        # Repeat-merchant behavior is what the traveler hard negative (M4)
        # relies on to be distinguishable from card fraud. Joined lazily: at
        # the mvp preset this pairs ~36M card transactions with their accounts.
        # The streaming engine matters here specifically: this is a 36M-row
        # join keyed on two string columns, and the in-memory engine peaked
        # near physical RAM on it. Every other query in this module is an
        # aggregate that stays small.
        per_pair = (
            pl.scan_parquet(graph / "txn_at_merchant.parquet")
            .join(
                pl.scan_parquet(graph / "txn_from.parquet").rename({"end_id": "account_id"}),
                on="start_id",
                how="inner",
            )
            .group_by(["account_id", "end_id"])
            .len()
            .select("len")
            .collect(engine="streaming")["len"]
            .to_numpy()
        )
        checks.append(
            Check(
                "mean_repeat_visits_per_merchant_pair",
                float(per_pair.mean()),
                1.5,
                12.0,
                "Customers shop at the same places repeatedly. At 1.0 every "
                "visit is to a new merchant, which erases the loyalty signal "
                "the frequent-traveler hard negative depends on.",
            )
        )

    # --- composition ---
    cash = lazy.filter(pl.col("channel") == "cash").select("amount_usd").collect()
    checks.append(
        Check(
            "cash_transaction_share",
            cash.height / n_txn,
            0.02,
            0.20,
            "Cash is a minority of transactions but must be present in volume: "
            "it is the channel the structuring typology lives in, and a thin "
            "cash background makes T1 trivially visible.",
        )
    )
    if cash.height:
        checks.append(
            Check(
                "cash_under_ctr_threshold_share",
                float((cash["amount_usd"] < 10_000).mean()),
                0.95,
                1.0,
                "Almost all legitimate cash activity is far below the USD "
                "10,000 CTR threshold. Structured deposits also sit below it — "
                "which is exactly why proximity to the threshold, not position "
                "relative to it, has to carry the signal.",
            )
        )

    ctr = float(lazy.select(pl.col("ctr_reportable").mean()).collect().item())
    checks.append(
        Check(
            "ctr_reportable_share",
            ctr,
            0.0,
            0.01,
            "CTR filings should be rare and driven by genuinely large cash "
            "activity. A high rate means the cash amount model is inflated.",
        )
    )

    n_internal = int(pl.scan_parquet(graph / "txn_to.parquet").select(pl.len()).collect().item())
    checks.append(
        Check(
            "internal_counterparty_share",
            n_internal / n_txn,
            0.03,
            0.35,
            "Share of transactions where both sides bank here. Too low and the "
            "graph is a set of disconnected stars with nothing to traverse; too "
            "high and a single-institution simulation stops being realistic.",
        )
    )

    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    passed = sum(1 for c in checks if c.ok)
    return passed, len(checks)
