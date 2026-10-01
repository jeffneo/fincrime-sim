# Synthetic Financial Crime Simulator

Generates large-scale, richly-labeled synthetic datasets representing a full
financial ecosystem — legitimate activity plus embedded financial-crime
typologies — over a Neo4j property graph.

- [Spec](financial-crime-simulator-spec.md) — product and technical spec
- [Phase 1 plan](PHASE1-PLAN.md) — MVP scope, decisions, milestones, acceptance criteria
- [Bloom demo bundle](BLOOM.md) — seven saved search phrases for a five-minute walkthrough
- [Performance notes](PERFORMANCE-NOTES.md) — what is fast, what is not, and why

**Status: M6 — release payload cut.** The pipeline generates a labeled
population with four crime typologies and four hard-negative look-alikes,
validates detectability against two baselines, loads into Neo4j, and assembles
a self-contained release. `make release SCALE=mvp` produces it.

Four typologies (structuring, shell layering, mule networks, CNP fraud) and
four look-alikes, which deliberately outnumber the rings. Every typology clears
the learnability floor; two of them only with graph features, which is the
dataset's argument rather than a shortfall — see
[PHASE1-PLAN.md §7a](PHASE1-PLAN.md).

| Preset | Entities | Window | Transactions | Generation |
|---|---|---|---|---|
| `dev` | 10K | 3 months | 1.42M | ~5s |
| `mvp` | 100K | 12 months | 57.4M | ~3min, 9GB peak, 1.9GB Parquet |

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
| `make backup` | Dump the mvp graph and Studio's saved assets to `backups/latest` |
| `make restore` | Rebuild everything in Docker from `backups/latest` — see below |
| `make release SCALE=mvp` | Assemble `releases/<version>/`; gated on `release-check` |
| `make release-check` | Demo role runs the whole query set; answer key stays unreadable |
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
  narrative.py     template-driven per-ring case narratives (ground truth)
  release.py       assembles releases/<version>/ with a checksummed manifest
  export/          parquet (canonical), neo4j_import (derived), datadict,
                   typologydoc (generated from the generator docstrings)
neo4j/             compose provisioning, RBAC, generated constraints
  demo/            the demo query set - analyst-runnable, time-scoped, timed
  gds/             graph-algorithm walkthrough: project, communities, score
  typology_checks  topology recovery + admin scoring against the answer key
scripts/           bench-demo.sh, which times neo4j/demo against either target
tests/             schema guards, RNG independence, determinism, live RBAC
out/               generated datasets and import staging (gitignored)
releases/          versioned release payloads
backups/           database dumps (gitignored) - see Backup and restore
```

## Backup and restore

The Docker volume is disposable. Nothing in it is the only copy of anything that
cannot be regenerated, with one exception - Studio's saved perspectives, scenes
and graphs - which `make backup` covers.

| What | Where it lives | Gets you back |
|---|---|---|
| Code, config, seed, queries, docs | git (GitHub has whatever has been pushed) | everything below, by regeneration |
| The dataset (Parquet) | `out/mvp/`, `releases/<v>/parquet/` | the graph, via `make export load SCALE=mvp` |
| Neo4j dump of the graph | `backups/latest/fincrime.dump`, `releases/<v>/neo4j/` | the graph, via `make restore` |
| Studio saved assets (Bloom phrases etc.) | `backups/latest/tools-storage.dump` - **only here** | via `make restore` |
| Roles, users, deny rules | `neo4j/nes-setup.cypher` | reapplied by `make restore` |
| `.env`, `gds.license`, `nes.license` | project directory, **gitignored** | not in git or on GitHub; keep your own copy |

After clearing the volume (stop the stack first, `make down`):

```bash
make restore                    # Neo4j, databases, roles, users, dumps, Studio
make release-check SCALE=mvp    # demo role runs the whole set; answer key unreadable
```

`make restore` verifies the checksums first, and refuses to overwrite a database
that already holds data unless you pass `FORCE=1`. If there is no usable dump,
the graph is derivable from what is on disk - slower, but it needs nothing from
Docker:

```bash
make export load SCALE=mvp                  # from out/mvp, ~15 min
make generate export load SCALE=mvp         # from the seed, byte-identical, ~20 min
```

Things to know:

- **Re-run `make backup` after saving anything in Studio.** It stops each
  database for about a minute. Studio's assets are the one thing the Parquet
  cannot rebuild.
- **The first start needs the network.** APOC and GDS are downloaded by the
  container, not baked into the image.
- **Images are pinned** (`neo4j:2026.07.1-enterprise`, Studio by digest). The
  floating `2026.07-enterprise` tag had already stopped existing locally, so a
  fresh `up` would have opened this store with a different server.
- **The Neo4j Enterprise evaluation license is time-limited** (the container
  logged 15 of 30 days remaining on 2026-10-01). Data is unaffected, but the
  server stops starting when it lapses.
- **Backups live on this machine only.** GitHub receives code, never dumps.
- `make restore` has been rehearsed end to end against `fincrime-dev` -
  drop the database, restore, check the roles still hide the answer key - but
  not from a truly empty volume at mvp scale.

## Reproducibility

Every dataset carries a `manifest.json` with the triple that defines it: master
seed, resolved config digest, and schema digest. The same triple regenerates
byte-identical Parquet, asserted in `tests/test_determinism.py`.

## Privacy

Every row is synthetic and no attribute is conditioned on any real record.
Identifiers are drawn from ranges that can never be validly issued — SSNs in
the never-issued 900–999 area prefix, IBANs with invalid check digits, PANs on
a reserved test BIN, phone numbers in the NANP 555-01xx fictitious range, IPs
in 240.0.0.0/4 (RFC 1112, reserved and never routed) and 100.64.0.0/10 (RFC
6598 carrier-grade NAT). The RFC 5737 documentation ranges are the textbook
choice and were rejected deliberately: 762 usable addresses across 100K
entities would put dozens of unrelated customers behind each IP, and shared
infrastructure across unrelated accounts is the headline mule-network signal —
the pool would have manufactured it. The generated data dictionary lists every
control.

Ground-truth labels are research and engineering annotations. They are not
legal determinations and must not be presented as real SARs or real case data.
