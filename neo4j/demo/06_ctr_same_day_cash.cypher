// Regulatory view: accounts whose same-day cash credits aggregate over the
// USD 10,000 CTR threshold (31 CFR 1010.311/1010.313) while every individual
// deposit stays under it. Structuring as the BSA officer sees it, rather than
// as a graph pattern.
//
// A wide aggregation rather than a traversal, and the cheapest of the heavy
// queries: one index seek on (channel, direction, amount_usd), one expansion
// per matching transaction, no second hop.
// Parameters: run PARAMS.cypher first, or pass them yourself.
MATCH (t:Transaction)-[:FROM]->(a:Account)
WHERE t.channel = 'cash'
  AND t.direction = 'credit'
  AND t.amount_usd < 10000.0
  AND t.booked_at >= datetime($window_start)
  AND t.booked_at <  datetime($window_end)
WITH a, date(t.booked_at) AS day, sum(t.amount_usd) AS dayTotal, count(t) AS deposits
WHERE dayTotal > 10000.0 AND deposits >= 2
RETURN a.account_id AS account,
       day,
       deposits,
       round(dayTotal) AS cashUsd
ORDER BY cashUsd DESC
LIMIT 50;
