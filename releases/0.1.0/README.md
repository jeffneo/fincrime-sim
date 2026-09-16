# fincrime synthetic dataset — release 0.1.0

Synthetic financial-crime dataset: **100,000 entities**,
**57,406,190 transactions** over 365 days
(2025-01-01 → 2025-12-31).

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

```bash
# Self-hosted Neo4j Enterprise 2026.07+, database stopped:
neo4j-admin database load fincrime --from-path=neo4j --overwrite-destination=true
# Aura:
neo4j-admin database upload fincrime --from-path=neo4j \
  --to-uri=$AURA_URI --to-user=$AURA_USERNAME --to-password=$AURA_PASSWORD \
  --overwrite-destination=true
```

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
cypher-shell -u analyst -p analystanalyst -d fincrime \
  -P "window_start => '2025-06-01T00:00:00Z'" \
  -P "window_end   => '2025-07-01T00:00:00Z'" \
  -P "account_id   => 'ACC-000043670'" \
  -f queries/01_structuring_recovery.cypher
```

**Warm the set once after loading.** The first pass runs against an empty page
cache and is several times slower; nothing is wrong.

One caveat worth knowing before a live demo: `03_mule_shared_device.cypher`
only returns rings whose activity falls inside the window you pass. Mule rings
run for one to three months each, spread across the year, so a window has to
contain one. An admin can list them:

```cypher
MATCH (r:Ring {typology: 'mule_network', polarity: 'illicit'})
      <-[:MEMBER_OF_RING]-(l:TypologyLabel {subject_type: 'transaction'})
      -[:LABELS_SUBJECT]->(t:Transaction)
RETURN r.ring_id, r.difficulty_tier,
       date(min(t.booked_at)) AS first, date(max(t.booked_at)) AS last
ORDER BY first;
```

## What is injected

51 illicit rings across four typologies, and
1148 **hard negatives** — legitimate patterns built
to be mistaken for them (a cash-intensive business banking its takings, a
payroll treasury account, a cardholder abroad, a holding group moving money
between its own companies). Look-alikes deliberately outnumber rings, because a
real alert queue is dominated by legitimate oddities. They carry
`polarity = 'hard_negative'` and the typology they *mimic*, and must never be
scored as positives.

1199 case narratives (`template m6-stub-1`)
describe one ring each — for a look-alike, the narrative is the exoneration:
what an investigator should find that closes the case.

## Reproducing this dataset

```
seed           20260915
config_digest  1799380c736d372f5cdf1076f284df6f
schema_digest  ecdd151af61c67aefb3d0eadd21b83af
git_sha        ac839b0cacc499bba6bb4c5cfdb619c8383526ad
```

Same triple, byte-identical Parquet. The dataset is not sampled from any real
population, so it can be regenerated rather than archived.
