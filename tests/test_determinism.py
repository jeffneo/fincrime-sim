"""End-to-end reproducibility.

PHASE1-PLAN.md §7 promises "same seed + config => byte-identical Parquet". This
asserts it over the real pipeline rather than over the RNG alone, because the
usual cause of failure is not randomness but the writer: Parquet embeds
creation metadata and statistics that vary per run unless suppressed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fincrime.config import load_config, schema_digest
from fincrime.export import parquet

DEV_CONFIG = Path("config/scale-dev.yaml")


def _digest_dataset(root: Path) -> dict[str, str]:
    """Hash every Parquet file under a dataset dir, keyed by relative path."""
    return {
        str(p.relative_to(root)): hashlib.blake2b(p.read_bytes(), digest_size=16).hexdigest()
        for p in sorted(root.rglob("*.parquet"))
    }


def _generate(tmp_path: Path, *, seed: int | None = None) -> Path:
    cfg = load_config(DEV_CONFIG, seed_override=seed)
    out = tmp_path / "ds"
    cfg = type(cfg)(
        **{
            **{f: getattr(cfg, f) for f in cfg.__dataclass_fields__},
            "output": type(cfg.output)(
                dir=out,
                row_group_size=cfg.output.row_group_size,
                compression=cfg.output.compression,
            ),
        }
    )
    parquet.write_all_empty(cfg)
    return out


def test_two_runs_are_byte_identical(tmp_path):
    a = _digest_dataset(_generate(tmp_path / "a"))
    b = _digest_dataset(_generate(tmp_path / "b"))
    assert a and b, "no parquet files were written"
    assert a == b


def test_every_declared_table_is_written(tmp_path):
    from fincrime.schema import ALL_NODE_TABLES, EDGE_TABLES

    written = {Path(p).stem for p in _digest_dataset(_generate(tmp_path))}
    declared = {t.name for t in (*ALL_NODE_TABLES, *EDGE_TABLES)}
    assert declared == written


def test_ground_truth_is_written_to_its_own_directory(tmp_path):
    """Separate directory is the first of the three separation levels.

    It is what lets a release ship unlabeled by omitting a directory, rather
    than by filtering columns out of every table.
    """
    from fincrime.schema import ground_truth_tables

    root = _generate(tmp_path)
    paths = _digest_dataset(root)
    gt_names = {t.name for t in ground_truth_tables()}
    for rel in paths:
        if Path(rel).stem in gt_names:
            assert rel.startswith(f"{parquet.GROUND_TRUTH_DIR}/"), (
                f"ground-truth table {rel} was written outside {parquet.GROUND_TRUTH_DIR}/"
            )


def test_schema_digest_is_stable_across_calls():
    assert schema_digest() == schema_digest()


def test_config_digest_changes_with_seed():
    """A --seed run must not be mistaken for the preset's default run."""
    base = load_config(DEV_CONFIG)
    other = load_config(DEV_CONFIG, seed_override=base.seed + 1)
    assert base.config_digest != other.config_digest


def test_config_digest_is_order_independent(tmp_path):
    """Reordering YAML keys must not change the config identity.

    Otherwise a cosmetic edit to a config file invalidates every prior dataset
    generated from it.
    """
    import yaml

    original = yaml.safe_load(DEV_CONFIG.read_text())
    shuffled = dict(reversed(list(original.items())))
    shuffled["population"] = dict(reversed(list(original["population"].items())))

    path = tmp_path / "shuffled.yaml"
    # The institution/typology refs are resolved relative to the config's own
    # directory, so point them at the real files by absolute path.
    shuffled["institution"] = str(Path("config/institution.yaml").resolve())
    shuffled["typologies"] = str(Path("config/typologies.yaml").resolve())
    path.write_text(yaml.safe_dump(shuffled))

    reference = dict(original)
    reference["institution"] = str(Path("config/institution.yaml").resolve())
    reference["typologies"] = str(Path("config/typologies.yaml").resolve())
    ref_path = tmp_path / "reference.yaml"
    ref_path.write_text(yaml.safe_dump(reference))

    assert load_config(path).config_digest == load_config(ref_path).config_digest


def test_manifest_records_the_reproducibility_triple(tmp_path):
    from fincrime.config import build_manifest

    cfg = load_config(DEV_CONFIG)
    manifest = build_manifest(cfg, {"individual": 0})
    repro = manifest["reproducibility"]
    assert repro["seed"] == cfg.seed
    assert repro["config_digest"] == cfg.config_digest
    assert repro["schema_digest"] == schema_digest()
    # Fully-labeled releases must say so, and say how the answers are hidden.
    assert manifest["labeling"]["fully_labeled"] is True
    assert "fincrime_demo" in manifest["labeling"]["access_control"]
    # Serializable, since it is written to disk as JSON.
    json.dumps(manifest)


@pytest.mark.parametrize("preset", ["config/scale-dev.yaml", "config/scale-mvp.yaml"])
def test_presets_load(preset):
    cfg = load_config(preset)
    assert cfg.total_entities > 0
    assert cfg.window.days > 0
    assert cfg.institution["controls"]["ctr_threshold_usd"] == 10_000
