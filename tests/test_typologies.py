"""Typology injection invariants.

The tests that matter most here are the ones about what an injected transaction
must NOT look like. A typology whose transactions are identifiable by anything
other than their own behaviour — an id range, a memo format, a channel no
legitimate transaction uses — makes the whole dataset worthless for training,
and none of it would show up as a failing distribution check.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fincrime import labels as labels_mod
from fincrime import population, typologies
from fincrime.behavior import simulate_to_frames
from fincrime.config import load_config
from fincrime.institution import Controls, ctr_reportable
from fincrime.rng import streams
from fincrime.typologies.base import _tier_counts


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/scale-dev.yaml")


@pytest.fixture(scope="module")
def injected(cfg):
    rng = streams(cfg.seed)
    pop = population.build(cfg, rng)
    controls = Controls.from_config(cfg.institution)
    result = typologies.inject(cfg, rng, pop, controls)
    return cfg, pop, controls, result


def test_rings_are_produced(injected):
    _, _, _, result = injected
    assert result.rings, "no rings injected at the configured prevalence"
    assert result.pending.total > 0


def test_prevalence_is_within_the_configured_rate(injected):
    """Crime must stay rare (spec §3).

    The band is one-sided-ish on purpose: undershooting is a nuisance, but
    overshooting destroys the class imbalance that makes the detection problem
    realistic and silently inflates every precision number downstream.
    """
    cfg, pop, _, result = injected
    target = cfg.typologies["prevalence"]["illicit_entity_fraction"]
    entity_labels = {
        label.subject_id
        for label in result.labels
        if label.subject_type in ("individual", "legal_entity")
    }
    actual = len(entity_labels) / pop.tables["individual"].height
    assert actual <= target * 1.5, f"prevalence {actual:.4f} overshoots target {target}"


def test_every_ring_has_a_collector_and_multiple_smurfs(injected):
    _, _, _, result = injected
    by_ring: dict[str, list[str]] = {}
    for label in result.labels:
        if label.subject_type == "account":
            by_ring.setdefault(label.ring_id, []).append(label.role)
    for ring_id, roles in by_ring.items():
        assert roles.count("collector") == 1, f"{ring_id} has {roles.count('collector')} collectors"
        assert roles.count("smurf") >= 2, f"{ring_id} has too few smurfs"


def test_no_account_is_used_by_two_rings(injected):
    """Overlapping hosts would make a ring's boundary ambiguous, and any
    topology check that recovered one would appear to recover the other."""
    _, _, _, result = injected
    seen: set[tuple[str, str]] = set()
    per_account: dict[str, set[str]] = {}
    for label in result.labels:
        if label.subject_type == "account":
            per_account.setdefault(label.subject_id, set()).add(label.ring_id)
            seen.add((label.subject_id, label.ring_id))
    overlapping = {a: r for a, r in per_account.items() if len(r) > 1}
    assert not overlapping, f"accounts shared across rings: {list(overlapping)[:3]}"


def test_deposits_stay_under_the_reporting_threshold(injected):
    """Every individual structured deposit must sit below the CTR threshold.

    A deposit at or above it would be reported outright; a scheme would not
    place one, and a generator that did would be modelling the typology wrong.
    """
    _, _, controls, result = injected
    for chunks in result.pending.by_month.values():
        for chunk in chunks:
            if chunk["txn_class"] != "cash_deposit":
                continue
            assert chunk["amount"].max() < controls.ctr_threshold_usd


def test_same_day_aggregates_never_trip_the_ctr_rule(injected):
    """The control the scheme is actually evading (31 CFR 1010.313).

    Two 9,000 deposits on one day aggregate to 18,000 and get reported. If the
    generator let that happen, the bank would catch the ring for free and the
    typology would look harder to evade than it is.
    """
    cfg, pop, controls, result = injected
    rows = [
        chunk
        for chunks in result.pending.by_month.values()
        for chunk in chunks
        if chunk["txn_class"] == "cash_deposit"
    ]
    if not rows:
        pytest.skip("no structured deposits")

    account = np.concatenate([c["from_account"] for c in rows])
    amount = np.concatenate([c["amount"] for c in rows])
    ts = np.concatenate([c["ts"] for c in rows])

    owner = pop.account_owner[account]
    owner_key = np.where(pop.account_owner_is_entity[account], owner + 10**9, owner)
    flags = ctr_reportable(
        controls,
        owner_key=owner_key,
        day=ts.astype("datetime64[D]").astype("int64"),
        amount_usd=amount,
        is_cash=np.ones(len(amount), dtype=bool),
    )
    assert not flags.any(), f"{int(flags.sum())} structured deposits would be CTR-reported"


def test_difficulty_tiers_differ_in_generator_parameters(injected):
    """D6: a tier must mean different generation, not a different label.

    If easy and hard rings were produced by the same knobs, curriculum
    evaluation would be measuring nothing.
    """
    _, _, _, result = injected
    digests = {r.difficulty_tier: r.knob_digest for r in result.rings}
    if len(digests) > 1:
        assert len(set(digests.values())) == len(digests)


def test_tier_split_preserves_the_ring_budget():
    rng = np.random.default_rng(0)
    for total in (1, 2, 7, 13, 100):
        counts = _tier_counts(rng, total, {"easy": 0.3, "medium": 0.5, "hard": 0.2})
        assert sum(counts.values()) == total


# ---------------------------------------------------------------------------
# Anti-leakage: an injected transaction must be indistinguishable
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def generated(cfg):
    rng = streams(cfg.seed)
    pop = population.build(cfg, rng)
    controls = Controls.from_config(cfg.institution)
    result = typologies.inject(cfg, rng, pop, controls)

    txns: list[pl.DataFrame] = []
    edge_chunks: dict[str, list[pl.DataFrame]] = {}
    tags: list[pl.DataFrame] = []

    from fincrime.behavior import simulate

    def sink(txn, edges):
        txns.append(txn)
        for name, df in edges.items():
            edge_chunks.setdefault(name, []).append(df)

    _, resolved = simulate(cfg, rng, pop, sink, pending=result.pending)
    tags.append(resolved)
    return pop, pl.concat(txns), {k: pl.concat(v) for k, v in edge_chunks.items()}, result, resolved


def test_illicit_transactions_are_interleaved_not_appended(generated):
    """Injected rows must not cluster at the end of the id sequence.

    Ids are assigned in timestamp order, so if illicit transactions were
    appended rather than merged, their ids would sit in a contiguous block and
    "id > N" would be a perfect classifier.
    """
    _, txn, _, _, resolved = generated
    if resolved.height == 0:
        pytest.skip("nothing injected")
    positions = txn.with_row_index("pos").join(resolved, on="txn_id")["pos"].to_numpy()
    # Injected rows should span most of the log, not sit in one block.
    spread = (positions.max() - positions.min()) / txn.height
    assert spread > 0.25, f"injected transactions occupy only {spread:.1%} of the id range"


def test_illicit_transactions_use_classes_the_background_also_uses(generated):
    """A class or channel unique to a typology would be a free feature."""
    _, txn, _, _, resolved = generated
    if resolved.height == 0:
        pytest.skip("nothing injected")
    illicit = txn.join(resolved, on="txn_id")
    legit = txn.join(resolved, on="txn_id", how="anti")
    for column in ("txn_class", "channel", "direction", "currency"):
        unique_to_illicit = set(illicit[column].unique()) - set(legit[column].unique())
        assert not unique_to_illicit, f"{column} values only ever illicit: {unique_to_illicit}"


def test_illicit_memos_match_the_background_format(generated):
    _, txn, _, _, resolved = generated
    if resolved.height == 0:
        pytest.skip("nothing injected")
    illicit = txn.join(resolved, on="txn_id")
    legit = txn.join(resolved, on="txn_id", how="anti")
    assert set(illicit["memo"].unique()) <= set(legit["memo"].unique())


def test_hosts_retain_their_normal_activity(generated):
    """A ring member must not consist only of illicit transactions.

    Otherwise "has no ordinary activity" separates the classes perfectly, and
    the blending requirement in spec §5.3 is not met.
    """
    pop, txn, edges, result, resolved = generated
    if resolved.height == 0:
        pytest.skip("nothing injected")
    smurf_accounts = {
        label.subject_id
        for label in result.labels
        if label.subject_type == "account" and label.role == "smurf"
    }
    per_account = (
        txn.join(edges["txn_from"], left_on="txn_id", right_on="start_id")
        .join(resolved, on="txn_id", how="left")
        .group_by("end_id")
        .agg(
            pl.len().alias("total"),
            pl.col("label_tag").is_not_null().sum().alias("illicit"),
        )
        .filter(pl.col("end_id").is_in(list(smurf_accounts)))
    )
    assert per_account.height > 0
    assert (per_account["illicit"] < per_account["total"]).all(), (
        "some ring members have only illicit activity"
    )
    # And the illicit share should be a minority of their overall behaviour.
    share = (per_account["illicit"] / per_account["total"]).max()
    assert share < 0.5, f"illicit activity is {share:.0%} of a host's stream"


# ---------------------------------------------------------------------------
# Ground truth tables
# ---------------------------------------------------------------------------


def test_labels_resolve_to_real_subjects(generated):
    pop, txn, _, result, resolved = generated
    tables = labels_mod.build(result, resolved)
    label_rows = tables["typology_label"]
    if label_rows.height == 0:
        pytest.skip("nothing injected")

    known = {
        "account": set(pop.account_ids.tolist()),
        "individual": set(pop.tables["individual"]["individual_id"].to_list()),
        "legal_entity": set(pop.tables["legal_entity"]["entity_id"].to_list()),
        "transaction": set(txn["txn_id"].to_list()),
    }
    for subject_type, group in label_rows.group_by("subject_type"):
        key = subject_type[0] if isinstance(subject_type, tuple) else subject_type
        assert set(group["subject_id"].to_list()) <= known[key], f"dangling {key} label"


def test_label_ids_are_unique(generated):
    _, _, _, result, resolved = generated
    tables = labels_mod.build(result, resolved)
    labels = tables["typology_label"]
    assert labels["label_id"].n_unique() == labels.height


def test_every_label_belongs_to_a_declared_ring(generated):
    _, _, _, result, resolved = generated
    tables = labels_mod.build(result, resolved)
    rings = set(tables["ring"]["ring_id"].to_list())
    assert set(tables["typology_label"]["ring_id"].to_list()) <= rings


def test_injection_is_reproducible(cfg):
    def once():
        rng = streams(cfg.seed)
        pop = population.build(cfg, rng)
        result = typologies.inject(cfg, rng, pop, Controls.from_config(cfg.institution))
        return [(r.ring_id, r.knob_digest) for r in result.rings], [
            (label.ring_id, label.role, label.subject_id) for label in result.labels
        ]

    assert once() == once()


def test_background_is_unchanged_by_injection(cfg):
    """Adding typologies must not perturb the background population.

    This is what the independent-stream design in rng.py exists for: if
    injecting rings shifted the population, no two dataset versions would be
    comparable and every calibration number would be measured against a moving
    target.
    """
    rng_a = streams(cfg.seed)
    pop_a = population.build(cfg, rng_a)
    txn_a, _ = simulate_to_frames(cfg, rng_a, pop_a)

    rng_b = streams(cfg.seed)
    pop_b = population.build(cfg, rng_b)
    typologies.inject(cfg, rng_b, pop_b, Controls.from_config(cfg.institution))
    txn_b, _ = simulate_to_frames(cfg, rng_b, pop_b)

    assert pop_a.tables["individual"].equals(pop_b.tables["individual"])
    assert txn_a.equals(txn_b)
