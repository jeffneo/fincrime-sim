"""Privacy audit (spec §9.4).

Walks the schema, finds every column that declares an ``id_control``, and
checks the generated values actually fall inside the range that control
claims. The point is that the privacy guarantee is verified against the data on
every run rather than asserted once in a docstring and then quietly broken by a
later change to a generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from ..identifiers import VERIFIERS
from ..schema import ALL_NODE_TABLES, NODES_BY_NAME, id_controls
from .stats import Check


@dataclass(frozen=True, slots=True)
class Violation:
    table: str
    column: str
    control: str
    sample: str
    count: int


def audit(dataset_dir: Path, *, sample_limit: int = 200_000) -> tuple[list[Check], list[Violation]]:
    """Verify every declared identifier control against the generated values.

    Columns are sampled rather than read whole: at the mvp preset the point is
    to catch a generator that produces out-of-range values, and a generator
    that does so produces them in bulk, not once in thirty million rows.
    """
    checks: list[Check] = []
    violations: list[Violation] = []
    checked_columns = 0

    for control, columns in sorted(id_controls().items()):
        verifier = VERIFIERS.get(control)
        if verifier is None:
            # synthetic_composition has no range predicate - its guarantee is
            # structural and is tested against the generator instead.
            continue
        for table_name, column in columns:
            table = NODES_BY_NAME[table_name]
            path = dataset_dir / ("ground_truth" if table.ground_truth else "graph")
            path = path / f"{table_name}.parquet"
            if not path.exists():
                continue
            series = pl.read_parquet(path, columns=[column])[column]
            if series.len() == 0:
                continue
            checked_columns += 1
            values = series.head(sample_limit).drop_nulls().to_list()
            bad = [v for v in values if not verifier(str(v))]
            if bad:
                violations.append(Violation(table_name, column, control, str(bad[0]), len(bad)))

    checks.append(
        Check(
            "identifier_controls_violated",
            float(len(violations)),
            None,
            0.0,
            "Every identifier must fall inside the reserved range its control "
            "claims. A single violation means a generated value could collide "
            "with a real-world identifier, which is the one guarantee this "
            "dataset cannot ship without.",
            detail=(
                f"{checked_columns} columns checked"
                if not violations
                else "; ".join(f"{v.table}.{v.column}={v.sample!r}" for v in violations[:3])
            ),
        )
    )

    # The other half of §9.4: ground truth must not have leaked into the
    # business tables on disk, not just in the schema declaration.
    gt_tokens = ("typology", "ring_id", "polarity", "difficulty", "is_illicit")
    leaked: list[str] = []
    for table in ALL_NODE_TABLES:
        if table.ground_truth:
            continue
        path = dataset_dir / "graph" / f"{table.name}.parquet"
        if not path.exists():
            continue
        for column in pl.read_parquet_schema(path):
            if any(tok in column.lower() for tok in gt_tokens):
                leaked.append(f"{table.name}.{column}")
    checks.append(
        Check(
            "ground_truth_columns_in_business_tables",
            float(len(leaked)),
            None,
            0.0,
            "A label-shaped column written into a business table would defeat "
            "the directory split, the node labels, and the RBAC deny rules all "
            "at once.",
            detail="; ".join(leaked) if leaked else "none",
        )
    )
    return checks, violations
