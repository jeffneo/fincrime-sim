"""Topology recovery against a live graph (M2 exit criterion).

Two separate claims, tested separately because they fail for different reasons:

1. **The typology is really there.** A Cypher query written from the FATF
   description of structuring — convergence of sub-threshold cash through
   several feeders into one collector — recovers the members of what the
   generator injected. If it cannot, the generator is not producing the
   typology, whatever its labels say.

2. **The demo works without the answer key** (PHASE1-PLAN.md D5''). The
   recovery query runs as ``analyst``, who cannot read ``:GroundTruth``.
   Detection that reads the labels is not a demo.

Needs a loaded graph: ``make all`` then ``make rbac-check``-style invocation
with ``--run-neo4j``.
"""

from __future__ import annotations

import pytest

neo4j = pytest.importorskip("neo4j")

pytestmark = pytest.mark.neo4j

DB = "fincrime"

#: Structure only. No amounts near the threshold (only the easy tier has
#: those), no timing tightness (easy and medium only), no labels. Convergence
#: is the one thing every difficulty tier shares.
RECOVERY = """
MATCH (collector:Account)<-[:TO]-(t:Transaction)-[:FROM]->(feeder:Account)
WHERE t.txn_class = 'p2p_transfer'
WITH collector, collect(DISTINCT feeder) AS feeders
WHERE size(feeders) >= 2
UNWIND feeders AS feeder
MATCH (feeder)<-[:FROM]-(cash:Transaction)
WHERE cash.channel = 'cash'
  AND cash.direction = 'credit'
  AND cash.amount_usd < 10000.0
WITH collector, feeder, count(cash) AS deposits
WHERE deposits >= 2
WITH collector, collect(feeder.account_id) AS smurfs
WHERE size(smurfs) >= 2
RETURN collector.account_id AS collector, smurfs
"""


@pytest.fixture(scope="module")
def admin(neo4j_uri, admin_auth):
    driver = neo4j.GraphDatabase.driver(neo4j_uri, auth=admin_auth)
    yield driver
    driver.close()


@pytest.fixture(scope="module")
def demo(neo4j_uri, demo_auth):
    driver = neo4j.GraphDatabase.driver(neo4j_uri, auth=demo_auth)
    yield driver
    driver.close()


@pytest.fixture(scope="module")
def injected_rings(admin):
    """The answer key, read as admin."""
    with admin.session(database=DB) as s:
        rows = s.run(
            """
            MATCH (r:Ring {typology: 'structuring'})<-[:MEMBER_OF_RING]-(l:TypologyLabel)
            WHERE l.subject_type = 'account'
            RETURN r.ring_id AS ring, r.difficulty_tier AS tier,
                   collect(l.subject_id) AS members
            """
        ).data()
    if not rows:
        pytest.skip("no structuring rings in the loaded graph")
    return rows


@pytest.fixture(scope="module")
def recovered(demo):
    """What the structure query finds, run as the unprivileged demo role."""
    with demo.session(database=DB) as s:
        return s.run(RECOVERY).data()


def test_recovery_query_runs_as_the_demo_role(recovered):
    """D5'': the detection half of the demo needs no access to ground truth."""
    assert recovered, "the structure query found nothing at all"


def test_recovery_finds_most_injected_rings(injected_rings, recovered):
    """At least half of the injected rings should be recoverable by structure.

    Not all of them: the hard tier is built to be missed by a query this
    simple, and a generator whose every ring fell out of one Cypher pattern
    would not be producing a useful dataset. Requiring *most* rings to be found
    is what distinguishes "the typology is present" from "the typology is
    trivial".
    """
    found_members = {m for row in recovered for m in [row["collector"], *row["smurfs"]]}
    hit = [row for row in injected_rings if len(set(row["members"]) & found_members) >= 2]
    ratio = len(hit) / len(injected_rings)
    assert ratio >= 0.5, (
        f"structure query recovered {len(hit)}/{len(injected_rings)} rings "
        f"({ratio:.0%}) - the injected pattern may not match the typology"
    )


def test_easy_tier_is_recovered_more_often_than_hard(injected_rings, recovered):
    """Difficulty tiers must be ordered in reality, not just in the label (D6).

    If hard rings were recovered as often as easy ones, the tier parameters are
    not actually changing how detectable a ring is, and curriculum evaluation
    over this dataset would be measuring nothing.
    """
    found_members = {m for row in recovered for m in [row["collector"], *row["smurfs"]]}
    by_tier: dict[str, list[bool]] = {}
    for row in injected_rings:
        hit = len(set(row["members"]) & found_members) >= 2
        by_tier.setdefault(row["tier"], []).append(hit)

    if not {"easy", "hard"} <= by_tier.keys():
        pytest.skip("need both easy and hard rings to compare tiers")
    easy = sum(by_tier["easy"]) / len(by_tier["easy"])
    hard = sum(by_tier["hard"]) / len(by_tier["hard"])
    assert easy >= hard, f"hard tier ({hard:.0%}) recovered more often than easy ({easy:.0%})"


def test_recovery_precision_is_not_perfect(recovered, injected_rings):
    """The structure query must also pick up legitimate look-alikes.

    A query that returns only true rings would mean the background contains no
    account that legitimately receives converging transfers - and a dataset
    like that makes detection look far easier than it is (spec §5.4).
    """
    true_members = {m for row in injected_rings for m in row["members"]}
    collectors = [row["collector"] for row in recovered]
    false_positives = [c for c in collectors if c not in true_members]
    assert false_positives, (
        "the structure query returned no false positives at all, which means "
        "the background has no legitimate convergence patterns for a detector "
        "to have to rule out"
    )


def test_demo_role_cannot_see_the_scoring_half(demo):
    """The scoring query in typology_checks.cypher must be admin-only."""
    with demo.session(database=DB) as s:
        row = s.run("MATCH (r:Ring) RETURN count(r) AS n").single()
    assert row["n"] == 0
