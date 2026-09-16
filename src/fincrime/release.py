"""Assemble a release payload (PHASE1-PLAN.md M6, §9.4).

A release is a directory someone else can load without this repository: the
canonical Parquet, a Neo4j dump, the generated documentation, the Cypher a demo
needs, and a manifest that says exactly what produced it.

Two properties the assembly is built around:

* **Self-describing.** Every file is listed in ``MANIFEST.json`` with its size
  and SHA-256, so a payload that arrives incomplete or altered says so rather
  than failing halfway through a demo.
* **Fully labeled.** Ground truth ships *in* the dump and is hidden at query
  time by RBAC (D5'), so the release directory contains the answer key and
  ``neo4j/roles.cypher`` is what keeps a demo user from reading it. Applying
  that file is a required step, not an optional one, and the README says so.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from . import __version__
from .config import RunConfig, schema_digest
from .export import datadict, typologydoc

#: Cypher shipped with the release. The demo set is the point of the payload;
#: the rest is what a recipient needs to stand the graph up.
_CYPHER = {
    "constraints.cypher": "neo4j/constraints.cypher",
    "roles.cypher": "neo4j/aura-setup.cypher",
    "roles-selfhosted.cypher": "neo4j/nes-setup.cypher",
    "typology_checks.cypher": "neo4j/typology_checks.cypher",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        # Large files: the mvp dump is ~4GB, so this is chunked rather than
        # read whole.
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> list[dict[str, object]]:
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.json"):
        files.append(
            {
                "path": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return files


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.parquet")):
        shutil.copy2(path, dest / path.name)


def assemble(
    cfg: RunConfig,
    version: str,
    *,
    releases_dir: Path = Path("releases"),
    dump: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Build ``releases/<version>/`` from a generated dataset.

    ``dump`` is optional because producing it needs a running Neo4j: the
    payload is assembled and the manifest records the dump as absent rather
    than failing, so the documentation half of a release can be rebuilt without
    the stack up. `make release` produces the dump first and passes it in.
    """
    dataset = cfg.output.dir
    manifest_path = dataset / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no dataset at {dataset}; generate it first")
    dataset_manifest = json.loads(manifest_path.read_text())

    root = releases_dir / version
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    _copy_tree(dataset / "graph", root / "parquet" / "graph")
    _copy_tree(dataset / "ground_truth", root / "parquet" / "ground_truth")

    (root / "DATA_DICTIONARY.md").write_text(datadict.render())
    (root / "TYPOLOGIES.md").write_text(typologydoc.render(cfg))

    cypher_dir = root / "neo4j"
    cypher_dir.mkdir(parents=True, exist_ok=True)
    for name, source in _CYPHER.items():
        path = Path(source)
        if path.exists():
            shutil.copy2(path, cypher_dir / name)

    queries = root / "queries"
    queries.mkdir(parents=True, exist_ok=True)
    for path in sorted(Path("neo4j/demo").glob("*.cypher")):
        shutil.copy2(path, queries / path.name)

    if dump is not None and Path(dump).exists():
        shutil.copy2(dump, cypher_dir / "fincrime.dump")

    # Ring and narrative counts, read back from what was actually copied
    # rather than from the generator, so the manifest describes the payload.
    rings = pl.read_parquet(root / "parquet" / "ground_truth" / "ring.parquet")
    narratives = pl.read_parquet(root / "parquet" / "ground_truth" / "case_narrative.parquet")
    by_polarity = dict(rings.group_by("polarity").agg(pl.len().alias("n")).iter_rows())

    manifest: dict[str, object] = {
        "release": version,
        "fincrime_version": __version__,
        "assembled_at": datetime.now(UTC).isoformat(),
        "preset": cfg.name,
        # The triple that regenerates this payload, carried through from the
        # dataset manifest rather than recomputed - a release must record what
        # produced it, not what the working tree says now.
        "reproducibility": dataset_manifest["reproducibility"],
        "git_sha": dataset_manifest.get("git_sha"),
        "schema_digest_now": schema_digest(),
        "window": dataset_manifest["window"],
        "population": dataset_manifest["population"],
        "row_counts": dataset_manifest["row_counts"],
        "ground_truth": {
            "rings_illicit": int(by_polarity.get("illicit", 0)),
            "rings_hard_negative": int(by_polarity.get("hard_negative", 0)),
            "case_narratives": narratives.height,
            "narrative_template": (
                narratives["template_version"][0] if narratives.height else None
            ),
        },
        "neo4j_dump": "neo4j/fincrime.dump" if (cypher_dir / "fincrime.dump").exists() else None,
        "files": [],
    }

    # README first, then inventory once. Hashing is the expensive part of the
    # assembly - the mvp payload is ~6GB - so it runs after every file the
    # inventory should cover exists, rather than once here and again below.
    (root / "README.md").write_text(_readme(cfg, version, manifest))
    manifest["files"] = _inventory(root)
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return root, manifest


