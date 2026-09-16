"""Data dictionary generation.

Spec §7 requires a data dictionary shipped with every release. Generated from
``schema.py`` rather than written by hand: a hand-maintained dictionary is
wrong within two milestones, and a wrong dictionary is worse than none for a
dataset whose whole purpose is reproducible benchmarking.
"""

from __future__ import annotations

from ..schema import (
    EDGE_TABLES,
    business_tables,
    ground_truth_tables,
    id_controls,
)

_ID_CONTROL_NOTES: dict[str, str] = {
    "never_issued_ssn": "SSN area prefix 900-999, never issued by the SSA.",
    "unassigned_ein": "EIN prefix outside the IRS's assigned campus ranges.",
    "reserved_rssd": "RSSD id above the Federal Reserve's issued range.",
    "reserved_registration": "Registry format no real state registry issues.",
    "invalid_checkdigit_iban": "IBAN with a deliberately invalid ISO 7064 check digit.",
    "reserved_test_bin": "Luhn-valid PAN on a reserved test BIN, not routable on any network.",
    "reserved_555_range": "NANP 555-0100 to 555-0199, reserved for fictitious use.",
    "reserved_ipv4_space": (
        "IPv4 from 240.0.0.0/4 (RFC 1112, reserved and never allocated) or "
        "100.64.0.0/10 (RFC 6598 carrier-grade NAT, never publicly routed). "
        "RFC 5737 documentation ranges are deliberately NOT used: they offer "
        "only 762 addresses, which at 100K entities would force dozens of "
        "unrelated customers onto each IP and manufacture the exact "
        "shared-infrastructure signal the mule typology is detected by."
    ),
    "reserved_house_number": (
        "House number >= 900000, far above anything US street addressing "
        "issues, so the line cannot coincide with a real deliverable address "
        "while city/state/ZIP stay geographically realistic."
    ),
    "synthetic_composition": (
        "Composed from generated parts. Not sampled from any real record; any "
        "collision with a real name is coincidental and every other attribute "
        "of the row is independent of it."
    ),
}


def render() -> str:
    """Render the full data dictionary as Markdown."""
    out: list[str] = [
        "# Data Dictionary",
        "",
        "Generated from `src/fincrime/schema.py`. Do not edit by hand.",
        "",
        "## Reading this dataset",
        "",
        "The canonical form is Parquet under `graph/` (business data) and",
        "`ground_truth/` (labels). The Neo4j graph is derived from it.",
        "",
        "**Ground truth is separated at three levels:** its own Parquet",
        "directory, its own node labels, and the `:GroundTruth` marker label",
        "that Neo4j RBAC denies to the `fincrime_demo` role. A detector built",
        "on this dataset cannot reach a label by accident - it has to go",
        "looking in a different directory or authenticate as a different user.",
        "",
        "## Privacy",
        "",
        "Every row is synthetic. No attribute is conditioned on any real",
        "record. Identifiers are additionally drawn from ranges that can never",
        "be validly issued, so no value here can collide with a real-world",
        "identifier:",
        "",
        "| Control | Guarantee | Columns |",
        "|---|---|---|",
    ]
    for control, cols in sorted(id_controls().items()):
        note = _ID_CONTROL_NOTES.get(control, "")
        where = ", ".join(f"`{t}.{c}`" for t, c in cols)
        out.append(f"| `{control}` | {note} | {where} |")

    out += [
        "",
        "Ground-truth labels are research and engineering annotations. They are",
        "not legal determinations and must not be presented as real SARs or",
        "real case data.",
        "",
        "## Business graph",
        "",
    ]
    out += _tables(business_tables())

    out += [
        "## Ground truth",
        "",
        "Hidden from the `fincrime_demo` role. Scoring runs as `fincrime_admin`.",
        "",
    ]
    out += _tables(ground_truth_tables())

    out += [
        "## Relationships",
        "",
        "| Type | From | To | Ground truth | Description |",
        "|---|---|---|---|---|",
    ]
    for edge in EDGE_TABLES:
        gt = "yes" if edge.ground_truth else ""
        doc = " ".join(edge.doc.split())
        out.append(f"| `{edge.rel_type}` | `{edge.start}` | `{edge.end}` | {gt} | {doc} |")
    out.append("")

    for edge in EDGE_TABLES:
        if not edge.columns:
            continue
        out += [f"### `{edge.rel_type}` ({edge.start} → {edge.end}) properties", ""]
        out += ["| Column | Type | Description |", "|---|---|---|"]
        for col in edge.columns:
            out.append(f"| `{col.name}` | {col.type} | {' '.join(col.doc.split())} |")
        out.append("")

    return "\n".join(out)


def _tables(tables: list) -> list[str]:
    out: list[str] = []
    for table in tables:
        out += [
            f"### `:{table.label}` — table `{table.name}`",
            "",
            " ".join(table.doc.split()),
            "",
            f"Labels: {', '.join(f'`:{label}`' for label in table.labels)}",
            "",
            "| Column | Type | Key | Description |",
            "|---|---|---|---|",
        ]
        for col in table.columns:
            key = "PK" if col.key else ("FK" if col.ref else "")
            doc = " ".join(col.doc.split())
            out.append(f"| `{col.name}` | {col.type} | {key} | {doc} |")
        out.append("")
    return out
