// Project the account/device graph for community detection.
//
// Runnable as `analyst`: it reads only business data, and a GDS graph lives in
// the per-user catalog, so projecting touches nothing in the dataset.
// Measured: 3s, 139,974 nodes, 147,388 relationships, 16 MiB.
//
// **Bipartite, not account-to-account.** Two accounts belong together when a
// device connects them, and in an Account-Device graph that is exactly what a
// connected component computes - without materialising the O(n^2) account
// pairs a device with n accounts would otherwise produce. The device nodes are
// scaffolding; the answer is the account partition.
//
// **Scoped to peer transfers in a window**, for the same reason the queries in
// neo4j/demo are: it is what makes this interactive. The unscoped all-channel
// version of this projection reads all 28M :VIA_DEVICE relationships and takes
// **730 seconds**; this one seeks (txn_class, booked_at) and takes three. Both
// find the same rings - see 03_score_communities.cypher - because a ring's
// members share a device for the whole window, not only while the money is
// moving.
//
// Two alternatives were measured and rejected:
//
//   * **Native tripartite projection** of Account/Transaction/Device over
//     FROM and VIA_DEVICE. GDS estimates **6,726 MiB** for it at the mvp
//     preset (57.6M nodes, 170.9M relationship directions) against a 4G heap,
//     so it does not fit - and it answers the wrong question anyway, since
//     accounts would be linked through shared transactions as well as devices.
//   * **Materialising `(:Account)-[:USED_DEVICE]->(:Device)`** so the
//     projection could be native. Correct modelling, and what a real
//     deployment would keep - but `apoc.periodic.iterate` over 93K devices was
//     on track for about an hour, because it pays the same 28M-relationship
//     scan plus a MERGE per pair. Not worth it when the scoped projection is
//     three seconds.
//
// Parameters: $window_start, $window_end - the same block the Cypher demos
// use, so run ../demo/PARAMS.cypher first.
MATCH (a:Account)<-[:FROM]-(t:Transaction)-[:VIA_DEVICE]->(d:Device)
WHERE t.txn_class = 'p2p_transfer'
  AND t.booked_at >= datetime($window_start)
  AND t.booked_at <  datetime($window_end)
WITH DISTINCT a, d
RETURN gds.graph.project(
  'sharedDevice',
  a,
  d,
  {},
  {undirectedRelationshipTypes: ['*']}
) AS g;
