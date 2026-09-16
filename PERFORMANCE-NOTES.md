# Query performance at `mvp` scale — assessment, 2026-09-16

Status: **unresolved.** The graph is correct and loaded; the detection queries
are not interactive at 57.3M transactions. This records what was measured, what
the cause is, and what has not been tried, so the next session does not repeat
the diagnosis.

## What was measured

Graph: `fincrime` database, 57,339,520 transactions, 189M relationships,
100K customers. Neo4j 2026.07 Enterprise, 4G heap / 2G pagecache (`.env`),
Docker on a 13.6GB machine.

| Step | Rows | As `neo4j` (admin) | Notes |
|---|---|---|---|
| Indexed seek: `txn_class='p2p_transfer' AND amount_usd>1000` | 82,237 | **1.6s** | composite index working |
| …plus expand to both endpoints | 81,898 | **1.7s** | |
| …plus group by collector | 20,514 collectors / 48,510 feeder slots | **1.6s** | |
| Cash side: `channel='cash' AND direction='credit' AND amount_usd<10000`, aggregated per account | 46,940 accounts | **2m 57s** | the bottleneck |
| Full structuring recovery (both sides joined) | — | **>10 min, killed** | |

Same seek, as the `analyst` (demo) role: **8.3s vs 1.6s — 5.4x slower.**

## What is actually slow

Not the index. The composite indexes added this session
(`(txn_class, amount_usd)` and `(channel, direction, amount_usd)`) are ONLINE at
100% and the seek they serve returns 82K rows out of 57.3M in 1.6 seconds.

Two things dominate instead:

1. **Fan-out from accounts.** The original query shape ends by expanding
   `(feeder)<-[:FROM]-(cash:Transaction)` for each of 48,510 feeder accounts.
   At ~387 transactions per account that is ~18.8M relationship traversals,
   each followed by property reads on the Transaction. Starting from the cash
   side instead trades that for a ~2.6M-row index seek plus aggregation, which
   is better but still takes three minutes.

2. **RBAC multiplies everything by ~5x for the demo role.** The `fincrime_demo`
   deny rules mean every node touched needs a visibility check. This is the
   cost of the design in PHASE1-PLAN.md D5' and it is not obviously avoidable
   while the answer key lives in the same database — but it means a three-minute
   admin query is a fifteen-minute analyst query, and the analyst path is the
   demo.

## What this means for the demo

The claims made about *structure* still hold and were verified independently:
the topologies are correct, the Cypher patterns recover the rings, tier
ordering and look-alike ratios are measured, RBAC is enforced. What does not
hold is that these run interactively at `mvp` scale. Treat the demo assessment
given earlier as applying to the `dev` preset (1.4M transactions), where the
same queries return in seconds, until the work below is done.

## Not yet tried, roughly in order of expected payoff

1. **Memory.** The container runs 4G heap / 2G pagecache against a 15GB store.
   The pagecache cannot hold a fraction of the graph, so most traversals are
   hitting disk. This is the most likely single cause of the three-minute
   aggregation and the cheapest thing to change — but the host has 13.6GB
   total, so the honest options are a smaller `mvp` preset or a bigger machine.
2. **Time-scoping.** No analyst queries a full year unfiltered. A one-month
   window should cut the cash side ~12x. The attempt was cut off before it
   returned, so this is untested.
3. **Materializing candidates.** Run the expensive aggregation once as a batch
   job and write a flag or a projection, then make the demo query read it.
   This is what a real deployment would do, and it turns the demo into "here is
   the alert queue" rather than "watch me compute the alert queue".
4. **GDS projections.** Community detection over a projected subgraph avoids
   the Cypher expansion cost entirely and is the more compelling demo anyway.
   Untested at this scale.
5. **Checking whether RBAC defeats index usage specifically**, rather than just
   adding a constant factor. The 5.4x was measured on a seek that returns 82K
   rows; it may behave differently on a large aggregation.

## Process note

Two mistakes worth not repeating:

- `TaskStop` kills the local shell, **not** the Cypher query. A killed client
  leaves the server executing; one abandoned query ran for 42 minutes and
  everything else queued behind it, which made later timings meaningless.
  Terminate server-side with `SHOW TRANSACTIONS` / `TERMINATE TRANSACTIONS`.
- Neo4j Browser / Studio polling `db.schema.visualization()` is expensive on a
  57M-node graph and several of those were running concurrently during
  measurement. Close them before timing anything.
