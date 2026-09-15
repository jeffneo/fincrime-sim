"""Canonical Parquet output.

Parquet is the authoritative dataset (PHASE1-PLAN.md D1); the graph is derived
from it. Two properties this module is responsible for:

* **Schema validity.** Every table is written with the Arrow schema declared in
  ``schema.py``, even when empty, so a consumer can read the shape of a dataset
  that has not been fully generated yet. That is what makes M0 verifiable
  before any population logic exists.
* **Byte-identical output for a given seed.** Parquet writers embed metadata
  that varies between runs unless suppressed - see ``_write`` below.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..config import RunConfig
from ..schema import ALL_NODE_TABLES, EDGE_TABLES, EdgeTable, NodeTable

#: Subdirectories of the output dir. Ground truth is written to its own
#: directory as well as its own tables, so a release can be shipped without it
#: by omitting one directory rather than by filtering columns.
BUSINESS_DIR = "graph"
GROUND_TRUTH_DIR = "ground_truth"


def table_dir(cfg: RunConfig, *, ground_truth: bool) -> Path:
    return cfg.output.dir / (GROUND_TRUTH_DIR if ground_truth else BUSINESS_DIR)


def table_path(cfg: RunConfig, table: NodeTable | EdgeTable) -> Path:
    return table_dir(cfg, ground_truth=table.ground_truth) / f"{table.name}.parquet"


def _write(path: Path, table: pa.Table, cfg: RunConfig) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression=cfg.output.compression,
        row_group_size=cfg.output.row_group_size,
        # Determinism (D8). Both default to True and both embed run-varying
        # bytes: the writer version string changes with the pyarrow build, and
        # column statistics can differ in float encoding across platforms.
        # Without these two flags, "same seed => byte-identical output" fails
        # for reasons that have nothing to do with the simulation.
        write_statistics=False,
        store_schema=True,
        write_page_index=False,
    )
    return table.num_rows


def empty_table(table: NodeTable | EdgeTable) -> pa.Table:
    """An empty Arrow table with the declared schema."""
    return pa.Table.from_pylist([], schema=table.arrow_schema())


def write_all_empty(cfg: RunConfig) -> dict[str, int]:
    """Write a schema-valid, zero-row dataset.

    This is the M0 deliverable: it proves the schema, config, output layout,
    and Neo4j load path all work end to end before any generator exists, so
    later milestones add rows to a pipeline that is already verified rather
    than debugging both at once.
    """
    counts: dict[str, int] = {}
    for table in (*ALL_NODE_TABLES, *EDGE_TABLES):
        counts[table.name] = _write(table_path(cfg, table), empty_table(table), cfg)
    return counts


def write_table(cfg: RunConfig, table: NodeTable | EdgeTable, data: pa.Table) -> int:
    """Write one table, validating it against the declared schema first.

    Casting rather than trusting the caller means a generator that produces an
    int where the schema says double fails here, with the table and column
    named, instead of producing a dataset that loads into Neo4j with the wrong
    property type.
    """
    expected = table.arrow_schema()
    if data.schema != expected:
        try:
            data = data.cast(expected)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
            raise ValueError(
                f"table {table.name!r} does not match its declared schema and "
                f"cannot be cast to it: {exc}"
            ) from exc
    return _write(table_path(cfg, table), data, cfg)
