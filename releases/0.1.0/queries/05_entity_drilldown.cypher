// Investigator drill-down: one account's largest counterparties and its owner.
// This is what an analyst does after an alert, and it is the query shape that
// should be interactive at any scale - a point lookup on the account key, then
// bounded expansion.
MATCH (a:Account {account_id: $account_id})<-[:OWNS]-(owner)
CALL (a) {
  MATCH (a)<-[:FROM]-(t:Transaction)-[:TO]->(cp:Account)
  RETURN cp.account_id AS counterparty,
         count(t) AS txns,
         round(sum(t.amount_usd)) AS sentUsd
  ORDER BY sentUsd DESC
  LIMIT 10
}
RETURN coalesce(owner.individual_id, owner.entity_id) AS owner,
       labels(owner)[0] AS ownerType,
       a.account_id AS account,
       counterparty, txns, sentUsd
ORDER BY sentUsd DESC;
