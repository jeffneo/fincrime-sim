"""Case-narrative invariants (M6 stub).

A narrative is ground truth in prose, so the failure modes are the ones that
matter most for a shipped release: saying something the data does not support,
or leaking into a table a demo user can read.
"""

from __future__ import annotations

import polars as pl
import pytest

from fincrime import hard_negatives, labels, narrative, population, typologies
from fincrime.config import load_config
from fincrime.institution import Controls
from fincrime.rng import streams
from fincrime.schema import NODES_BY_NAME


@pytest.fixture(scope="module")
def built():
    cfg = load_config("config/scale-dev.yaml")
    rng = streams(cfg.seed)
    pop = population.build(cfg, rng)
    controls = Controls.from_config(cfg.institution)
    injected = typologies.inject(cfg, rng, pop, controls)
    injected.extend(
        hard_negatives.inject(cfg, rng, pop, controls, typologies.claimed(injected, pop))
    )
    # The narrative builder needs transaction dates, which only exist once the
    # stream is simulated. Synthesising them from the pending buckets keeps
    # this test off the full behaviour model.
    tables = labels.build(
        injected, pl.DataFrame(schema={"label_tag": pl.Int64, "txn_id": pl.String})
    )
    return tables


def test_every_ring_gets_exactly_one_narrative(built):
    rings = built["ring"]
    text = narrative.build(
        rings,
        built["typology_label"],
        pl.DataFrame(schema={"txn_id": pl.String, "booked_at": pl.Datetime("us")}),
    )
    assert text.height == rings.height
    assert text["ring_id"].n_unique() == rings.height
    assert text["narrative_id"].n_unique() == rings.height
    assert set(text["template_version"].unique()) == {narrative.TEMPLATE_VERSION}


def test_no_unresolved_placeholders(built):
    """A template naming a fact the generator never emitted would ship `{x}`."""
    text = narrative.build(
        built["ring"],
        built["typology_label"],
        pl.DataFrame(schema={"txn_id": pl.String, "booked_at": pl.Datetime("us")}),
    )
    for summary in text["summary"]:
        assert "{" not in summary and "}" not in summary, summary


def test_look_alike_narratives_say_they_are_not_rings(built):
    """The exoneration half. A look-alike narrative that reads like a crime
    summary would invert the answer key for anyone using it as one."""
    rings = built["ring"]
    text = narrative.build(
        rings,
        built["typology_label"],
        pl.DataFrame(schema={"txn_id": pl.String, "booked_at": pl.Datetime("us")}),
    )
    joined = text.join(rings.select("ring_id", "polarity"), on="ring_id")
    benign = joined.filter(pl.col("polarity") == "hard_negative")
    assert benign.height > 0
    for summary in benign["summary"]:
        assert summary.startswith("NOT A RING."), summary
    illicit = joined.filter(pl.col("polarity") == "illicit")
    for summary in illicit["summary"]:
        assert not summary.startswith("NOT A RING."), summary


def test_narratives_state_the_difficulty_tier(built):
    text = narrative.build(
        built["ring"],
        built["typology_label"],
        pl.DataFrame(schema={"txn_id": pl.String, "booked_at": pl.Datetime("us")}),
    )
    for summary in text["summary"]:
        assert "Difficulty tier:" in summary


def test_case_narrative_is_ground_truth_in_the_schema():
    """Narratives describe the answer, so the table has to carry the marker
    label the deny rule targets - otherwise they ship readable."""
    table = NODES_BY_NAME["case_narrative"]
    assert table.ground_truth
    assert "GroundTruth" in table.extra_labels
