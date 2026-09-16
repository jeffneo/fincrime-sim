// T2 shell layering - consecutive wire hops that carry most of their value on.
//
// Window-scoped and floored on amount. The unscoped form in
// typology_checks.cypher joins every wire in the year against every wire that
// follows it within 30 days, which does not return at mvp scale. The floor is
// defensible on its own terms: layering exists to move a sum worth layering,
// and the generator's chains start above USD 25,000.
MATCH (a:Account)<-[:FROM]-(t1:Transaction)-[:TO]->(b:Account)
WHERE t1.channel = 'wire'
  AND t1.direction = 'debit'
  AND t1.amount_usd >= 25000.0
  AND t1.booked_at >= datetime($window_start)
  AND t1.booked_at <  datetime($window_end)
MATCH (b)<-[:FROM]-(t2:Transaction)-[:TO]->(c:Account)
WHERE t2.channel = 'wire'
  AND t2.booked_at > t1.booked_at
  AND t2.booked_at <= t1.booked_at + duration('P30D')
  AND t2.amount_usd > 0.7 * t1.amount_usd
  AND a <> c
RETURN b.account_id AS middleHop,
       count(*) AS chains,
       round(max(t1.amount_usd)) AS largestInUsd
ORDER BY largestInUsd DESC
LIMIT 50;
