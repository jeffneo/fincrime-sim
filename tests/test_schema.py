"""Schema invariants.

The leak guard here is the enforcement mechanism for PHASE1-PLAN.md D5': it
makes "ground truth never touches a business node" a test failure instead of a
code-review habit that erodes over four milestones.
"""

from __future__ import annotations

import pytest

from fincrime.schema import (
    ALL_NODE_TABLES,
    EDGE_TABLES,
    GROUND_TRUTH_LABEL,
    LEAK_GUARD_EXEMPTIONS,
    LEAKY_COLUMN_TOKENS,
    NODES_BY_NAME,
    business_tables,
    ground_truth_tables,
)


@pytest.mark.parametrize("table", ALL_NODE_TABLES, ids=lambda t: t.name)
def test_node_table_has_exactly_one_key(table):
    assert table.key is not None


@pytest.mark.parametrize("table", ALL_NODE_TABLES, ids=lambda t: t.name)
def test_node_arrow_schema_matches_columns(table):
    schema = table.arrow_schema()
    assert schema.names == [c.name for c in table.columns]


@pytest.mark.parametrize("table", ALL_NODE_TABLES, ids=lambda t: t.name)
def test_import_header_declares_id_space(table):
    header = table.import_header()
    assert header[0] == f"{table.key.name}:ID({table.label})"
    assert header[-1] == ":LABEL"
    # One header field per column, plus :LABEL.
    assert len(header) == len(table.columns) + 1


@pytest.mark.parametrize("table", EDGE_TABLES, ids=lambda t: t.name)
def test_edge_endpoints_exist(table):
    assert table.start in NODES_BY_NAME, f"{table.name} start {table.start!r} is not a node table"
    assert table.end in NODES_BY_NAME, f"{table.name} end {table.end!r} is not a node table"


@pytest.mark.parametrize("table", EDGE_TABLES, ids=lambda t: t.name)
def test_edge_import_header_declares_both_id_spaces(table):
    header = table.import_header(NODES_BY_NAME)
    assert header[0] == f"start_id:START_ID({NODES_BY_NAME[table.start].label})"
    assert header[1] == f"end_id:END_ID({NODES_BY_NAME[table.end].label})"
    assert header[-1] == ":TYPE"


def test_foreign_keys_resolve():
    for table in ALL_NODE_TABLES:
        for col in table.columns:
            if col.ref:
                assert col.ref in NODES_BY_NAME, (
                    f"{table.name}.{col.name} references unknown table {col.ref!r}"
                )


# ---------------------------------------------------------------------------
# D5' - the leak guard
# ---------------------------------------------------------------------------


def test_no_business_column_leaks_ground_truth():
    """No business table may carry a label-shaped column.

    Ground truth reaches a consumer only via the ground_truth tables, which are
    a separate Parquet directory, separate node labels, and denied to the demo
    role. A `is_mule` column on Individual would silently defeat all three.
    """
    violations: list[str] = []
    for table in business_tables():
        for col in table.columns:
            if (table.name, col.name) in LEAK_GUARD_EXEMPTIONS:
                continue
            lowered = col.name.lower()
            hits = sorted(tok for tok in LEAKY_COLUMN_TOKENS if tok in lowered)
            if hits:
                violations.append(f"{table.name}.{col.name} contains {hits}")
    assert not violations, (
        "ground truth is leaking into the business graph:\n  "
        + "\n  ".join(violations)
        + "\n\nMove it to a ground_truth table, or add a justified entry to "
        "LEAK_GUARD_EXEMPTIONS."
    )


def test_leak_guard_exemptions_are_all_live():
    """Every exemption must name a real column.

    A stale exemption is a hole in the guard that nothing points at, so it
    would go unnoticed until some future column happens to match its name.
    """
    for (table_name, col_name), reason in LEAK_GUARD_EXEMPTIONS.items():
        assert table_name in NODES_BY_NAME, f"exemption names unknown table {table_name!r}"
        names = {c.name for c in NODES_BY_NAME[table_name].columns}
        assert col_name in names, f"exemption names unknown column {table_name}.{col_name}"
        assert reason.strip(), f"exemption {table_name}.{col_name} has no justification"


def test_ground_truth_tables_carry_the_marker_label():
    """The RBAC deny rule targets one label. Every answer must be under it.

    A ground-truth table without :GroundTruth would be fully visible to the
    demo role - the exact failure this whole design exists to prevent.
    """
    for table in ground_truth_tables():
        assert GROUND_TRUTH_LABEL in table.labels, (
            f"{table.name} is ground truth but lacks the :{GROUND_TRUTH_LABEL} "
            "marker label, so neo4j/nes-setup.cypher will not hide it"
        )


def test_business_tables_never_carry_the_marker_label():
    for table in business_tables():
        assert GROUND_TRUTH_LABEL not in table.labels, (
            f"{table.name} is business data but carries :{GROUND_TRUTH_LABEL}, "
            "which would hide it from the demo role entirely"
        )


def test_ground_truth_edges_are_denied_by_type():
    """Ground-truth edges need a dedicated relationship type.

    The deny rules name relationship types explicitly. An answer edge sharing a
    type with a business edge (say, both `OWNS`) could not be denied without
    also breaking the business graph.
    """
    business_types = {e.rel_type for e in EDGE_TABLES if not e.ground_truth}
    for edge in EDGE_TABLES:
        if edge.ground_truth:
            assert edge.rel_type not in business_types, (
                f"ground-truth edge {edge.name} reuses business relationship "
                f"type {edge.rel_type!r}, which cannot be denied selectively"
            )


def test_rbac_denies_every_ground_truth_label():
    """Every ground-truth label needs its own DENY, not just the marker.

    The marker deny hides the data on its own, but `db.labels()` filters per
    label token - so without a per-label rule the demo user can still see that
    a `:Ring` label exists. Catches the realistic drift: a ground-truth table
    added to schema.py without a matching rule in the Cypher.
    """
    from pathlib import Path

    cypher = Path("neo4j/nes-setup.cypher").read_text()
    for table in ground_truth_tables():
        for label in table.labels:
            assert f"NODES {label} " in cypher, (
                f":{label} is a ground-truth label but neo4j/nes-setup.cypher "
                "has no DENY TRAVERSE for it, so CALL db.labels() will "
                "advertise it to the demo role"
            )


def test_rbac_denies_every_ground_truth_relationship_type():
    """The Cypher file must actually deny what the schema marks as ground truth.

    Catches the realistic failure: someone adds a ground-truth edge to
    schema.py and forgets the matching DENY, shipping a dataset whose answers
    are half-visible.
    """
    from pathlib import Path

    cypher = Path("neo4j/nes-setup.cypher").read_text()
    for edge in EDGE_TABLES:
        if edge.ground_truth:
            assert f"RELATIONSHIPS {edge.rel_type}" in cypher, (
                f"{edge.rel_type} is ground truth but neo4j/nes-setup.cypher "
                "has no DENY TRAVERSE for it"
            )
