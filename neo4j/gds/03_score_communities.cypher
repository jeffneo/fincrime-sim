// ADMIN ONLY. Scores the communities from 02 against the answer key.
//
// Reads :GroundTruth, so this is invisible to the demo role - which is the
// point: 02 finds the rings without it, and this says how well.
//
// Measured at the mvp preset, 11 illicit mule rings:
//
//   tier    rings   found (year)   found (October)
//   easy        3      3  1.00       3  1.00
//   medium      6      6  1.00       5  0.83
//   hard        2      0  0.00       0  0.00
//
// The month-scoped projection loses one medium ring and costs three seconds
// instead of 730. Note how little the window matters otherwise: a ring's
// members share a device for the whole year, because the generator repoints
// the owner's device rather than attaching one to the ring's transactions, so
// the infrastructure signal outlives the money movement. That is realistic -
// people keep their phones - and it is why this method is robust to window
// choice while the Cypher check in neo4j/demo/03 is not: that one needs
// transfers to a common collector *inside* the window.
//
// The misses are the hard tier - which sets
// `device_sharing_rate` to [0.0, 0.15], i.e. it removes the signal this method
// depends on. That is difficulty tiers working as specified (D6) rather than a
// gap in the method: a detector that found the hard tier here would mean the
// generator had left the headline signal in.
//
// Compare the rules baseline on the same typology - tier recall 0.68 / 0.48 /
// 0.12 - and note what this buys beyond the numbers: a component is a list of
// accounts an investigator can open, not a score.

// --- per component: what was it, really? ---
CALL gds.wcc.stream('sharedDevice')
YIELD nodeId, componentId
WITH componentId, gds.util.asNode(nodeId) AS n
WHERE n:Account
WITH componentId, collect(n.account_id) AS accounts
WHERE size(accounts) >= 3
UNWIND accounts AS acct
OPTIONAL MATCH (l:TypologyLabel {subject_type: 'account', subject_id: acct})
               -[:MEMBER_OF_RING]->(r:Ring)
WITH componentId,
     size(accounts) AS accountCount,
     collect(DISTINCT r.ring_id) AS rings,
     collect(DISTINCT r.typology) AS typologies,
     collect(DISTINCT r.polarity) AS polarities
RETURN accountCount,
       coalesce(rings[0], '(unlabelled - household)') AS ring,
       coalesce(typologies[0], '-') AS typology,
       coalesce(polarities[0], '-') AS polarity
ORDER BY accountCount DESC
LIMIT 25;

// --- recall per difficulty tier ---
//
// Driven from the full set of rings rather than from what was found, so a
// tier with zero hits reports 0 instead of vanishing from the output. The
// hard tier is the one that matters here and it is exactly the one that
// disappears if you aggregate the other way round.
CALL gds.wcc.stream('sharedDevice')
YIELD nodeId, componentId
WITH componentId, gds.util.asNode(nodeId) AS n
WHERE n:Account
WITH componentId, collect(n.account_id) AS accounts
WHERE size(accounts) >= 3
UNWIND accounts AS acct
OPTIONAL MATCH (l:TypologyLabel {subject_type: 'account', subject_id: acct})
               -[:MEMBER_OF_RING]->(r:Ring {typology: 'mule_network', polarity: 'illicit'})
WITH collect(DISTINCT r.ring_id) AS found

MATCH (ring:Ring {typology: 'mule_network', polarity: 'illicit'})
WITH found, ring.difficulty_tier AS tier, collect(ring.ring_id) AS ringIds
WITH tier,
     size(ringIds) AS total,
     size([x IN ringIds WHERE x IN found]) AS ringsFound
RETURN tier,
       total,
       ringsFound,
       round(100.0 * ringsFound / total) AS recallPct
ORDER BY tier;
