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

// ---------------------------------------------------------------------------
// T2 shell layering - a directed chain of corporate pass-through
// ---------------------------------------------------------------------------
//
// Money entering one company and leaving the next within days, repeatedly.
// Stated without labels: consecutive wire hops between business accounts where
// each hop follows its predecessor closely and carries most of its value.

MATCH (a:Account)<-[:FROM]-(t1:Transaction)-[:TO]->(b:Account)
      <-[:FROM]-(t2:Transaction)-[:TO]->(c:Account)
WHERE t1.channel = 'wire' AND t2.channel = 'wire'
  AND t2.booked_at > t1.booked_at
  AND duration.between(t1.booked_at, t2.booked_at).days <= 30
  AND t2.amount_usd > 0.7 * t1.amount_usd
  AND a <> c
RETURN b.account_id AS middleHop,
       count(*) AS chains,
       round(max(t1.amount_usd)) AS largestInUsd
ORDER BY largestInUsd DESC
LIMIT 50;

// ---------------------------------------------------------------------------
// T3 mule network - fan-in to many, fan-out to few, over shared infrastructure
// ---------------------------------------------------------------------------
//
// The device link is what makes this a graph problem: each mule alone is a
// person who received some transfers and sent some on. Households legitimately
// share devices too, so this returns those as well - which is the point.

MATCH (d:Device)<-[:VIA_DEVICE]-(t:Transaction)-[:FROM]->(a:Account)
WITH d, collect(DISTINCT a) AS accounts
WHERE size(accounts) >= 3
UNWIND accounts AS a
MATCH (a)<-[:FROM]-(out:Transaction)-[:TO]->(collector:Account)
WHERE out.txn_class = 'p2p_transfer'
WITH d, collector, count(DISTINCT a) AS feeders, sum(out.amount_usd) AS moved
WHERE feeders >= 3
RETURN d.device_id AS sharedDevice,
       collector.account_id AS collector,
       feeders,
       round(moved) AS movedUsd
ORDER BY feeders DESC, movedUsd DESC
LIMIT 50;

// ---------------------------------------------------------------------------
// T4 card-not-present fraud - one device across unrelated cards
// ---------------------------------------------------------------------------
//
// Cross-card reuse, not per-card velocity. A single card's burst is a holiday;
// the same device on cards belonging to unrelated people is not.

// The cards must belong to DIFFERENT customers. A household's shared tablet
// touching three cards from the same two people is not fraud, and without the
// distinct-owner condition this query returns mostly those.
// No `<-[:FROM]-()` on the card: a Card records its funding account as a
// property (card.account_id), not a relationship, so that decoration matched
// nothing and this check returned zero rows regardless of the data. The
// funding account arrives below through the transaction's own :FROM edge.
MATCH (d:Device)<-[:VIA_DEVICE]-(t:Transaction)-[:ON_CARD]->(card:Card)
WHERE t.channel = 'card_cnp'
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
