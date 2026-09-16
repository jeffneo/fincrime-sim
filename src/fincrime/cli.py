"""Command-line entry point.

M0 wires every stage of the pipeline with the population and typology stages
still empty, so the plumbing - config, seeding, schema, Parquet, CSV staging,
graph load, manifest - is verified before any generator exists. Later
milestones fill in ``generate`` rather than extending the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import polars as pl
import typer
from rich.console import Console
from rich.table import Table

from . import (
    __version__,
    behavior,
    hard_negatives,
    labels,
    narrative,
    population,
    typologies,
)
from . import (
    release as release_mod,
)
from .config import build_manifest, load_config, schema_digest
from .export import datadict, neo4j_import, parquet
from .institution import Controls
from .rng import streams
from .schema import ALL_NODE_TABLES, EDGE_TABLES, GROUND_TRUTH_LABEL
from .validate import detectability, privacy, stats

app = typer.Typer(
    name="fincrime",
    help="Synthetic financial crime simulator.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

PRESETS = {"dev": "config/scale-dev.yaml", "mvp": "config/scale-mvp.yaml"}

ScaleOpt = Annotated[
    str, typer.Option("--scale", "-s", help="Preset name (dev, mvp) or a path to a config file.")
]
SeedOpt = Annotated[int | None, typer.Option("--seed", help="Override the preset's master seed.")]


def _resolve(scale: str, seed: int | None):
    path = Path(PRESETS.get(scale, scale))
    if not path.exists():
        raise typer.BadParameter(
            f"no config at {path}. Presets: {', '.join(PRESETS)}", param_hint="--scale"
        )
    return load_config(path, seed_override=seed)


@app.command()
def generate(scale: ScaleOpt = "dev", seed: SeedOpt = None) -> None:
    """Generate a dataset as Parquet.

    M0 emits a schema-valid zero-row dataset. The population (M1), typology
    (M2-M3) and hard-negative (M4) stages plug in here.
    """
    cfg = _resolve(scale, seed)
    rng = streams(cfg.seed)

    console.print(
        f"[bold]{cfg.name}[/bold] preset · seed {cfg.seed} · "
        f"{cfg.total_entities:,} entities · {cfg.window.days} days "
        f"({cfg.window.start} → {cfg.window.end})"
    )

    with console.status("generating population..."):
        pop = population.build(cfg, rng)
    console.print(f"population · {len(pop.account_ids):,} accounts, {len(pop.card_ids):,} cards")

    controls = Controls.from_config(cfg.institution)

    # Typologies are planned before the stream is generated: they select hosts
    # from the population and emit transactions bucketed by month, which the
    # behavior model folds into each month's buffer before sorting. That is
    # what interleaves illicit activity with the host's own normal activity
    # rather than appending it as a separable block (spec §5.3).
    with console.status("injecting typologies..."):
        injected = typologies.inject(cfg, rng, pop, controls)
    if injected.rings:
        console.print(
            f"typologies · {len(injected.rings)} rings, "
            f"{injected.pending.total:,} illicit transactions"
        )

    # Hard negatives share the same structure and the same claimed-account set,
    # so a subject is never both a crime and a look-alike - there would be no
    # correct answer for a detector that flagged it.
    with console.status("injecting hard negatives..."):
        injected.extend(
            hard_negatives.inject(cfg, rng, pop, controls, typologies.claimed(injected, pop))
        )
    hard_negative_rings = sum(1 for r in injected.rings if r.ring_id.startswith("HN-"))
    if hard_negative_rings:
        console.print(f"hard negatives · {hard_negative_rings} instances")

    # Transactions stream to disk a month at a time; everything else is small
    # enough to write whole. Accumulating the transaction stream first peaked
    # over physical RAM at the mvp preset.
    with parquet.StreamingWriter(cfg) as writer:
        with console.status("simulating background behavior..."):
            n_txn, resolved_tags = behavior.simulate(
                cfg,
                rng,
                pop,
                lambda txn, edges: parquet.append_transactions(writer, txn, edges),
                pending=injected.pending,
            )
        console.print(f"transactions · {n_txn:,} total")
        streamed = writer.close()

    ground_truth = labels.build(injected, resolved_tags)
    txn_facts = (
        pl.scan_parquet(cfg.output.dir / "graph" / "transaction.parquet")
        .select("txn_id", "amount_usd", "booked_at")
        .collect()
    )
    labels.attach_ring_totals(ground_truth, txn_facts.select("txn_id", "amount_usd"))
    # Narratives last: they quote the ring totals, so they have to be built
    # after those are filled in.
    ground_truth["case_narrative"] = narrative.build(
        ground_truth["ring"],
        ground_truth["typology_label"],
        txn_facts.select("txn_id", "booked_at"),
    )
    labeled = ground_truth["typology_label"].height
    console.print(f"ground truth · {labeled:,} labels across {ground_truth['ring'].height} rings")

    counts = parquet.write_dataset(cfg, {**pop.tables, **ground_truth}, skip=set(streamed))
    counts.update(streamed)

    manifest_path = cfg.output.dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(build_manifest(cfg, counts), indent=2) + "\n")

    dict_path = cfg.output.dir / "DATA_DICTIONARY.md"
    dict_path.write_text(datadict.render())

    console.print(f"wrote {len(counts)} tables → [cyan]{cfg.output.dir}[/cyan]")
    console.print(f"manifest → [cyan]{manifest_path}[/cyan]")
    console.print(f"data dictionary → [cyan]{dict_path}[/cyan]")


@app.command("export-csv")
def export_csv(
    scale: ScaleOpt = "dev",
    seed: SeedOpt = None,
    constraints: Annotated[
        bool, typer.Option("--constraints", help="Also regenerate neo4j/constraints.cypher.")
    ] = True,
) -> None:
    """Stage neo4j-admin import CSV from the generated Parquet."""
    cfg = _resolve(scale, seed)
    if not (cfg.output.dir / "manifest.json").exists():
        raise typer.BadParameter(
            f"no dataset at {cfg.output.dir}. Run `fincrime generate --scale {scale}` first.",
            param_hint="--scale",
        )

    counts = neo4j_import.stage(cfg)
    console.print(f"staged {len(counts)} import files → [cyan]{cfg.import_dir}[/cyan]")
    console.print(f"importer script → [cyan]{cfg.import_dir / 'import.sh'}[/cyan]")

    if constraints:
        path = Path("neo4j/constraints.cypher")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(neo4j_import.constraints_cypher())
        console.print(f"constraints → [cyan]{path}[/cyan]")


@app.command()
def validate(
    scale: ScaleOpt = "dev",
    seed: SeedOpt = None,
    detect: Annotated[
        bool,
        typer.Option(
            "--detect/--no-detect",
            help="Run the rules and GBM detectability baselines (slower).",
        ),
    ] = True,
) -> None:
    """Check a generated dataset for statistical fidelity and privacy.

    Exits non-zero if any check falls outside its band, so this is usable as a
    gate rather than only as a report.
    """
    cfg = _resolve(scale, seed)
    if not (cfg.output.dir / "manifest.json").exists():
        raise typer.BadParameter(
            f"no dataset at {cfg.output.dir}. Run `fincrime generate --scale {scale}` first.",
            param_hint="--scale",
        )

    checks = stats.run(cfg.output.dir)
    privacy_checks, violations = privacy.audit(cfg.output.dir)
    checks += privacy_checks

    scores: list = []
    if detect:
        with console.status("running detectability baselines..."):
            detect_checks, scores = detectability.run(
                cfg.output.dir, Controls.from_config(cfg.institution), seed=cfg.seed % 2**31
            )
        checks += detect_checks

    table = Table(title=f"Validation — {cfg.name}", header_style="bold", show_lines=False)
    for column in ("", "check", "value", "expected", "note"):
        table.add_column(column, overflow="fold")
    for c in checks:
        table.add_row(
            "[green]pass[/green]" if c.ok else "[red]FAIL[/red]",
            c.name,
            f"{c.value:,.4g}",
            c.band,
            c.detail,
        )
    console.print(table)

    failed = [c for c in checks if not c.ok]
    for c in failed:
        console.print(f"\n[red]FAIL[/red] [bold]{c.name}[/bold] = {c.value:,.4g}, want {c.band}")
        console.print(f"  {' '.join(c.why.split())}")
    for v in violations[:10]:
        console.print(
            f"  [red]{v.table}.{v.column}[/red] violates {v.control}: "
            f"{v.count:,} bad values, e.g. {v.sample!r}"
        )

    if scores:
        detail = Table(title="Detectability", header_style="bold")
        for column in (
            "typology",
            "rings",
            "pos",
            "look-alikes",
            "rules recall @1/5/10%",
            "easy/med/hard recall",
            "GBM tabular",
            "GBM +graph",
            "lift",
        ):
            detail.add_column(column)
        for s_ in scores:
            detail.add_row(
                s_.typology,
                f"{s_.rings}",
                f"{s_.positives:,}",
                f"{s_.hard_negatives:,}",
                " / ".join(f"{v:.2f}" for v in s_.recall_curve.values()),
                " / ".join(
                    f"{s_.tier_recall.get(t, float('nan')):.2f}" for t in ("easy", "medium", "hard")
                ),
                f"{s_.gbm_auc_pr:.3f}",
                f"{s_.gbm_graph_auc_pr:.3f}",
                f"{s_.lift:.0f}x",
            )
        console.print(detail)

    passed, total = stats.summarize(checks)
    console.print(f"\n{passed}/{total} checks passed")
    if failed:
        raise typer.Exit(1)


@app.command()
def manifest(scale: ScaleOpt = "dev", seed: SeedOpt = None) -> None:
    """Print the reproducibility identity for a config, without generating."""
    cfg = _resolve(scale, seed)
    console.print_json(
        json.dumps(
            {
                "preset": cfg.name,
                "seed": cfg.seed,
                "config_digest": cfg.config_digest,
                "schema_digest": schema_digest(),
            }
        )
    )


@app.command("release")
def release_cmd(
    scale: ScaleOpt = "mvp",
    seed: SeedOpt = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Release directory name. Defaults to the package version."),
    ] = None,
    dump: Annotated[
        Path | None,
        typer.Option("--dump", help="Neo4j dump to include. `make release` produces one first."),
    ] = None,
) -> None:
    """Assemble releases/<version>/ from a generated dataset."""
    cfg = _resolve(scale, seed)
    root, manifest = release_mod.assemble(cfg, version or __version__, dump=dump)
    truth = manifest["ground_truth"]
    total = sum(int(f["bytes"]) for f in manifest["files"])
    console.print(f"release [bold]{manifest['release']}[/bold] → [cyan]{root}[/cyan]")
    console.print(
        f"  {len(manifest['files'])} files, {total / 1e9:.2f} GB · "
        f"{manifest['row_counts'].get('transaction', 0):,} transactions"
    )
    console.print(
        f"  ground truth · {truth['rings_illicit']} rings, "
        f"{truth['rings_hard_negative']} look-alikes, "
        f"{truth['case_narratives']} narratives"
    )
    if manifest["neo4j_dump"] is None:
        console.print(
            "  [yellow]no Neo4j dump included[/yellow] (pass --dump, or use `make release`)"
        )
    sha = manifest.get("git_sha") or ""
    if str(sha).endswith("-dirty"):
        console.print(
            "  [yellow]built from an uncommitted tree[/yellow] - the recorded git_sha "
            "does not reproduce this payload. Commit and re-cut before shipping it."
        )


@app.command("datadict")
def datadict_cmd(
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write here instead of stdout.")
    ] = None,
) -> None:
    """Render the data dictionary from the schema."""
    text = datadict.render()
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        console.print(f"wrote [cyan]{out}[/cyan]")
    else:
        typer.echo(text)


@app.command("schema")
def schema_cmd() -> None:
    """Summarize the declared schema."""
    nodes = Table(title="Node tables", header_style="bold")
    for column in ("table", "labels", "cols", "ground truth"):
        nodes.add_column(column)
    for t in ALL_NODE_TABLES:
        nodes.add_row(
            t.name,
            ", ".join(f":{label}" for label in t.labels),
            str(len(t.columns)),
            "[red]yes[/red]" if t.ground_truth else "",
        )
    console.print(nodes)

    edges = Table(title="Edge tables", header_style="bold")
    for column in ("table", "type", "from", "to", "ground truth"):
        edges.add_column(column)
    for e in EDGE_TABLES:
        edges.add_row(
            e.name,
            e.rel_type,
            e.start,
            e.end,
            "[red]yes[/red]" if e.ground_truth else "",
        )
    console.print(edges)
    console.print(
        f"\nGround truth marker label: [red]:{GROUND_TRUTH_LABEL}[/red] "
        "— denied to fincrime_demo in neo4j/nes-setup.cypher"
    )
    console.print(f"schema digest: [cyan]{schema_digest()}[/cyan]")


@app.command()
def version() -> None:
    """Print the simulator version."""
    console.print(__version__)


if __name__ == "__main__":
    app()
