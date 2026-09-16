// T3 mule network - accounts sharing a device that all pay one collector.
//
// One pass over the peer transfers in the window, driven by the
// (txn_class, booked_at) index: the device, the sender and the beneficiary all
// come off the same transaction. The two-stage form in typology_checks.cypher
// collects every account seen on every device first, which means scanning the
// whole 28M-relationship device layer before the fan-out even starts, and does
// not return at mvp scale.
//
// This asks for the device link on the TRANSFER itself rather than anywhere in
// the sender's history. It is the stricter reading, and it still recovers
// rings: over calendar 2025 the top hit is the collector of
// RING-mule_network-00000 with 18 distinct feeders behind it.
//
// WINDOW: this is the one query in the set that needs a long window. The
// generator runs a mule ring continuously rather than in a burst - the ring
// above spreads 134 receipts over 12 months - and the shared device appears on
// only a fraction of them, so a month or a quarter holds too few co-occurring
// feeders to clear the >= 3 bar. A full year returns the rings in ~3.5 minutes;
// a quarter returns in 8 seconds and finds nothing. That is a property of the
// data, not of the query, and it is recorded as a calibration item in
// PHASE1-PLAN.md.
//
// Households legitimately share devices, so this returns those too - which is
// the point of the demo, not a defect.
MATCH (d:Device)<-[:VIA_DEVICE]-(t:Transaction)-[:FROM]->(a:Account)
WHERE t.txn_class = 'p2p_transfer'
  AND t.booked_at >= datetime($window_start)
  AND t.booked_at <  datetime($window_end)
MATCH (t)-[:TO]->(collector:Account)
WITH d, collector, count(DISTINCT a) AS feeders, sum(t.amount_usd) AS moved
WHERE feeders >= 3
RETURN d.device_id AS sharedDevice,
       collector.account_id AS collector,
       feeders,
       round(moved) AS movedUsd
ORDER BY feeders DESC, movedUsd DESC
LIMIT 50;
