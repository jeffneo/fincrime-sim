# Query performance at `mvp` scale

Status: **resolved.** The demo query set returns in 1-9 seconds against the
57.3M-transaction graph, on the laptop container as well as on Aura. This file
records what the problem actually was, because the first diagnosis (2026-09-16,
morning) was wrong in a way worth remembering.

## The wrong answer

The first assessment concluded the bottleneck was memory: 4G heap and 2G page
cache against a 28GB store, so most traversals hit disk. The recommendation was
a smaller preset or a bigger machine.

That was tested directly: the `mvp` graph was dumped and pushed to a 32GB Aura
instance (`make aura-push`). On 8x the memory, with every index ONLINE at 100%,
**four of the six demo queries still did not return inside five minutes.**
Memory was not the constraint.

## The right answer

Two problems, both in the queries rather than the hardware.

**1. The window predicate was outside the index.** Only one range predicate per
index can be served by a seek. The composite indexes added the day before ended
in `amount_usd`, so a query scoped to June still seeked the whole year on
`(channel, direction, amount_usd)` - 3.3M index entries - and then read
`booked_at` off each of those 3.3M nodes to apply the window. Scattered
property reads over a 28GB store are the expensive part, and they were being
done 12x more often than necessary.

The fix is two more composite indexes ending in `booked_at`
(`schema.py: COMPOSITE_INDEXES`). Same query, same data, same hardware:

| CTR same-day cash aggregation, one month | Time |
|---|---|
| `(channel, direction, amount_usd)` only | **222s** |
| plus `(channel, direction, booked_at)` | **20s** first run, **4s** warm |

**2. The structuring query nested two index seeks instead of joining them.**
Both legs of T1 are cheap on their own - the cash leg is an index seek, the
transfer leg is an index seek. The planner was expanding every candidate
account's entire transaction history to connect them: ~387 transactions per
account across tens of thousands of accounts, ~18.8M traversals with a property
read each. `USING JOIN ON feeder` turns that into a hash join on the account
the two seeks share:

| T1 structuring recovery, one month | Time |
|---|---|
| nested (original shape, time-scoped) | **>300s, killed** |
| hash join on `feeder` | **5s** |

## Measured, after the fix

Both targets, same six queries in `neo4j/demo/`, one-month window
(2025-06-01 to 2025-07-01), warm. `local` is the Docker container: 4G heap,
2G page cache, 13.6GB host. `aura` is a 32GB instance.

| Query | local, admin | local, analyst | aura, admin | aura, analyst |
|---|---|---|---|---|
| 01 structuring recovery | 9s | 2s | 5s | 5s |
| 02 layering chains | 3s | 2s | 4s | 3s |
| 03 mule shared device | 2s | 2s | 3s | 3s |
| 04 CNP device reuse | 6s | 5s | 14s | 12s |
| 05 entity drill-down | 2s | 1s | 3s | 2s |
| 06 CTR same-day cash | 2s | 2s | 4s | 4s |

Every number includes ~1.5s of `cypher-shell` JVM startup, so the queries
themselves are faster than this table suggests.

**Warm the queries before demoing.** These are steady-state figures. The first
pass after a `make load` runs against an empty page cache and is far slower -
measured on the M5 reload of the same six queries: 27s, 8s, 16s, 27s, 2s, and
one that did not finish inside 300s, against 7s, 3s, 2s, 7s, 2s, 2s on the
second pass. Nothing is wrong when that happens; every index is ONLINE and the
store is simply not in memory yet. Run the set once after any reload.

The laptop is not slower than Aura here, and on two queries it is faster. That
is partly network - every Aura round trip crosses the internet - and partly
that the working set for an index-driven month-scoped query is small enough to
sit in 2G of page cache. **The mvp preset does not need a managed instance to
demo.** Aura remains a good demo target for other reasons (no local stack, a
URL to hand out, GDS available), not for speed.

Full-year, unscoped, for reference - this is the batch shape, not the demo
shape:

| Query, full year | local, admin | aura, admin |
|---|---|---|
| 01 structuring | **OOM at 89s** (4G heap) | not retried |
| 02 layering | 22s (44 rows) | - |
| 03 mule shared device | 90s (8 rows) | 222s |
| 04 CNP device reuse | 124s | - |
| 06 CTR same-day cash | 77s | - |

