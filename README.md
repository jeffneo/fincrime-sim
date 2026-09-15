# Synthetic Financial Crime Simulator

Generates large-scale, richly-labeled synthetic datasets representing a full
financial ecosystem — legitimate activity plus embedded financial-crime
typologies — over a Neo4j property graph.

- [Spec](financial-crime-simulator-spec.md) — product and technical spec
- [Phase 1 plan](PHASE1-PLAN.md) — MVP scope, decisions, milestones, acceptance criteria

**Status: M0 complete.** The full pipeline runs end to end — config, seeding,
schema, Parquet, CSV staging, bulk graph load, constraints, RBAC — with the
population and typology generators still empty. Later milestones fill in
`generate` rather than extending the pipeline.

## Quick start

Needs Docker, `uv`, and `gds.license` + `nes.license` at the repo root.

```bash
make up && make nes && make all SCALE=dev
```

Then `make check` for lint and tests, and `make rbac-check` to prove the demo
role cannot read the answer key.

| | |
|---|---|
| Neo4j | http://localhost:7476 — `neo4j` / `fincrimefincrime` |
| Enterprise Studio | http://localhost:8081 |
| Admin (sees labels, runs scoring) | `neo4j` or `scorer` / `scorerscorer` |
| Demo (labels denied) | `analyst` / `analystanalyst` |

Ports live in `.env` — the defaults 7474/7687/8080 collide with the `codekg`
and `ultraviz` stacks on this machine, so this one uses 7476/7689/8081.

## How the answer key is protected

The release ships **fully labeled**, and ground truth is hidden from demo users
at query time rather than stripped from the data. Three separations, so a
detector cannot reach a label by accident:

1. **Separate Parquet directory** — `ground_truth/`, not `graph/`. A release can
   ship unlabeled by omitting a directory rather than filtering columns.
2. **Separate node labels and relationship types** — `:Ring`, `:TypologyLabel`,
   `:CaseNarrative`, all carrying the `:GroundTruth` marker; `LABELS_SUBJECT`
   and `MEMBER_OF_RING` edges.
3. **Neo4j Enterprise RBAC** — `fincrime_demo` is denied traverse and read on
   all of it. `neo4j/nes-setup.cypher` holds the rules.

Ground-truth labels also carry no constraints or indexes, which closes the one
surface deny rules miss: `SHOW CONSTRAINTS` is not privilege-filtered, so a
constraint on `:Ring` would name it. Nothing is lost — `neo4j-admin import`
enforces uniqueness at load time, and ground truth is ~50K nodes against 30M
transactions, so a label scan is milliseconds. Business labels keep their
constraints, so Bloom's schema panel still works for demo users.

Two tests keep this from eroding: `tests/test_schema.py` fails the build if a
ground-truth-shaped column appears on a business table or if a ground-truth
label or relationship type has no matching deny rule; `tests/test_rbac.py`
proves against a live database that the demo role cannot read, count, or
traverse into the answer key — while still being able to read the business
graph and run GDS.

## Commands

```bash
make help
```

| Command | Does |
|---|---|
| `make up` | Neo4j Enterprise + APOC + GDS |
| `make nes` | Enterprise Studio, plus the `fincrime` database and its roles |
| `make generate SCALE=dev` | Generate the dataset as Parquet |
| `make export SCALE=dev` | Stage `neo4j-admin import` CSV + regenerate constraints |
| `make load SCALE=dev` | Bulk-import into Neo4j, apply constraints, print counts |
| `make all SCALE=dev` | All three |
| `make check` | Lint + tests |
| `make rbac-check` | Prove the demo role cannot read ground truth (needs a live DB) |
| `make clean` | **Destructive**: drops the graph and `out/` |

Presets: `SCALE=dev` (10K entities, 3 months) and `SCALE=mvp` (100K, 12 months).

## Layout

```
config/            scale presets, institution controls, typology knobs
src/fincrime/
  schema.py        authoritative schema - Parquet, import headers, constraints,
                   and the data dictionary are all generated from it
  rng.py           seed hierarchy; streams are independent, so adding a typology
                   never perturbs the background population
  config.py        config loading + the reproducibility manifest
  export/          parquet (canonical), neo4j_import (derived), datadict
neo4j/             compose provisioning, RBAC, generated constraints
tests/             schema guards, RNG independence, determinism, live RBAC
out/               generated datasets and import staging (gitignored)
releases/          versioned release payloads
```

## Reproducibility

Every dataset carries a `manifest.json` with the triple that defines it: master
seed, resolved config digest, and schema digest. The same triple regenerates
byte-identical Parquet, asserted in `tests/test_determinism.py`.

## Privacy

Every row is synthetic and no attribute is conditioned on any real record.
Identifiers are drawn from ranges that can never be validly issued — SSNs in
the never-issued 900–999 area prefix, IBANs with invalid check digits, PANs on
a reserved test BIN, phone numbers in the NANP 555-01xx fictitious range, IPs
in RFC 5737 documentation ranges. The generated data dictionary lists every
control.

Ground-truth labels are research and engineering annotations. They are not
legal determinations and must not be presented as real SARs or real case data.
