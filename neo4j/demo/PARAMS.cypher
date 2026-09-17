// Demo parameters. Set them once, then run any query in this directory.
//
// In Neo4j Browser or Enterprise Studio - the way a demo is actually driven -
// paste the three `:param` lines into the editor and run them. They persist
// for the session, so every query afterwards picks them up.
//
// From the shell, pipe this file and the query into ONE session:
//
//   cat PARAMS.cypher 01_structuring_recovery.cypher \
//     | cypher-shell -u analyst -p analystanalyst -d fincrime
//
// Two `-f` flags will NOT work: each file is evaluated separately and the
// parameters do not carry across, which fails with "Expected $`window_start`
// ... but got nothing". Scripts that prefer explicit arguments can skip this
// file and pass `-P "window_start => '2025-10-01T00:00:00Z'"` and friends,
// which is what scripts/bench-demo.sh does.
//
// `:param` is a client command rather than Cypher, so an application driver
// sets the same three names as an ordinary parameter map instead.
//
// Every query here takes the same three, so one block covers the set - the
// window pair for 01, 02, 03, 04, 06 and the GDS projection in ../gds, the
// account for 05.
//
// -------------------------------------------------------------------------
// These values are tied to the shipped seed (20260915). Regenerate with a
// different seed and the ids move; the window stays valid, since the
// simulation window is always calendar 2025.
// -------------------------------------------------------------------------

// A one-month window. The queries are scoped rather than unscoped because
// that is what makes them interactive at 57M transactions - the unscoped year
// does not return (PERFORMANCE-NOTES.md).
//
// October specifically: all six queries return rows in it, and five mule
// rings are active. That matters most for 03_mule_shared_device, which can
// only find a ring whose activity falls inside the window - pick an arbitrary
// month and it legitimately returns nothing. An admin can list the ring
// calendar; see the release README.
:param window_start => '2025-10-01T00:00:00Z';
:param window_end   => '2025-11-01T00:00:00Z';

// The top hit of 01_structuring_recovery in that window, which makes the
// drill-down a continuation of the demo rather than a random lookup: nine
// counterparties paid this account between USD 8,000 and 31,000 during
// October. It is in fact a mule-network collector - the structuring pattern
// and the mule pattern overlap at the collector, which is a real property of
// the typologies rather than a labelling accident - and it is also the
// account behind the largest community that ../gds/02_communities.cypher
// returns. The same ring, found three different ways.
:param account_id   => 'ACC-000057796';
