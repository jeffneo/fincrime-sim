"""RBAC enforcement against a live database.

The release ships fully labeled and relies on Neo4j Enterprise deny rules to
keep the answers away from demo users (PHASE1-PLAN.md D5'). That is a claim
about a running system, not about a Cypher file, so it is tested against one.

These tests seed their own probe nodes, so they are meaningful at M0 before any
dataset exists. Requires `make up && make nes`; run via `make rbac-check`.
"""

from __future__ import annotations

import pytest

neo4j = pytest.importorskip("neo4j")

pytestmark = pytest.mark.neo4j

DB = "fincrime"
PROBE_RING = "rbac-probe-ring"
PROBE_LABEL = "rbac-probe-label"
PROBE_ACCOUNT = "rbac-probe-account"


@pytest.fixture(scope="module")
def seeded(neo4j_uri, admin_auth):
    """Plant one business node and one ground-truth node linked to it."""
    driver = neo4j.GraphDatabase.driver(neo4j_uri, auth=admin_auth)
    with driver.session(database=DB) as s:
        s.run(
            """
            MERGE (a:Account {account_id: $acct})
              SET a.account_type = 'checking'
            MERGE (r:Ring:GroundTruth {ring_id: $ring})
              SET r.typology = 'structuring', r.difficulty_tier = 'medium'
            MERGE (l:TypologyLabel:GroundTruth {label_id: $label})
              SET l.typology = 'structuring', l.polarity = 'illicit',
                  l.subject_id = $acct, l.subject_type = 'account'
            MERGE (l)-[:LABELS_SUBJECT]->(a)
            MERGE (l)-[:MEMBER_OF_RING]->(r)
            """,
            acct=PROBE_ACCOUNT,
            ring=PROBE_RING,
            label=PROBE_LABEL,
        )
    yield driver
    with driver.session(database=DB) as s:
        s.run(
            """
            MATCH (n) WHERE n.account_id = $acct OR n.ring_id = $ring
                         OR n.label_id = $label
            DETACH DELETE n
            """,
            acct=PROBE_ACCOUNT,
            ring=PROBE_RING,
            label=PROBE_LABEL,
        )
    driver.close()


@pytest.fixture
def demo(neo4j_uri, demo_auth, seeded):
    driver = neo4j.GraphDatabase.driver(neo4j_uri, auth=demo_auth)
    yield driver
    driver.close()


def _one(driver, cypher, **params):
    with driver.session(database=DB) as s:
        return s.run(cypher, **params).single()


# ---------------------------------------------------------------------------
# The admin role must see everything, or scoring is impossible.
# ---------------------------------------------------------------------------


def test_admin_can_read_ground_truth(seeded):
    row = _one(seeded, "MATCH (r:Ring {ring_id: $id}) RETURN r.typology AS t", id=PROBE_RING)
    assert row is not None and row["t"] == "structuring"


def test_admin_can_traverse_from_label_to_subject(seeded):
    row = _one(
        seeded,
        """
        MATCH (l:TypologyLabel {label_id: $id})-[:LABELS_SUBJECT]->(a:Account)
        RETURN a.account_id AS acct
        """,
        id=PROBE_LABEL,
    )
    assert row is not None and row["acct"] == PROBE_ACCOUNT


# ---------------------------------------------------------------------------
# The demo role must not.
# ---------------------------------------------------------------------------


def test_demo_cannot_see_ground_truth_nodes(demo):
    assert _one(demo, "MATCH (r:Ring {ring_id: $id}) RETURN r", id=PROBE_RING) is None
    assert _one(demo, "MATCH (l:TypologyLabel) RETURN l LIMIT 1") is None
    assert _one(demo, f"MATCH (g:{'GroundTruth'}) RETURN g LIMIT 1") is None


def test_demo_cannot_count_ground_truth(demo):
    """A count is a leak too.

    Denied traverse makes the nodes invisible to matching, so the count must be
    zero rather than an error - knowing how many rings exist would size the
    answer key even without reading it.
    """
    row = _one(demo, "MATCH (r:Ring) RETURN count(r) AS n")
    assert row["n"] == 0


def test_demo_cannot_reach_ground_truth_by_traversal(demo):
    """The path that would otherwise work: start from a visible node.

    Relationship-type denies on LABELS_SUBJECT and MEMBER_OF_RING close the
    back door of walking into the answer key from a business node the demo
    user can legitimately see.
    """
    row = _one(
        demo,
        """
        MATCH (a:Account {account_id: $acct})
        OPTIONAL MATCH (a)<-[r]-(x)
        RETURN count(r) AS rels
        """,
        acct=PROBE_ACCOUNT,
    )
    assert row["rels"] == 0


def test_demo_can_read_the_business_graph(demo):
    """The deny rules must not have broken the demo itself.

    Over-broad denies that hide business data are the other failure mode, and
    a silent one - the demo just returns nothing.
    """
    row = _one(
        demo,
        "MATCH (a:Account {account_id: $acct}) RETURN a.account_type AS t",
        acct=PROBE_ACCOUNT,
    )
    assert row is not None and row["t"] == "checking"


def test_demo_cannot_write(demo):
    with pytest.raises(neo4j.exceptions.Neo4jError):
        _one(demo, "CREATE (:Account {account_id: 'rbac-probe-illegal-write'})")


def test_demo_has_gds(demo):
    """Investigators need GDS - community detection IS the demo.

    Denying it to protect the answer key would remove the thing being
    demonstrated.
    """
    with demo.session(database=DB) as s:
        version = s.run("RETURN gds.version() AS v").single()["v"]
    assert version


def test_ground_truth_labels_are_hidden_from_schema_introspection(demo):
    """db.labels() filters by traverse privilege, so the answer key should not
    be advertised there. Constraint listings still name the labels, which is
    accepted and documented in neo4j/nes-setup.cypher - the data is what is
    protected.
    """
    with demo.session(database=DB) as s:
        labels = {r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label")}
    assert "GroundTruth" not in labels
    assert "Ring" not in labels
    assert "TypologyLabel" not in labels
