// Neo4j Enterprise Studio prerequisites + the fincrime database and its roles.
// Runs against the `system` database, applied once by the `nes-init` service
// before Studio starts. Idempotent - re-running after `make nes` is harmless.
//
// Credentials here are local-dev only and are duplicated in docker-compose.yml
// under the enterprise-studio service. If you change one, change both - Studio
// fails to start with an opaque asset-store error if they drift.

// ---------------------------------------------------------------------------
// 1. Studio asset store
// ---------------------------------------------------------------------------

CREATE USER tools_service IF NOT EXISTS
  SET PASSWORD 'toolspassword'
  SET PASSWORD CHANGE NOT REQUIRED;

// `architect` carries the token and constraint privileges Studio needs to build
// its asset schema on first start.
GRANT ROLE architect TO tools_service;

// Name must match NES_assetStore_default_database in docker-compose.yml.
CREATE DATABASE `tools-storage` IF NOT EXISTS;

// Without these, Bloom silently shows no schema (it reads SHOW INDEXES /
// SHOW CONSTRAINTS) and asset sharing cannot enumerate roles or users.
GRANT SHOW CONSTRAINTS ON DATABASES * TO reader;
GRANT SHOW INDEXES ON DATABASES * TO reader;
GRANT SHOW ROLE ON DBMS TO reader;
GRANT SHOW USER ON DBMS TO reader;

// ---------------------------------------------------------------------------
// 2. The dataset databases
// ---------------------------------------------------------------------------

// One database per scale preset. A bulk load replaces the whole store, so
// sharing a name between presets means a dev smoke test destroys the mvp
// graph. Both carry identical roles and identical deny rules, because a demo
// runs against whichever one is loaded.
CREATE DATABASE fincrime IF NOT EXISTS;
CREATE DATABASE `fincrime-dev` IF NOT EXISTS;

// ---------------------------------------------------------------------------
// 3. Roles. The release ships FULLY LABELED (PHASE1-PLAN.md §9.4) - ground
//    truth is present in the dump and hidden at query time by the deny rules
//    below, rather than being stripped from the data.
// ---------------------------------------------------------------------------

// fincrime_admin: sees everything, runs scoring and GDS write-back.
CREATE ROLE fincrime_admin IF NOT EXISTS;
GRANT ALL ON DATABASES fincrime, `fincrime-dev` TO fincrime_admin;
GRANT MATCH {*} ON GRAPHS fincrime, `fincrime-dev` NODES * TO fincrime_admin;
GRANT MATCH {*} ON GRAPHS fincrime, `fincrime-dev` RELATIONSHIPS * TO fincrime_admin;
GRANT WRITE ON GRAPHS fincrime, `fincrime-dev` TO fincrime_admin;
// GDS needs to project and, for mutate/write modes, write back.
GRANT EXECUTE BOOSTED PROCEDURES gds.*, apoc.* ON DBMS TO fincrime_admin;

// fincrime_demo: the role a customer demo actually runs as. Reads the whole
// business graph, denied the answer key.
CREATE ROLE fincrime_demo IF NOT EXISTS;
GRANT ACCESS ON DATABASES fincrime, `fincrime-dev` TO fincrime_demo;
GRANT SHOW CONSTRAINTS ON DATABASES fincrime, `fincrime-dev` TO fincrime_demo;
GRANT SHOW INDEXES ON DATABASES fincrime, `fincrime-dev` TO fincrime_demo;
GRANT MATCH {*} ON GRAPHS fincrime, `fincrime-dev` NODES * TO fincrime_demo;
GRANT MATCH {*} ON GRAPHS fincrime, `fincrime-dev` RELATIONSHIPS * TO fincrime_demo;
// Investigators need GDS for community detection and centrality - those are
// the demo, not a leak. Projections are read-only for this role.
GRANT EXECUTE BOOSTED PROCEDURES gds.*, apoc.* ON DBMS TO fincrime_demo;
// GDS projections and algorithm state are held per-user in the graph catalog,
// so this write privilege never touches the dataset itself.
GRANT NAME MANAGEMENT ON DATABASES fincrime, `fincrime-dev` TO fincrime_demo;

// --- The answer key. ---
// Every ground-truth node carries the marker label :GroundTruth in addition to
// its specific label (:TypologyLabel, :Ring, :CaseNarrative). A deny on ANY of
// a node's labels hides the node entirely, so this one rule is the whole
// control point - which is exactly why ground truth is a label and never a
// property on a business node (D5'). Property-level denies would have to be
// repeated per node type and would silently miss new ones.
// The marker label. This is the catch-all: any future ground-truth node type
// is hidden the moment it carries :GroundTruth, even if someone forgets to add
// a rule here.
DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` NODES GroundTruth TO fincrime_demo;
DENY READ {*} ON GRAPHS fincrime, `fincrime-dev` NODES GroundTruth TO fincrime_demo;

// The concrete labels as well. The marker deny alone already makes the DATA
// unreachable, but `db.labels()` filters per label token - so with only the
// marker denied, a demo user running `CALL db.labels()` in Studio still sees
// `Ring` and `TypologyLabel` listed and learns an answer key exists. Denying
// each label by name removes them from schema introspection too.
// Keep in step with the ground-truth tables in schema.py; tests/test_schema.py
// fails the build if a table is added here without a rule.
DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` NODES Ring TO fincrime_demo;
DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` NODES TypologyLabel TO fincrime_demo;
DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` NODES CaseNarrative TO fincrime_demo;

DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` RELATIONSHIPS LABELS_SUBJECT TO fincrime_demo;
DENY TRAVERSE ON GRAPHS fincrime, `fincrime-dev` RELATIONSHIPS MEMBER_OF_RING TO fincrime_demo;

// Residual disclosure, accepted: `SHOW CONSTRAINTS` still names these labels,
// because a constraint definition carries its own label. The data is what is
// protected, and tests/test_rbac.py asserts it is unreachable by read, count,
// and traversal from a visible business node.

// ---------------------------------------------------------------------------
// 4. Demo users
// ---------------------------------------------------------------------------

CREATE USER analyst IF NOT EXISTS
  SET PASSWORD 'analystanalyst'
  SET PASSWORD CHANGE NOT REQUIRED;
GRANT ROLE fincrime_demo TO analyst;

CREATE USER scorer IF NOT EXISTS
  SET PASSWORD 'scorerscorer'
  SET PASSWORD CHANGE NOT REQUIRED;
GRANT ROLE fincrime_admin TO scorer;
