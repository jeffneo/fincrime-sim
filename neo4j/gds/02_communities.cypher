// Shared-device communities, ranked. Needs the `sharedDevice` projection.
//
// Runnable as `analyst`, and it reads no ground truth: the ranking alone is
// the finding. Measured at 2s.
//
// **Connected components, not Louvain.** The question is "which accounts are
// reachable from each other through a device", which is precisely a component.
// Louvain partitions *within* a component by edge density, and in a bipartite
// graph whose devices carry one to four accounts there is no internal density
// structure to find - it would be a more expensive way to get the same answer,
// with a resolution parameter to argue about.
//
// **What this returns at the mvp preset**: sorted by size, the top five
// components are the five mule rings active in the window, and everything of
// four accounts or fewer is a household sharing a phone. No threshold tuning,
// no ground truth - the ordering does the work. 03_score_communities.cypher
// proves it against the answer key, as an admin.
CALL gds.wcc.stream('sharedDevice')
YIELD nodeId, componentId
WITH componentId, gds.util.asNode(nodeId) AS n
WHERE n:Account
WITH componentId, collect(n.account_id) AS accounts
WHERE size(accounts) >= 3
RETURN componentId,
       size(accounts) AS accountCount,
       accounts[0..6] AS sample
ORDER BY accountCount DESC
LIMIT 25;
