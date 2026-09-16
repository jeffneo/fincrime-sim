// Aura variant of nes-setup.cypher: the same two roles and two users, applied
// to an Aura instance loaded by `neo4j-admin database upload`.
//
// Run against the `system` database of the Aura instance:
//   cypher-shell -a $AURA_URI -u $AURA_USERNAME -p $AURA_PASSWORD -d system \
//     -f /cypher/aura-setup.cypher
//
// Three differences from the self-hosted file, all forced by Aura:
//
// * One database, always named `neo4j`. Aura has no multi-database, so the
//   per-preset split (mvp -> fincrime, dev -> fincrime-dev) collapses: an
//   instance holds whichever dump was last uploaded. The DatasetManifest node
//   in the graph is the only record of which preset that was.
// * No CREATE DATABASE and no `tools-storage`. Studio assets live wherever
//   Studio is configured to keep them; nothing here provisions them.
// * No EXECUTE BOOSTED on apoc/gds. Aura ships its own procedure allow-list
//   and grants execution through its built-in roles; asking for BOOSTED here
//   fails rather than adding anything.
//
// Everything that matters for D5' is identical: ground truth is hidden by
// label denies, not by stripping it from the data.

// ---------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------

// fincrime_admin: sees everything, runs the scoring half of
// typology_checks.cypher.
CREATE ROLE fincrime_admin IF NOT EXISTS;
GRANT ACCESS ON DATABASE neo4j TO fincrime_admin;
GRANT SHOW CONSTRAINTS ON DATABASE neo4j TO fincrime_admin;
GRANT SHOW INDEXES ON DATABASE neo4j TO fincrime_admin;
GRANT MATCH {*} ON GRAPH neo4j NODES * TO fincrime_admin;
GRANT MATCH {*} ON GRAPH neo4j RELATIONSHIPS * TO fincrime_admin;
GRANT WRITE ON GRAPH neo4j TO fincrime_admin;
GRANT NAME MANAGEMENT ON DATABASE neo4j TO fincrime_admin;

// fincrime_demo: the role a customer demo runs as. Reads the whole business
// graph, denied the answer key.
CREATE ROLE fincrime_demo IF NOT EXISTS;
GRANT ACCESS ON DATABASE neo4j TO fincrime_demo;
GRANT SHOW CONSTRAINTS ON DATABASE neo4j TO fincrime_demo;
GRANT SHOW INDEXES ON DATABASE neo4j TO fincrime_demo;
GRANT MATCH {*} ON GRAPH neo4j NODES * TO fincrime_demo;
GRANT MATCH {*} ON GRAPH neo4j RELATIONSHIPS * TO fincrime_demo;
// GDS projections are held per-user in the graph catalog, so this never
// touches the dataset itself.
GRANT NAME MANAGEMENT ON DATABASE neo4j TO fincrime_demo;

// --- The answer key. ---
// A deny on ANY of a node's labels hides the node entirely, so the marker
// label is the whole control point (PHASE1-PLAN.md D5'). The concrete labels
// are denied as well because `db.labels()` filters per label token: with only
// the marker denied, a demo user still sees `Ring` and `TypologyLabel` listed
// and learns an answer key exists.
DENY TRAVERSE ON GRAPH neo4j NODES GroundTruth TO fincrime_demo;
DENY READ {*} ON GRAPH neo4j NODES GroundTruth TO fincrime_demo;

DENY TRAVERSE ON GRAPH neo4j NODES Ring TO fincrime_demo;
DENY TRAVERSE ON GRAPH neo4j NODES TypologyLabel TO fincrime_demo;
DENY TRAVERSE ON GRAPH neo4j NODES CaseNarrative TO fincrime_demo;

DENY TRAVERSE ON GRAPH neo4j RELATIONSHIPS LABELS_SUBJECT TO fincrime_demo;
DENY TRAVERSE ON GRAPH neo4j RELATIONSHIPS MEMBER_OF_RING TO fincrime_demo;

// ---------------------------------------------------------------------------
// Demo users
// ---------------------------------------------------------------------------

CREATE USER analyst IF NOT EXISTS
  SET PASSWORD 'analystanalyst'
  SET PASSWORD CHANGE NOT REQUIRED;
GRANT ROLE fincrime_demo TO analyst;

CREATE USER scorer IF NOT EXISTS
  SET PASSWORD 'scorerscorer'
  SET PASSWORD CHANGE NOT REQUIRED;
GRANT ROLE fincrime_admin TO scorer;
