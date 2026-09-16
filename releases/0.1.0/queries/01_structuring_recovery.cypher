// T1 structuring - recover the star topology into a collector.
//
// Runnable as `analyst`: no :GroundTruth is touched. Same signal as the T1
// check in typology_checks.cypher - convergence of peer transfers whose
// senders funded themselves with sub-threshold cash - but written so it
// returns in seconds at mvp scale rather than not at all.
//
// Three things make the difference, measured on a 32GB Aura instance holding
// 57.3M transactions (PERFORMANCE-NOTES.md):
//
// * Both sides are driven from an index. The cash leg seeks
//   (channel, direction, booked_at), the transfer leg (txn_class, booked_at).
//   Whichever side drives, the other must otherwise be found by expanding
//   every candidate account's whole history - ~387 transactions each over tens
//   of thousands of accounts, ~18.8M traversals.
// * USING JOIN ON feeder makes the planner hash-join the two seeks on the
//   account they share. Without the hint it nests them and the query does not
//   return.
// * The window is part of the index, not a filter applied after it. Scoping to
//   a month with only (channel, direction, amount_usd) available still reads
//   booked_at off 3.3M scattered nodes; that one difference was 222s vs 20s on
//   the CTR aggregation.
MATCH (cash:Transaction)-[:FROM]->(feeder:Account)<-[:FROM]-(t:Transaction)-[:TO]->(collector:Account)
USING JOIN ON feeder
WHERE cash.channel = 'cash'
  AND cash.direction = 'credit'
  AND cash.amount_usd < 10000.0
  AND cash.booked_at >= datetime($window_start)
  AND cash.booked_at <  datetime($window_end)
  AND t.txn_class = 'p2p_transfer'
  AND t.booked_at >= datetime($window_start)
  AND t.booked_at <  datetime($window_end)

// The join produces one row per (cash deposit x onward transfer), so the cash
// total has to be de-duplicated by deposit identity before it is summed.
WITH collector, feeder,
     count(DISTINCT cash) AS deposits,
     collect(DISTINCT [elementId(cash), cash.amount_usd]) AS cashRows
WHERE deposits >= 2

WITH collector,
     collect(DISTINCT [feeder.account_id,
                       reduce(s = 0.0, c IN cashRows | s + c[1])]) AS placements
WHERE size(placements) >= 2
RETURN collector.account_id AS collector,
       size(placements) AS smurfCount,
       round(reduce(s = 0.0, p IN placements | s + p[1])) AS placedUsd
ORDER BY smurfCount DESC, placedUsd DESC
LIMIT 50;
