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
from ..schema import (
    ALL_NODE_TABLES,
    EDGE_TABLES,
    EDGES_BY_NAME,
    NODES_BY_NAME,
    EdgeTable,
    NodeTable,
)

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


def _conform(table: NodeTable | EdgeTable, data: object) -> pa.Table:
    """Coerce a caller's table to the declared schema.

    Accepts an Arrow table or anything with ``.to_arrow()`` (Polars). Columns
    are selected by name in schema order and then cast, so a generator that
    builds its columns in a different order, or produces an int where the
    schema says double, is corrected here rather than silently loading into
    Neo4j with the wrong property type. A missing or unexpected column is an
    error naming the table and the column, because that is a generator bug.
    """
    expected = table.arrow_schema()
    arrow = data if isinstance(data, pa.Table) else data.to_arrow()  # type: ignore[union-attr]

    have = set(arrow.column_names)
    want = [f.name for f in expected]
    missing = [c for c in want if c not in have]
    extra = [c for c in arrow.column_names if c not in want]
    if missing or extra:
        raise ValueError(
            f"table {table.name!r} does not match its declared schema: "
            f"missing={missing or 'none'}, unexpected={extra or 'none'}"
        )

    arrow = arrow.select(want)
    try:
        return arrow.cast(expected)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
        raise ValueError(
            f"table {table.name!r} cannot be cast to its declared schema: {exc}"
        ) from exc


def write_table(cfg: RunConfig, table: NodeTable | EdgeTable, data: object) -> int:
    """Write one table in full, conforming it to the declared schema first."""
    return _write(table_path(cfg, table), _conform(table, data), cfg)


class StreamingWriter:
    """Append-as-you-go Parquet writer, one file per table.

    The transaction stream is generated a month at a time. Holding every month
    and concatenating at the end peaked at 15.6GB on the mvp preset — over
    physical RAM on a 16GB machine, so it swapped. Writing each month as it is
    produced keeps the working set to one month regardless of window length,
    which is also what makes the Phase 3 scale-out possible at all.

    Used as a context manager; ``counts`` is valid after close.
    """

    def __init__(self, cfg: RunConfig) -> None:
        self._cfg = cfg
        self._writers: dict[str, pq.ParquetWriter] = {}
        self.counts: dict[str, int] = {}

    def append(self, table: NodeTable | EdgeTable, data: object) -> None:
        arrow = _conform(table, data)
        writer = self._writers.get(table.name)
        if writer is None:
            path = table_path(self._cfg, table)
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = pq.ParquetWriter(
                path,
                table.arrow_schema(),
                compression=self._cfg.output.compression,
                write_statistics=False,
                write_page_index=False,
            )
            self._writers[table.name] = writer
            self.counts.setdefault(table.name, 0)
        writer.write_table(arrow, row_group_size=self._cfg.output.row_group_size)
        self.counts[table.name] += arrow.num_rows

    def close(self) -> dict[str, int]:
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()
        return self.counts

    def __enter__(self) -> StreamingWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def append_transactions(writer: StreamingWriter, txn: object, edges: dict[str, object]) -> None:
    """Sink one month of transactions and their edges into a streaming writer."""
    writer.append(NODES_BY_NAME["transaction"], txn)
    for name, df in edges.items():
        writer.append(EDGES_BY_NAME[name], df)


def write_dataset(
    cfg: RunConfig, tables: dict[str, object], *, skip: set[str] | None = None
) -> dict[str, int]:
    """Write every declared table, using ``tables`` where provided.

    Tables a milestone has not implemented yet are written empty but
    schema-valid, so a partially built dataset is still loadable end to end -
    which is what lets each milestone be verified against the real graph
    instead of only against its own output.

    ``skip`` names tables a StreamingWriter has already written; they must not
    be overwritten with an empty file here.
    """
    skip = skip or set()
    counts: dict[str, int] = {}
    for table in (*ALL_NODE_TABLES, *EDGE_TABLES):
        if table.name in skip:
            continue
        data = tables.get(table.name)
        if data is None:
            counts[table.name] = _write(table_path(cfg, table), empty_table(table), cfg)
        else:
            counts[table.name] = write_table(cfg, table, data)
    return counts