T1 over a full year exhausts the 4G transaction memory pool: the hash join has
to hold a year of cash aggregates. A year is not a demo query, but if one is
wanted it needs either more heap or a two-pass form.

## Two corrections to the earlier assessment

**The "5.4x RBAC penalty" does not exist.** It was a cold-start artifact. The
same seek, measured on a quiet database:

| | run 1 | run 2 |
|---|---|---|
| as `neo4j` | 1728ms | 1434ms |
| as `analyst` | 8976ms | 1496ms |

The first query on a new connection pays for authentication and privilege
loading. In steady state the demo role costs nothing measurable, and the
per-query table above shows `analyst` matching or beating `neo4j` throughout.
The D5' design is not a performance compromise.

**Several earlier timings were measuring contention with my own abandoned
query.** A client killed with `TaskStop` leaves the server executing; one such
query ran 42 minutes while later measurements queued behind it. The bench
harness now terminates server-side after every timeout
(`scripts/bench-demo.sh: reap`), and that is the only reason the numbers above
are trustworthy.

## Two query bugs found while doing this

* **T4 returned zero rows at any scale.** The CNP check decorated the card with
  `<-[:FROM]-()`, but a `Card` records its funding account as a *property*
  (`card.account_id`) and has no relationship to it, so the pattern matched
  nothing. Fixed in `neo4j/typology_checks.cypher` and `neo4j/demo/`. The
  detectability report never caught this because it scores from Parquet, not
  from Cypher.
* **The drill-down's default account was dormant.** `ACC-000122985`, the first
  account in the id space, is a savings account with no transactions. Not a
  bug, but worth knowing before demoing an "investigator drill-down" on it.

## GDS at mvp scale

Measured. The short version: **the algorithm is free and the projection is
where the entire cost sits**, and scoping the projection the same way the
Cypher demos are scoped takes it from twelve minutes to three seconds.

| Step | Time | Result |
|---|---|---|
| `gds.graph.project.estimate` for a native Account/Transaction/Device projection | 1s | **6,726 MiB required** — does not fit a 4G heap |
| Cypher projection, all channels, full year | **730s** | 199,282 nodes / 220,206 rels / **16 MiB** |
| Cypher projection, p2p + one month | **3s** | 139,974 nodes / 147,388 rels |
| `gds.wcc.stream` over either | **1–3s** | 89,179 components |
| Admin scoring against ground truth | 61s | — |

Three things worth keeping:

1. **The natural projection is the wrong one.** Projecting the tripartite
   graph natively needs 6.7GB, and it answers the wrong question anyway —
   accounts would be linked through shared transactions as well as shared
   devices. The useful projection is bipartite Account↔Device, which is 16 MiB.
2. **The projection query is a 28M-relationship scan, and that is the 730s.**
   Filtering it to `txn_class = 'p2p_transfer'` inside a window lets it seek
   the `(txn_class, booked_at)` index added in the performance pass above, and
   the same result arrives in three seconds. Materialising
   `(:Account)-[:USED_DEVICE]->(:Device)` so the projection could be native
   was tried and abandoned: `apoc.periodic.iterate` over 93K devices was on
   track for about an hour, because it pays the same scan plus a MERGE per
   pair.
3. **The demo role can do all of it.** Projecting and running WCC as `analyst`
   works — GDS graphs live in a per-user catalog, so nothing touches the
   dataset — which is what D5'' requires of a demo.

What it finds, at the mvp preset with 11 illicit mule rings: sorted by size,
the **top five components are the five mule rings active in the window and
everything of four accounts or fewer is a household**. Recall by tier is
3/3 easy, 5/6 medium, 0/2 hard over a month, and 3/3, 6/6, 0/2 over the year.
The hard tier sets `device_sharing_rate` to `[0.0, 0.15]` — it removes the
signal this method uses — so scoring zero there is the difficulty design
working, not a gap.

## Still open
* **A year-scoped T1 needs more than 4G of transaction memory.** Either raise
  the pool for batch runs or write a two-pass form.
* **The Docker VM disk is nearly full** - 4.8GB free of 59GB, with the mvp
  store at 28GB and each new Transaction index costing ~3.8GB. The first
  attempt at the two new indexes failed with `No space left on device`; they
  had to be built one at a time after pruning the build cache. Another index on
  Transaction will not fit.
