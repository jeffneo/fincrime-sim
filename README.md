# Synthetic Financial Crime Simulator

Generates large-scale, richly-labeled synthetic datasets representing a full
financial ecosystem — legitimate activity plus embedded financial-crime
typologies — over a Neo4j property graph.

- [Spec](financial-crime-simulator-spec.md) — product and technical spec
- [Phase 1 plan](PHASE1-PLAN.md) — MVP scope, decisions, milestones, acceptance criteria

**Status: M1 complete.** The pipeline generates a full synthetic population and
its background transaction activity, validates it, and loads it into Neo4j. No
typologies are injected yet — that is M2–M3, and the whole point of M1 is that
the *background* has to be hard to separate from crime before any crime exists.

| Preset | Entities | Window | Transactions | Generation |
|---|---|---|---|---|
| `dev` | 10K | 3 months | 1.37M | ~5s |
| `mvp` | 100K | 12 months | 55.4M | ~3min, 9GB peak, 1.8GB Parquet |

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
| `make validate SCALE=dev` | Statistical fidelity + privacy audit; non-zero exit on failure |
| `make export SCALE=dev` | Stage `neo4j-admin import` CSV + regenerate constraints |
| `make load SCALE=dev` | Bulk-import into Neo4j, apply constraints, print counts |
| `make all SCALE=dev` | Generate, validate, export, load |
| `make check` | Lint + tests |
| `make rbac-check` | Prove the demo role cannot read ground truth (needs a live DB) |
| `make bench TARGET=local` | Time the demo query set; `USER_ROLE=analyst` for the demo role |
| `make aura-push SCALE=mvp` | Dump the graph and upload it to the Aura instance in `.env` |
| `make aura-setup` | Roles, deny rules and demo users on that Aura instance |
| `make aura-bench` | Time the demo set on Aura, as admin and as analyst |
| `make clean` | **Destructive**: drops the graph and `out/` |

Presets: `SCALE=dev` (10K entities, 3 months) and `SCALE=mvp` (100K, 12 months).

The `mvp` graph runs the demo set in seconds on the local container — see
[PERFORMANCE-NOTES.md](PERFORMANCE-NOTES.md) for the measurements and for why
the queries are written the way they are. `make aura-push` exists because a
managed instance is a convenient demo target, not because the laptop is too
slow; it replaces everything in the target instance.

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
  demo/            the demo query set - analyst-runnable, time-scoped, timed
  typology_checks  topology recovery + admin scoring against the answer key
scripts/           bench-demo.sh, which times neo4j/demo against either target
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
