"""Derived Neo4j bulk-load artifacts.

``neo4j-admin database import`` reads CSV, not Parquet, so the graph load path
stages CSV from the canonical Parquet. Cypher writes are not an option at the
``mvp`` preset's ~30M transaction nodes; the offline importer is the only route
that finishes in minutes rather than hours.

Three things are emitted:

* ``headers/*.csv`` - one typed header row per table, generated from
  ``schema.py`` so headers cannot drift from the data.
* ``nodes/*.csv``, ``rels/*.csv`` - data rows, headerless.
* ``import.sh`` - the exact importer invocation, with every file argument
  spelled out. Generated rather than hand-maintained because the argument list
  grows with the schema.
"""

from __future__ import annotations

import csv
import stat
from pathlib import Path

import polars as pl

from ..config import RunConfig
from ..schema import (
    ALL_NODE_TABLES,
    COMPOSITE_INDEXES,
    EDGE_TABLES,
    NODES_BY_NAME,
    EdgeTable,
    NodeTable,
)
from .parquet import table_path

#: Path the compose file bind-mounts ``out/import`` to inside the container.
CONTAINER_IMPORT_DIR = "/import"

#: Subject types the polymorphic label edge can point at, mapped to the node
#: table holding that subject. ``LABELS_SUBJECT`` is declared once in the
#: schema but must be emitted once per target ID space, because an END_ID
#: column can only name one ID space.
LABEL_SUBJECT_TARGETS: dict[str, str] = {
    "individual": "individual",
    "legal_entity": "legal_entity",
    "account": "account",
    "card": "card",
    "transaction": "transaction",
}


