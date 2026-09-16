"""Configuration loading and the run manifest.

A manifest is written next to every dataset and is what makes a release
reproducible: seed, resolved config digest, schema digest, and code version. If
any of those differ between two runs, the datasets are not comparable, and the
manifest is how you find out which one moved.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .schema import ALL_NODE_TABLES, EDGE_TABLES


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} precedes start {self.start}")


@dataclass(frozen=True, slots=True)
class OutputConfig:
    dir: Path
    row_group_size: int = 131_072
    compression: str = "zstd"


@dataclass(frozen=True, slots=True)
class RunConfig:
    """A fully resolved simulation configuration."""

    name: str
    seed: int
    individuals: int
    legal_entities: int
    window: Window
    output: OutputConfig
    institution: dict[str, Any]
    typologies: dict[str, Any]
    #: Digest of the resolved config as loaded, before any defaults are applied
    #: downstream. Part of the reproducibility identity.
    config_digest: str = ""
    source_path: Path | None = field(default=None, compare=False)

    @property
    def total_entities(self) -> int:
        return self.individuals + self.legal_entities

    @property
    def import_dir(self) -> Path:
        """Where CSV is staged for ``neo4j-admin import``.

        Fixed at ``out/import`` because docker-compose bind-mounts exactly that
        path into the container as ``/import``. Keeping it out of the preset's
        output dir means switching presets does not require touching compose.
        """
        return Path("out/import")


def _digest(payload: Any) -> str:
    """Stable digest of a nested structure.

    ``sort_keys`` plus ``default=str`` makes this independent of dict insertion
    order and able to swallow dates, so the same config always digests the same
    regardless of how PyYAML happened to order its mappings.
    """
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def schema_digest() -> str:
    """Digest of the schema definition.

    Changes whenever a column is added, removed, renamed, or retyped - which is
    exactly when previously generated Parquet stops being loadable by the
    current code. Recorded in the manifest so that failure is diagnosable
    rather than mysterious.
    """
    shape = {
        "nodes": {
            t.name: {
                "labels": t.labels,
                "ground_truth": t.ground_truth,
                "columns": [(c.name, str(c.type)) for c in t.columns],
            }
            for t in ALL_NODE_TABLES
        },
        "edges": {
            t.name: {
                "type": t.rel_type,
                "start": t.start,
                "end": t.end,
                "ground_truth": t.ground_truth,
                "columns": [(c.name, str(c.type)) for c in t.columns],
            }
            for t in EDGE_TABLES
        },
    }
    return _digest(shape)


def _git_sha() -> str | None:
    """Current commit, or None if this is not a git checkout.

    Returns None rather than raising: the simulator is usable before the
    directory is a repository, it just cannot claim a code version in the
    manifest.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    if not sha:
        return None

    # Mark an uncommitted tree. A manifest whose git_sha names a commit that
    # does not contain the code that produced the dataset is worse than no sha
    # at all - it reads as traceable and is not. Untracked files count:
    # an untracked module under src/ is exactly the case that breaks
    # reproduction, and out/ and releases/ are gitignored so they do not
    # trip this.
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return sha
    if status.returncode == 0 and status.stdout.strip():
        return f"{sha}-dirty"
    return sha


def load_config(path: str | Path, *, seed_override: int | None = None) -> RunConfig:
    """Load a scale preset, resolving its institution and typology references."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    base = path.parent

    def _resolve(ref: Any) -> dict[str, Any]:
        """Inline a referenced YAML file, or pass an inline mapping through."""
        if isinstance(ref, str):
            return yaml.safe_load((base / ref).read_text())
        if isinstance(ref, dict):
            return ref
        raise TypeError(f"expected a filename or mapping, got {type(ref).__name__}")

    institution = _resolve(raw["institution"])
    typologies = _resolve(raw["typologies"])

    resolved = {
        **raw,
        "institution": institution,
        "typologies": typologies,
        # The override participates in the digest, so a run with --seed is not
        # mistaken for the preset's default run.
        "seed": seed_override if seed_override is not None else raw["seed"],
    }

    out = raw["output"]
    return RunConfig(
        name=raw["name"],
        seed=resolved["seed"],
        individuals=raw["population"]["individuals"],
        legal_entities=raw["population"]["legal_entities"],
        window=Window(start=raw["window"]["start"], end=raw["window"]["end"]),
        output=OutputConfig(
            dir=Path(out["dir"]),
            row_group_size=out.get("row_group_size", 131_072),
            compression=out.get("compression", "zstd"),
        ),
        institution=institution,
        typologies=typologies,
        config_digest=_digest(resolved),
        source_path=path,
    )


def build_manifest(cfg: RunConfig, table_stats: dict[str, int]) -> dict[str, Any]:
    """Assemble the reproducibility manifest for a generated dataset."""
    return {
        "fincrime_version": __version__,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        # The three values that together define reproducibility. Same triple =>
        # byte-identical Parquet, asserted by tests/test_determinism.py.
        "reproducibility": {
            "seed": cfg.seed,
            "config_digest": cfg.config_digest,
            "schema_digest": schema_digest(),
        },
        "preset": cfg.name,
        "config_source": str(cfg.source_path) if cfg.source_path else None,
        "population": {
            "individuals": cfg.individuals,
            "legal_entities": cfg.legal_entities,
            "total_entities": cfg.total_entities,
        },
        "window": {
            "start": cfg.window.start.isoformat(),
            "end": cfg.window.end.isoformat(),
            "days": cfg.window.days,
        },
        "row_counts": dict(sorted(table_stats.items())),
        "labeling": {
            "fully_labeled": True,
            "ground_truth_marker_label": "GroundTruth",
            "access_control": (
                "Ground truth is present in this dataset and is hidden from the "
                "fincrime_demo role by deny rules in neo4j/nes-setup.cypher. "
                "Scoring runs as fincrime_admin."
            ),
        },
        "disclaimer": (
            "Entirely synthetic. Contains no real personal data. Ground-truth "
            "labels are research/engineering annotations, not legal "
            "determinations, and must not be presented as real SARs or case data."
        ),
    }
