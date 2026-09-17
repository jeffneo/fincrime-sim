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
// the sender's history. It is the stricter reading and it still recovers
// rings, with no false positives at the shipped window: all five hits in
// October 2025 are labelled mule networks.
//
// WINDOW: the window has to CONTAIN a ring. Each ring runs for one to three
// months and they are spread across the year, so an arbitrary month may
// legitimately return nothing - that is the ring calendar, not a failure. The
// default window in PARAMS.cypher holds five of them; the release README
// shows the admin query that lists the calendar.
//
// (An earlier version of this comment claimed a ring's activity smears across
// all twelve months and that only a year-long window works. That was wrong -
// it measured the collector's own background peer traffic, not the ring. A
// ring's labelled transactions span about a month.)
//
// Households legitimately share devices, so this returns those too - which is
// the point of the demo, not a defect. For the same signal found without
// being told what shape to look for, see ../gds.
// Parameters: run PARAMS.cypher first, or pass them yourself.
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
