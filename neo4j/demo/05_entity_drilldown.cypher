// Investigator drill-down: one account's largest counterparties, both ways.
//
// What an analyst does after an alert, and the query shape that should be
// interactive at any scale - a point lookup on the account key, then bounded
// expansion. Measured at 1-2s.
//
// Both directions on purpose. The accounts the other queries surface are
// *collectors*: money converges on them and leaves in dribs, so an
// outbound-only drill-down shows a few hundred dollars of change and buries
// the story. Showing received alongside sent is also just what an
// investigator asks for first.
//
// Parameters: see PARAMS.cypher.
MATCH (a:Account {account_id: $account_id})<-[:OWNS]-(owner)
CALL (a) {
  MATCH (a)<-[:FROM]-(t:Transaction)-[:TO]->(cp:Account)
  RETURN cp.account_id AS counterparty,
         'sent' AS direction,
         count(t) AS txns,
         round(sum(t.amount_usd)) AS usd
  UNION
  MATCH (a)<-[:TO]-(t:Transaction)-[:FROM]->(cp:Account)
  RETURN cp.account_id AS counterparty,
         'received' AS direction,
         count(t) AS txns,
         round(sum(t.amount_usd)) AS usd
}
RETURN coalesce(owner.individual_id, owner.entity_id) AS owner,
       labels(owner)[0] AS ownerType,
       a.account_id AS account,
       direction,
       counterparty,
       txns,
       usd
ORDER BY usd DESC
LIMIT 15;
