// T4 card-not-present fraud - one device across cards of unrelated people.
//
// Cross-card reuse, not per-card velocity: a single card's burst is a holiday,
// the same device on cards belonging to unrelated people is not.
//
// The funding account comes from the transaction's own :FROM edge, not from the
// card: a Card records its funding account as a PROPERTY (card.account_id) and
// has no relationship to it. The version of this check in
// typology_checks.cypher decorated the card with `<-[:FROM]-()`, which matches
// nothing and made the whole query return zero rows at any scale.
// Parameters: run PARAMS.cypher first, or pass them yourself.
MATCH (d:Device)<-[:VIA_DEVICE]-(t:Transaction)-[:ON_CARD]->(card:Card)
WHERE t.channel = 'card_cnp'
  AND t.booked_at >= datetime($window_start)
  AND t.booked_at <  datetime($window_end)
WITH d, t, card
MATCH (owner)-[:OWNS]->(:Account)<-[:FROM]-(t)
WITH d,
     count(DISTINCT card) AS cardCount,
     count(DISTINCT owner) AS ownerCount,
     count(t) AS charges
WHERE cardCount >= 3 AND ownerCount >= 3
RETURN d.device_id AS device, cardCount, ownerCount, charges
ORDER BY cardCount DESC
LIMIT 50;
