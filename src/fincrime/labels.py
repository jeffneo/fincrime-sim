"""Ground-truth table construction.

Turns the rings and label specs an injection run produced into the three
ground-truth node tables and their two edge tables.

Transaction labels are resolved here rather than at injection time because
transaction ids do not exist until a month has been assembled. A typology tags
its rows with a label index; the assembler reports which ids carried which tag;
this module expands one tagged spec into one label per transaction.

Everything written here lands under ``ground_truth/`` and carries the
``:GroundTruth`` marker label, which is the single control point the demo role
is denied (PHASE1-PLAN.md D5').
"""

from __future__ import annotations

import polars as pl

from .typologies.base import InjectionResult

_LABEL_SCHEMA = {
    "label_id": pl.String,
    "ring_id": pl.String,
    "subject_id": pl.String,
    "subject_type": pl.String,
    "typology": pl.String,
    "role": pl.String,
    "polarity": pl.String,
    "confidence": pl.Float64,
    "difficulty_tier": pl.String,
}


def build(result: InjectionResult, resolved_tags: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Build the ground-truth tables for one injection run."""
    rows: list[dict[str, object]] = []

    # Tagged transaction labels: one spec covers many transactions, so expand
    # it against the ids the assembler reported for that tag.
    by_tag: dict[int, list[str]] = {}
    if resolved_tags.height:
        for tag, group in resolved_tags.group_by("label_tag"):
            key = tag[0] if isinstance(tag, tuple) else tag
            by_tag[int(key)] = group["txn_id"].to_list()

    for index, spec in enumerate(result.labels):
        if spec.subject_type == "transaction":
            for txn_id in by_tag.get(index, ()):
                rows.append(_row(spec, txn_id))
        elif spec.subject_id is not None:
            rows.append(_row(spec, spec.subject_id))

    labels = (
        pl.DataFrame(rows, schema=_LABEL_SCHEMA) if rows else pl.DataFrame(schema=_LABEL_SCHEMA)
    )
    labels = labels.with_columns(
        pl.format("LBL-{}", pl.int_range(pl.len()).cast(pl.String).str.zfill(12)).alias("label_id")
    )

    per_ring = (
        labels.group_by("ring_id").agg(
            pl.len().alias("member_count"),
            (pl.col("subject_type") == "transaction").sum().alias("illicit_txn_count"),
        )
        if labels.height
        else pl.DataFrame(
            schema={"ring_id": pl.String, "member_count": pl.Int64, "illicit_txn_count": pl.Int64}
        )
    )

    rings = pl.DataFrame(
        {
            "ring_id": [r.ring_id for r in result.rings],
            "typology": [r.typology for r in result.rings],
            "difficulty_tier": [r.difficulty_tier for r in result.rings],
            "knob_digest": [r.knob_digest for r in result.rings],
            "injected_from": [r.injected_from for r in result.rings],
            "injected_to": [r.injected_to for r in result.rings],
        },
        schema={
            "ring_id": pl.String,
            "typology": pl.String,
            "difficulty_tier": pl.String,
            "knob_digest": pl.String,
            "injected_from": pl.Date,
            "injected_to": pl.Date,
        },
    ).join(per_ring, on="ring_id", how="left")

    rings = rings.with_columns(
        pl.col("member_count").fill_null(0),
        pl.col("illicit_txn_count").fill_null(0),
        # Total value moved is a ring-level summary the scorer reports; it is
        # filled in below from the labelled transactions themselves.
        pl.lit(0.0).alias("illicit_amount_usd"),
    )

    return {
        "ring": rings,
        "typology_label": labels,
        "case_narrative": pl.DataFrame(
            schema={
                "narrative_id": pl.String,
                "ring_id": pl.String,
                "summary": pl.String,
                "template_version": pl.String,
            }
        ),
        "label_subject": labels.select(
            pl.col("label_id").alias("start_id"),
            pl.col("subject_id").alias("end_id"),
            "subject_type",
        ),
        "ring_member": labels.select(
            pl.col("label_id").alias("start_id"),
            pl.col("ring_id").alias("end_id"),
        ),
    }


def _row(spec, subject_id: str) -> dict[str, object]:
    return {
        "label_id": "",  # assigned in bulk after collection
        "ring_id": spec.ring_id,
        "subject_id": subject_id,
        "subject_type": spec.subject_type,
        "typology": spec.typology,
        "role": spec.role,
        "polarity": spec.polarity,
        "confidence": spec.confidence,
        "difficulty_tier": spec.difficulty_tier,
    }


def attach_ring_totals(tables: dict[str, pl.DataFrame], txn_amounts: pl.DataFrame) -> None:
    """Fill in each ring's total labelled value, in place.

    Kept separate from :func:`build` because it needs the transaction amounts,
    which live in the business tables — and the ground-truth builder should not
    have to read those to do its main job.
    """
    labels = tables["typology_label"]
    if labels.height == 0 or txn_amounts.height == 0:
        return
    totals = (
        labels.filter(pl.col("subject_type") == "transaction")
        .join(txn_amounts, left_on="subject_id", right_on="txn_id", how="inner")
        .group_by("ring_id")
        .agg(pl.col("amount_usd").sum().alias("total"))
    )
    tables["ring"] = (
        tables["ring"]
        .join(totals, on="ring_id", how="left")
        .with_columns(pl.col("total").fill_null(0.0).alias("illicit_amount_usd"))
        .drop("total")
    )
