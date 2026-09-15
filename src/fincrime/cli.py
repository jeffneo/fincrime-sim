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

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import build_manifest, load_config, schema_digest
from .export import datadict, neo4j_import, parquet
from .rng import streams
from .schema import ALL_NODE_TABLES, EDGE_TABLES, GROUND_TRUTH_LABEL

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

    counts = parquet.write_all_empty(cfg)

    # M1+ replaces the line above with the generator stages. Each takes its own
    # named stream, so adding a stage never perturbs an earlier one (rng.py).
    #   population.build(cfg, rng.get("population"))
    #   behavior.simulate(cfg, rng.get("behavior"), pop)
    #   typologies.inject(cfg, rng.get("typologies"), pop)
    #   hard_negatives.inject(cfg, rng.get("hard_negatives"), pop)
    _ = rng.get("population")  # touch the root stream so its derivation is exercised

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
