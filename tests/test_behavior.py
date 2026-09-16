"""Background transaction invariants.

The statistical shape of the background is checked by ``fincrime validate``
(``validate/stats.py``), which owns the bands and their justifications. What is
tested here is the things that must hold for *every* row rather than in
aggregate — a single transaction outside the simulation window, or a negative
amount, is a bug no distributional check would necessarily catch.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fincrime.behavior import month_windows
from fincrime.behavior import simulate_to_frames as simulate
from fincrime.config import load_config
from fincrime.population import build
from fincrime.reference import TXN_CLASSES
from fincrime.rng import streams


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/scale-dev.yaml")


@pytest.fixture(scope="module")
def generated(cfg):
    rng = streams(cfg.seed)
    pop = build(cfg, rng)
    txn, edges = simulate(cfg, rng, pop)
    return pop, txn, edges


def test_month_windows_cover_the_whole_period(cfg):
    windows = month_windows(cfg.window.start, cfg.window.end)
    assert sum(days for _, days in windows) == cfg.window.days
    assert windows[0][0] == cfg.window.start


def test_transactions_fall_inside_the_window(cfg, generated):
    _, txn, _ = generated
    booked = txn["booked_at"].to_numpy()
    assert booked.min() >= np.datetime64(cfg.window.start, "us")
    # End of the final day, not the start of it.
    assert booked.max() < np.datetime64(cfg.window.end, "D") + np.timedelta64(1, "D")


def test_all_amounts_are_positive(generated):
    _, txn, _ = generated
    assert txn["amount"].min() > 0


def test_amounts_are_rounded_to_cents(generated):
    _, txn, _ = generated
    cents = (txn["amount"] * 100).round(6)
    assert (cents - cents.round(0)).abs().max() < 1e-6


def test_channel_matches_transaction_class(generated):
    """A typology emitting a class/channel pair the background never produces
    would hand detectors a free feature, so the mapping has to be one table
    that both sides use."""
    _, txn, _ = generated
    pairs = txn.select("txn_class", "channel").unique()
    for cls, channel in pairs.iter_rows():
        assert TXN_CLASSES[cls] == channel


def test_every_transaction_has_an_originating_account(generated):
    _, txn, edges = generated
    assert edges["txn_from"].height == txn.height


def test_edge_endpoints_resolve(generated):
    pop, txn, edges = generated
    txn_ids = set(txn["txn_id"].to_list())
    targets = {
        "txn_from": set(pop.account_ids.tolist()),
        "txn_to": set(pop.account_ids.tolist()),
        "txn_at_merchant": set(pop.merchant_ids.tolist()),
        "txn_on_card": set(pop.card_ids.tolist()),
        "txn_via_device": set(pop.device_ids.tolist()),
        "txn_via_ip": set(pop.ip_ids.tolist()),
    }
    for name, valid in targets.items():
        df = edges[name]
        if df.height == 0:
            continue
        assert set(df["start_id"].to_list()) <= txn_ids, f"{name} has a dangling start_id"
        assert set(df["end_id"].to_list()) <= valid, f"{name} has a dangling end_id"


def test_no_self_payments(generated):
    _, _, edges = generated
    joined = edges["txn_from"].join(edges["txn_to"], on="start_id", how="inner")
    assert joined.filter(pl.col("end_id") == pl.col("end_id_right")).height == 0


def test_card_transactions_carry_both_a_card_and_a_merchant(generated):
    _, txn, edges = generated
    card_txns = set(
        txn.filter(pl.col("txn_class").is_in(["retail", "retail_online"]))["txn_id"].to_list()
    )
    assert card_txns == set(edges["txn_on_card"]["start_id"].to_list())
    assert card_txns == set(edges["txn_at_merchant"]["start_id"].to_list())


def test_cash_and_card_present_carry_no_device(generated):
    """Terminal transactions have no app session.

    Attaching a device to everything would give detectors a device signal on
    transactions that in reality carry none, which quietly makes several
    typologies easier than they should be.
    """
    _, txn, edges = generated
    terminal = set(
        txn.filter(pl.col("channel").is_in(["cash", "card_present"]))["txn_id"].to_list()
    )
    assert terminal & set(edges["txn_via_device"]["start_id"].to_list()) == set()


def test_atm_withdrawals_are_multiples_of_twenty(generated):
    """ATMs dispense $20 notes.

    This also puts a legitimate round-number mode into the cash channel, which
    is where structuring lives - so it has to be right.
    """
    _, txn, _ = generated
    cash_out = txn.filter(pl.col("txn_class") == "cash_withdrawal")
    assert (cash_out["amount"] % 20 == 0).all()


def test_ctr_aggregates_same_day_cash_per_customer(cfg, generated):
    """31 CFR 1010.313 aggregation, not a per-transaction test.

    Flagging only individually-large transactions would let a structuring
    generator evade a control the real institution does not have.
    """
    pop, txn, edges = generated
    threshold = cfg.institution["controls"]["ctr_threshold_usd"]
    cash = txn.filter(pl.col("channel") == "cash")
    if cash.height == 0:
        pytest.skip("no cash transactions")

    owner_of_account = dict(
        zip(
            pop.account_ids.tolist(),
            [
                (int(o) + (10**9 if e else 0))
                for o, e in zip(
                    pop.account_owner.tolist(), pop.account_owner_is_entity.tolist(), strict=True
                )
            ],
            strict=True,
        )
    )
    joined = (
        cash.join(edges["txn_from"], left_on="txn_id", right_on="start_id")
        .with_columns(
            pl.col("end_id").replace_strict(owner_of_account).alias("owner"),
            pl.col("booked_at").dt.date().alias("day"),
        )
        .group_by(["owner", "day"])
        .agg(
            pl.col("amount_usd").sum().alias("total"),
            pl.col("ctr_reportable").any().alias("flagged"),
        )
    )
    mismatched = joined.filter((pl.col("total") > threshold) != pl.col("flagged"))
    assert mismatched.height == 0


def test_recurring_income_actually_recurs(generated):
    """Payroll must arrive on a cadence, not as Poisson noise.

    The mixture of recurring and discretionary activity is what detectors key
    on; if payroll were just another random stream, every account would look
    like an unbanked one.
    """
    _, txn, edges = generated
    payroll = txn.filter(pl.col("txn_class") == "payroll").join(
        edges["txn_from"], left_on="txn_id", right_on="start_id"
    )
    per_account = payroll.group_by("end_id").agg(
        pl.col("amount").std().alias("sd"), pl.col("amount").mean().alias("mean"), pl.len()
    )
    repeat = per_account.filter(pl.col("len") >= 3)
    assert repeat.height > 0
    # Coefficient of variation stays low across periods for a given account.
    cv = (repeat["sd"] / repeat["mean"]).median()
    assert cv < 0.2, f"payroll varies too much period to period (cv={cv:.3f})"


def test_simulation_is_reproducible(cfg):
    def once():
        rng = streams(cfg.seed)
        pop = build(cfg, rng)
        return simulate(cfg, rng, pop)[0]

    assert once().equals(once())