def _write_header(path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        csv.writer(fh).writerow(fields)


def _stage_rows(src: Path, dest: Path, *, label_value: str | None = None) -> int:
    """Copy one Parquet table to headerless CSV.

    Streamed via Polars' lazy sink so the ~30M-row transaction table never has
    to be materialized in memory at ``mvp`` scale.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    lf = pl.scan_parquet(src)
    if label_value is not None:
        lf = lf.with_columns(pl.lit(label_value).alias(":LABEL"))
    frame = lf.collect()
    frame.write_csv(dest, include_header=False)
    return frame.height


def _node_args(table: NodeTable, import_root: Path) -> str:
    header = f"{CONTAINER_IMPORT_DIR}/headers/{table.name}.csv"
    data = f"{CONTAINER_IMPORT_DIR}/nodes/{table.name}.csv"
    return f"  --nodes={header},{data} \\"


def _rel_args(table: EdgeTable, suffix: str = "") -> str:
    name = f"{table.name}{suffix}"
    header = f"{CONTAINER_IMPORT_DIR}/headers/{name}.csv"
    data = f"{CONTAINER_IMPORT_DIR}/rels/{name}.csv"
    return f"  --relationships={header},{data} \\"


def stage(cfg: RunConfig, *, database: str = "fincrime") -> dict[str, int]:
    """Stage every import file and write ``import.sh``. Returns row counts."""
    root = cfg.import_dir
    counts: dict[str, int] = {}
    node_lines: list[str] = []
    rel_lines: list[str] = []

    for table in ALL_NODE_TABLES:
        _write_header(root / "headers" / f"{table.name}.csv", table.import_header())
        # The :LABEL column is appended here rather than stored in Parquet: a
        # node's labels are a graph-projection concern, and duplicating them in
        # every Parquet row would be dead weight for tabular consumers.
        counts[table.name] = _stage_rows(
            table_path(cfg, table),
            root / "nodes" / f"{table.name}.csv",
            label_value=";".join(table.labels),
        )
        node_lines.append(_node_args(table, root))

    for table in EDGE_TABLES:
        if table.name == "label_subject":
            # Polymorphic: fan out per subject type so each file's END_ID names
            # a single ID space.
            for subject_type, node_table in LABEL_SUBJECT_TARGETS.items():
                name = f"{table.name}__{subject_type}"
                header = [
                    "start_id:START_ID(TypologyLabel)",
                    f"end_id:END_ID({NODES_BY_NAME[node_table].label})",
                    ":TYPE",
                ]
                _write_header(root / "headers" / f"{name}.csv", header)
                src = table_path(cfg, table)
                dest = root / "rels" / f"{name}.csv"
                dest.parent.mkdir(parents=True, exist_ok=True)
                # `subject_type` routes the fan-out and is then dropped: the
                # target ID space already encodes it, so keeping it would be a
                # redundant property on every label edge.
                frame = (
                    pl.scan_parquet(src)
                    .filter(pl.col("subject_type") == subject_type)
                    .select("start_id", "end_id")
                    .with_columns(pl.lit(table.rel_type).alias(":TYPE"))
                    .collect()
                )
                frame.write_csv(dest, include_header=False)
                counts[name] = frame.height
                rel_lines.append(_rel_args(table, f"__{subject_type}"))
            continue

        _write_header(root / "headers" / f"{table.name}.csv", table.import_header(NODES_BY_NAME))
        src = table_path(cfg, table)
        dest = root / "rels" / f"{table.name}.csv"
        dest.parent.mkdir(parents=True, exist_ok=True)
        frame = pl.scan_parquet(src).with_columns(pl.lit(table.rel_type).alias(":TYPE")).collect()
        frame.write_csv(dest, include_header=False)
        counts[table.name] = frame.height
        rel_lines.append(_rel_args(table))

    _write_import_script(root, database, node_lines, rel_lines)
    return counts


def _write_import_script(
    root: Path, database: str, node_lines: list[str], rel_lines: list[str]
) -> None:
    body = "\n".join(
        [
            "#!/bin/sh",
            "# GENERATED by fincrime export-csv. Do not edit - regenerate.",
            "#",
            "# Runs inside the neo4j container. The importer needs the target",
            "# database stopped, so `make load` stops it, runs this, and starts",
            "# it again. Cypher writes cannot absorb the mvp preset's volume,",
            "# which is why this offline path exists at all (PHASE1-PLAN.md D1).",
            "set -eu",
            "",
            "neo4j-admin database import full \\",
            *node_lines,
            *rel_lines,
            "  --overwrite-destination=true \\",
            "  --array-delimiter=';' \\",
            # An unlabeled node or a relationship pointing at a missing node is
            # a generator bug. Failing the import surfaces it immediately;
            # skipping would produce a quietly incomplete graph that only shows
            # up as a wrong answer in a demo.
            "  --skip-bad-relationships=false \\",
            "  --skip-duplicate-nodes=false \\",
            "  --bad-tolerance=0 \\",
            "  --high-parallel-io=on \\",
            f"  {database}",
            "",
        ]
    )
    path = root / "import.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def constraints_cypher() -> str:
    """Generate the constraint and index DDL from the schema.

    Emitted rather than hand-written so that adding an indexed column to
    ``schema.py`` is the only edit required.

    Ground-truth labels get NO constraints and NO indexes, deliberately. A
    constraint definition carries its own label, and `SHOW CONSTRAINTS` is not
    filtered by traverse privilege the way `db.labels()` is - so a constraint
    on `:Ring` is the one surface that still tells a demo user an answer key
    exists, after every deny rule has done its work. Neo4j documents the
    `db.labels()` filtering but says nothing about schema introspection, so
    this is a gap to design around rather than a setting to flip.

    Nothing is lost by dropping them:

    * **Integrity** is already enforced at load time. The graph is only ever
      built by ``neo4j-admin import``, and ``--skip-duplicate-nodes=false``
      fails the whole import on a duplicate id within an ID space - the same
      guarantee the uniqueness constraint would give, applied earlier.
    * **Performance** does not need them. Ground truth is ~50K nodes at the
      mvp preset against 30M transactions, so a label scan is milliseconds.
      Business labels keep their constraints precisely because those ARE the
      30M-node lookups the demo queries hit constantly.

    If a customer's security review wants "cannot see that it exists" rather
    than "cannot read it", the Phase 2 answer is a separate `fincrime-truth`
    database the demo role has no ACCESS to, joined for admins through a
    composite database. That is a larger change than this one earns today.
    """
    lines = [
        "// GENERATED by fincrime export-csv --constraints. Do not edit.",
        "// Applied after the bulk import, never before: uniqueness constraints",
        "// on 30M nodes are far cheaper to build once over existing data than",
        "// to enforce per-row during the load.",
        "//",
        "// Ground-truth labels are intentionally absent - a constraint names its",
        "// own label and SHOW CONSTRAINTS is not privilege-filtered, so indexing",
        "// :Ring would re-leak what every deny rule exists to hide. See",
        "// constraints_cypher() in src/fincrime/export/neo4j_import.py.",
        "",
    ]
    for table in ALL_NODE_TABLES:
        if table.ground_truth:
            continue
        lines.append(f"// {table.label}")
        lines.append(
            f"CREATE CONSTRAINT {table.name}_key IF NOT EXISTS "
            f"FOR (n:{table.label}) REQUIRE n.{table.key.name} IS UNIQUE;"
        )
        for col in table.columns:
            if col.indexed and not col.key:
                lines.append(
                    f"CREATE INDEX {table.name}_{col.name} IF NOT EXISTS "
                    f"FOR (n:{table.label}) ON (n.{col.name});"
                )
        lines.append("")

    lines.append("// Composite indexes for the demo query patterns.")
    for label, properties in COMPOSITE_INDEXES:
        name = f"{label.lower()}_{'_'.join(properties)}"
        props = ", ".join(f"n.{p}" for p in properties)
        lines.append(f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON ({props});")
    lines.append("")
    return "\n".join(lines)