def _readme(cfg: RunConfig, version: str, manifest: dict) -> str:
    counts = manifest["row_counts"]
    truth = manifest["ground_truth"]
    repro = manifest["reproducibility"]
    dump = manifest["neo4j_dump"]
    total_txn = counts.get("transaction", 0)

    load_block = (
        """```bash
# Self-hosted Neo4j Enterprise 2026.07+, database stopped:
neo4j-admin database load fincrime --from-path=neo4j --overwrite-destination=true
# Aura:
neo4j-admin database upload fincrime --from-path=neo4j \\
  --to-uri=$AURA_URI --to-user=$AURA_USERNAME --to-password=$AURA_PASSWORD \\
  --overwrite-destination=true
```"""
        if dump
        else "_This payload ships without a Neo4j dump; load the Parquet directly._"
    )

    return f"""# fincrime synthetic dataset — release {version}

Synthetic financial-crime dataset: **{cfg.total_entities:,} entities**,
**{total_txn:,} transactions** over {manifest["window"]["days"]} days
({manifest["window"]["start"]} → {manifest["window"]["end"]}).

Every row is synthetic. No attribute is conditioned on any real record, and
identifiers are drawn from ranges that can never be validly issued — SSNs in
the never-issued 900–999 area prefix, IBANs with invalid check digits, PANs on
a reserved test BIN. See `DATA_DICTIONARY.md`.

## What is in here

| Path | Contents |
|---|---|
| `parquet/graph/` | The business data. Canonical form. |
| `parquet/ground_truth/` | The answer key: rings, labels, case narratives. |
| `neo4j/fincrime.dump` | Graph form of the same data, ready to load. |
| `neo4j/constraints.cypher` | Constraints and indexes. Already inside the dump. |
| `neo4j/roles.cypher` | **Required.** Roles, deny rules, demo users — single-database. |
| `neo4j/roles-selfhosted.cypher` | The same, for a multi-database server. |
| `neo4j/typology_checks.cypher` | Topology recovery plus admin-only scoring. |
| `queries/` | The demo query set. Runs as the demo role; touches no ground truth. |
| `TYPOLOGIES.md` | What is injected, how difficulty tiers are built, and the knob values. |
| `MANIFEST.json` | Row counts, checksums, and the triple that regenerates this. |

## The answer key is in this payload

This release ships **fully labeled**. Ground truth is not stripped out; it is
hidden at query time by Neo4j RBAC. Every ground-truth node carries the
`:GroundTruth` marker label, and `neo4j/roles.cypher` denies the demo role
traverse and read on it — one rule covering the whole answer key.

**Applying `roles.cypher` is a required step.** Load the dump without it and a
demo user can read `:Ring`, `:TypologyLabel` and `:CaseNarrative` directly.

- `analyst` / `analystanalyst` — the demo role. Reads the whole business graph,
  denied the answer key.
- `scorer` / `scorerscorer` — admin. Sees everything, runs the scoring half of
  `typology_checks.cypher`.

Change both passwords before exposing the instance to anything.

## Loading

{load_block}

Then, against the `system` database:

```bash
cypher-shell -u neo4j -p <password> -d system -f neo4j/roles.cypher
```

## Running the demo

The queries in `queries/` are time-scoped and take `window_start` and
`window_end` parameters. A one-month window returns in seconds at this scale;
an unscoped year does not, which is a property of the query shape rather than
of the hardware.

```bash
cypher-shell -u analyst -p analystanalyst -d fincrime \\
  -P "window_start => '2025-10-01T00:00:00Z'" \\
  -P "window_end   => '2025-11-01T00:00:00Z'" \\
  -P "account_id   => 'ACC-000043670'" \\
  -f queries/01_structuring_recovery.cypher
```

October is a good default window at this seed: all six queries return rows in
it. Three mule rings overlap it, which matters for the caveat below.

**Warm the set once after loading.** The first pass runs against an empty page
cache and is several times slower; nothing is wrong.

One caveat worth knowing before a live demo: `03_mule_shared_device.cypher`
only returns rings whose activity falls inside the window you pass. Mule rings
run for one to three months each, spread across the year, so a window has to
contain one. An admin can list them:

```cypher
MATCH (r:Ring {{typology: 'mule_network', polarity: 'illicit'}})
      <-[:MEMBER_OF_RING]-(l:TypologyLabel {{subject_type: 'transaction'}})
      -[:LABELS_SUBJECT]->(t:Transaction)
RETURN r.ring_id, r.difficulty_tier,
       date(min(t.booked_at)) AS first, date(max(t.booked_at)) AS last
ORDER BY first;
```

## What is injected

{truth["rings_illicit"]} illicit rings across four typologies, and
{truth["rings_hard_negative"]} **hard negatives** — legitimate patterns built
to be mistaken for them (a cash-intensive business banking its takings, a
payroll treasury account, a cardholder abroad, a holding group moving money
between its own companies). Look-alikes deliberately outnumber rings, because a
real alert queue is dominated by legitimate oddities. They carry
`polarity = 'hard_negative'` and the typology they *mimic*, and must never be
scored as positives.

{truth["case_narratives"]} case narratives (`template {truth["narrative_template"]}`)
describe one ring each — for a look-alike, the narrative is the exoneration:
what an investigator should find that closes the case.

## Reproducing this dataset

```
seed           {repro["seed"]}
config_digest  {repro["config_digest"]}
schema_digest  {repro["schema_digest"]}
git_sha        {manifest["git_sha"]}
```

Same triple, byte-identical Parquet. The dataset is not sampled from any real
population, so it can be regenerated rather than archived.
"""
