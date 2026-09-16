"""Hard-negative invariants.

The point of a hard negative is to be indistinguishable from a typology on the
surface and distinguishable underneath. Both halves need testing: one that it
really does produce the mimicked structure, and one that the legitimate
explanation really is present in the graph. A generator that got either wrong
would still produce labels and still pass every distributional check.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fincrime import hard_negatives, population, typologies
from fincrime.config import load_config
from fincrime.institution import Controls
from fincrime.rng import streams


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/scale-dev.yaml")


@pytest.fixture(scope="module")
def injected(cfg):
    rng = streams(cfg.seed)
    pop = population.build(cfg, rng)
    controls = Controls.from_config(cfg.institution)
    illicit = typologies.inject(cfg, rng, pop, controls)
    claimed = typologies.claimed(illicit, pop)
    benign = hard_negatives.inject(cfg, rng, pop, controls, claimed)
    return cfg, pop, controls, illicit, benign


def test_every_generator_produces_instances(injected):
    """A generator that silently produces nothing is the failure mode here.

    Each one filters its host pool hard, and an over-tight filter shows up as
    zero instances rather than as an error - the cash-business generator built
    1 of a requested 16 before its pool was pre-filtered on staff.
    """
    _, _, _, _, benign = injected
    produced = {r.ring_id.split("-")[1] for r in benign.rings}
    expected = {cls.name for cls in hard_negatives.HARD_NEGATIVES}
    missing = expected - produced
    assert not missing, f"generators produced no instances: {sorted(missing)}"


def test_all_labels_are_hard_negative_polarity(injected):
    _, _, _, _, benign = injected
    assert {label.polarity for label in benign.labels} == {"hard_negative"}


def test_hard_negatives_never_share_a_host_with_a_ring(injected):
    """A subject that is both a crime and a look-alike has no correct answer."""
    _, _, _, illicit, benign = injected
    illicit_accounts = {
        label.subject_id for label in illicit.labels if label.subject_type == "account"
    }
    benign_accounts = {
        label.subject_id for label in benign.labels if label.subject_type == "account"
    }
    assert not (illicit_accounts & benign_accounts)


def test_labels_name_the_typology_they_mimic(injected):
    """Scoring pairs them by typology, so the mimic has to be recorded there."""
    cfg, _, _, _, benign = injected
    declared = {block["mimics"] for block in cfg.typologies["hard_negatives"].values()}
    assert {label.typology for label in benign.labels} <= declared


def test_cash_business_produces_the_structuring_shape(injected):
    """The mimicry has to be real: several depositors, one collector, cash in.

    If the generator produced a different shape, it would not be competing
    with T1 for the detector's attention and the whole exercise would be
    decoration.
    """
    _, pop, _, _, benign = injected
    by_ring: dict[str, list] = {}
    for label in benign.labels:
        if label.role == "cash_intensive_business" and label.subject_type == "account":
            by_ring.setdefault(label.ring_id, []).append(label.subject_id)
    assert by_ring, "no cash-business instances"
    # Depositors plus the business account: at least three accounts per star.
    assert all(len(accounts) >= 3 for accounts in by_ring.values())


def test_cash_business_depositors_are_employees(injected):
    """The distinguishing signal must actually exist in the graph.

    A detector is supposed to be able to tell this from a smurf ring by the
    employment link. If the generator picked strangers, the hard negative
    would be *harder* than a real cash business - which is not realism, it is
    just an unwinnable example.
    """
    _, pop, _, _, benign = injected
    employed = pop.tables["employed_by"]
    pairs = set(zip(employed["start_id"].to_list(), employed["end_id"].to_list(), strict=True))

    by_ring: dict[str, dict[str, list[str]]] = {}
    for label in benign.labels:
        if label.role != "cash_intensive_business" or label.subject_id is None:
            continue
        slot = by_ring.setdefault(label.ring_id, {"individual": [], "legal_entity": []})
        if label.subject_type in slot:
            slot[label.subject_type].append(label.subject_id)

    checked = 0
    for parts in by_ring.values():
        if not parts["legal_entity"] or not parts["individual"]:
            continue
        employer = parts["legal_entity"][0]
        for person in parts["individual"]:
            assert (person, employer) in pairs, (
                f"{person} banks cash for {employer} but is not employed by it"
            )
            checked += 1
    assert checked > 0


def test_cash_business_deposits_are_not_capped_below_the_threshold(injected):
    """A legitimate business does get CTRs filed.

    Capping its deposits under the reporting threshold would make "never
    exceeds 10k" a free feature separating it from a ring that genuinely must
    stay under - inverting the whole point.
    """
    _, _, controls, _, benign = injected
    amounts = np.concatenate(
        [
            chunk["amount"]
            for chunks in benign.pending.by_month.values()
            for chunk in chunks
            if chunk["txn_class"] == "cash_deposit"
        ]
        or [np.array([])]
    )
    if amounts.size == 0:
        pytest.skip("no cash-business deposits")
    assert amounts.max() > controls.ctr_threshold_usd * 0.8


def test_travel_card_transactions_carry_a_card_and_merchant(injected):
    """Card rows without both would be identifiable by the absence alone."""
    _, _, _, _, benign = injected
    for chunks in benign.pending.by_month.values():
        for chunk in chunks:
            if chunk["txn_class"] != "retail_online":
                continue
            assert chunk["card"] is not None
            assert chunk["merchant"] is not None


def test_travel_destinations_are_real_jurisdictions(injected):
    from fincrime.hard_negatives.travel_card import KNOWN_JURISDICTIONS

    _, _, _, _, benign = injected
    destinations = {
        r.knobs.get("destination")
        for r in benign.rings
        if r.knobs.get("generator") == "frequent_traveler"
    }
    assert destinations <= KNOWN_JURISDICTIONS | {None}


def test_transaction_tags_point_at_this_run_s_labels(injected):
    """Guards the tag-rebasing contract in InjectionResult.extend.

    Each generator numbers tags relative to its own label list; the engine
    rebases on merge. Get it wrong and a later instance's transactions attach
    to an earlier instance's ring - which produces a plausible-looking dataset
    with quietly wrong ground truth.
    """
    _, _, _, _, benign = injected
    n_labels = len(benign.labels)
    for chunks in benign.pending.by_month.values():
        for chunk in chunks:
            tags = chunk["label_tag"]
            live = tags[tags >= 0]
            if live.size == 0:
                continue
            assert live.max() < n_labels
            # And every tag must point at a transaction-subject label.
            for tag in np.unique(live):
                assert benign.labels[int(tag)].subject_type == "transaction"


def test_tags_are_distinct_per_instance(injected):
    """Two instances must not share a tag, or their transactions merge rings."""
    _, _, _, _, benign = injected
    seen: dict[int, str] = {}
    for chunks in benign.pending.by_month.values():
        for chunk in chunks:
            tags = chunk["label_tag"]
            for tag in np.unique(tags[tags >= 0]):
                ring = benign.labels[int(tag)].ring_id
                assert seen.setdefault(int(tag), ring) == ring


def test_injection_is_reproducible(cfg):
    def once():
        rng = streams(cfg.seed)
        pop = population.build(cfg, rng)
        controls = Controls.from_config(cfg.institution)
        illicit = typologies.inject(cfg, rng, pop, controls)
        benign = hard_negatives.inject(cfg, rng, pop, controls, typologies.claimed(illicit, pop))
        return [(r.ring_id, r.knob_digest) for r in benign.rings]

    assert once() == once()


def test_hard_negatives_outnumber_positives(injected):
    """Real alert queues are dominated by legitimate oddities (spec §5.4).

    Fewer look-alikes than crimes would make the dataset easier than reality
    no matter how well each individual one is built.
    """
    _, _, _, illicit, benign = injected
    illicit_entities = {
        label.subject_id
        for label in illicit.labels
        if label.subject_type in ("individual", "legal_entity")
    }
    benign_entities = {
        label.subject_id
        for label in benign.labels
        if label.subject_type in ("individual", "legal_entity")
    }
    assert len(benign_entities) > len(illicit_entities)


def test_ground_truth_tables_round_trip(injected):
    from fincrime import labels as labels_mod

    _, _, _, illicit, benign = injected
    combined = illicit
    combined.extend(benign)
    tables = labels_mod.build(
        combined,
        pl.DataFrame(
            {"txn_id": [], "label_tag": []}, schema={"txn_id": pl.String, "label_tag": pl.Int64}
        ),
    )
    rings = set(tables["ring"]["ring_id"].to_list())
    assert set(tables["typology_label"]["ring_id"].to_list()) <= rings
    assert tables["typology_label"]["label_id"].n_unique() == tables["typology_label"].height
