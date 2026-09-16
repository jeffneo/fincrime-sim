// Typology topology recovery.
//
// Each check finds an injected typology by its STRUCTURE alone - no ground
// truth is read - and then scores what it found against the answer key. Run as
// an admin (`scorer` or `neo4j`); the recovery half must also be runnable as
// `analyst`, and tests/test_typology_checks.py asserts exactly that.
//
// This is the M2 exit criterion: if a Cypher query written from the FATF
// description of the typology cannot recover the members of what the generator
// injected, then the generator is not producing that typology, whatever its
// labels say.
//
// Run:  cypher-shell -u neo4j -p <pw> -d fincrime -f /cypher/typology_checks.cypher

// ---------------------------------------------------------------------------
// T1 structuring - star topology into a collector
// ---------------------------------------------------------------------------
//
// The shape, stated without reference to any label: an account that receives
// peer transfers from several distinct counterparties, each of whom funded
// themselves with cash deposits that all sat below the reporting threshold.
//
// Note what this does NOT use: amounts near the threshold (only the easy tier
// has those), timing tightness (only easy and medium), or any label. It keys on
// convergence, which is the one thing every tier shares.

:param ctrThreshold => 10000.0;
:param minFeeders   => 2;

// --- recovery: who does the structure point at? ---
MATCH (collector:Account)<-[:TO]-(t:Transaction)-[:FROM]->(feeder:Account)
WHERE t.txn_class = 'p2p_transfer'
WITH collector, collect(DISTINCT feeder) AS feeders
WHERE size(feeders) >= $minFeeders
UNWIND feeders AS feeder
// Each feeder must have been funded by sub-threshold cash, not by salary.
MATCH (feeder)<-[:FROM]-(cash:Transaction)
WHERE cash.channel = 'cash'
  AND cash.direction = 'credit'
  AND cash.amount_usd < $ctrThreshold
WITH collector,
     feeder,
     count(cash) AS cashDeposits,
     sum(cash.amount_usd) AS cashTotal
WHERE cashDeposits >= 2
WITH collector,
     collect(feeder) AS smurfs,
     sum(cashTotal) AS placedTotal
WHERE size(smurfs) >= $minFeeders
RETURN collector.account_id AS collector,
       size(smurfs) AS smurfCount,
       round(placedTotal) AS placedUsd
ORDER BY smurfCount DESC, placedUsd DESC;

// --- scoring: how much of each ring did that recover? ---
// ADMIN ONLY. Reads :GroundTruth, so this half is invisible to the demo role.
MATCH (r:Ring {typology: 'structuring'})<-[:MEMBER_OF_RING]-(l:TypologyLabel)
WHERE l.subject_type = 'account'
WITH r, collect(l.subject_id) AS trueMembers

CALL (r) {
  MATCH (collector:Account)<-[:TO]-(t:Transaction)-[:FROM]->(feeder:Account)
  WHERE t.txn_class = 'p2p_transfer'
  WITH collector, collect(DISTINCT feeder) AS feeders
  WHERE size(feeders) >= 2
  UNWIND feeders AS feeder
  MATCH (feeder)<-[:FROM]-(cash:Transaction)
  WHERE cash.channel = 'cash' AND cash.direction = 'credit' AND cash.amount_usd < 10000.0
  WITH collector, feeder, count(cash) AS deposits
  WHERE deposits >= 2
  WITH collector, collect(feeder.account_id) + [collector.account_id] AS found
  RETURN found
}

WITH r, trueMembers, collect(found) AS candidateSets
WITH r,
     trueMembers,
     head([s IN candidateSets WHERE any(m IN s WHERE m IN trueMembers)]) AS matched
RETURN r.ring_id AS ring,
       r.difficulty_tier AS tier,
       size(trueMembers) AS injected,
       size([m IN coalesce(matched, []) WHERE m IN trueMembers]) AS recovered,
       round(
         100.0 * size([m IN coalesce(matched, []) WHERE m IN trueMembers])
         / size(trueMembers)
       ) AS recallPct
ORDER BY tier, ring;
