# Bloom demo bundle

Seven saved Cypher search phrases for Neo4j Bloom — enough for a five-minute
walkthrough of the `mvp` graph and nothing more. This is deliberately a
one-off bundle rather than a curated Perspective: paste the phrases in, run
them in order, tell the story.

The tabular demo queries live in [`neo4j/demo/`](neo4j/demo) and the graph
algorithms in [`neo4j/gds/`](neo4j/gds). Those are for Studio Query. These are
different queries, not the same ones reformatted — see *Why these are written
differently* below.

## Setup

Log in to Bloom (standalone, or the Bloom tab in Enterprise Studio) as the
demo role:

```
analyst / analystanalyst
```

Not as `neo4j`. The whole point of the dataset is that the answer key is
present and unreadable: as `analyst`, `:Ring`, `:TypologyLabel` and
`:CaseNarrative` do not exist — they are absent from the perspective's label
list, absent from `db.labels()`, and unreachable by traversal. Phrase 7 below
demonstrates that directly.

Then, in the Perspective you are using: **Search phrases → new phrase**. Paste
the phrase text into the phrase field and the Cypher into the query field.
Bloom picks up each `$param` in the phrase and prompts for it when the phrase
is selected.

**Warm the graph first.** Run any one phrase twice before demoing. The first
query after a database start reads from an empty page cache and is several
times slower; nothing is wrong.

## Why these are written differently from the Studio queries

Two reasons, both worth knowing before you write your own.

**Bloom draws nodes and relationships, so a phrase has to return them.** The
queries in `neo4j/demo/` return aggregates — counts, sums, account ids — which
is right for a table and gives Bloom nothing to lay out. Every phrase below
returns the nodes and relationships themselves, or a path.

**A phrase anchored on an id must be written so the planner starts from that
id.** This is the sharp edge, and it cost me three wrong attempts. Transactions
are indexed on `(channel, direction, amount_usd)`, so the obvious form of
phrase 2 —

```cypher
WHERE cash.channel = 'cash' AND cash.direction = 'credit' AND cash.amount_usd < 10000
```

— makes the planner seek that index across all 3.3M sub-threshold cash credits
in the year and then check which of them touch your account. **Measured: 226
seconds**, for a query that should expand one account's 530 relationships.

Cypher pushes property predicates down to a leaf index seek, and it pushes
them *through* everything you might reach for to stop it: a `USING INDEX` hint
on the account, a `WITH` barrier, even `WITH ... LIMIT 1`. All three left the
plan unchanged at ~170 seconds. `channel IN ['cash']` does not help either —
a single-element list is rewritten back to an equality and seeks the index
just the same.

What works is projecting the properties into plain variables and filtering
those. A predicate on a variable is not a property lookup, so there is nothing
to push into an index:

```cypher
WITH account, f, cash, cash.channel AS ch, cash.direction AS dir, cash.amount_usd AS amt
WHERE dir = 'credit' AND ch = 'cash' AND amt < 10000
```

**2 seconds, same 38 rows.** Every phrase below that filters a transaction
property is written this way. It looks laborious and it is the difference
between a demo and a three-and-a-half-minute hang.

One more trap worth naming: this only shows up when the id arrives as a real
parameter. With a literal id pasted into the query the planner has enough
information to choose the account, so a phrase can test clean in Studio and
then hang in Bloom, where the id is always a parameter. Test phrases the way
Bloom runs them.

## Scene size

Transactions are nodes in this model (PHASE1-PLAN.md D4), so a two-hop pattern
returns transaction nodes as well as the accounts you care about, and a scene
fills up fast. The `LIMIT` on each phrase is tuned for legibility rather than
completeness — around 60 rows, which lands at roughly 40–120 nodes. Raise them
if you want, but a scene past a few hundred nodes stops reading as anything.

## The parameter values

The defaults below are **tied to the shipped seed (20260915)** and were picked
because they show something. Regenerate with a different seed and the ids
move; the structure does not.

| Phrase | Parameter | Value | What you get |
|---|---|---|---|
| 1 | `account_id` | `ACC-000057796` | 9 feeders paying one collector |
| 2 | `account_id` | `ACC-000055682` | 38 sub-threshold cash deposits |
| 3 | `account_id` | `ACC-000057796` | a mule ring, via its shared device |
| 4 | `account_id` | `ACC-000000021` | a 4-person household — the honest twin |
| 5 | `account_id` | `ACC-000138906` | a USD 94,000 wire chain |
| 6 | `device_id` | `DEV-000046867` | 10 cards, 10 unrelated owners |
| 7 | `account_id` | `ACC-000057796` | nothing, as `analyst` — that is the point |

`ACC-000057796` recurs on purpose: it is the same account the tabular demo set
surfaces first and the collector behind the largest community the GDS
walkthrough finds. One ring, reached four different ways.

---

## 1. Peer transfers into $account_id

The structuring and mule-collection shape: several accounts paying one.
Measured 2s, 60 rows.

```cypher
MATCH (collector:Account {account_id: $account_id})
      <-[to:TO]-(t:Transaction)-[from:FROM]->(feeder:Account)
WITH collector, to, t, from, feeder, t.txn_class AS class
WHERE class = 'p2p_transfer'
RETURN collector, to, t, from, feeder
LIMIT 60
```

## 2. Sub-threshold cash into $account_id

Where the money entered the bank. Every deposit is under the USD 10,000
currency-transaction-report threshold — individually unremarkable, which is
the evasion. Run this on one of the feeders from phrase 1. Measured 2s, 38
rows.

```cypher
MATCH (account:Account {account_id: $account_id})
      <-[f:FROM]-(cash:Transaction)
WITH account, f, cash,
     cash.channel AS ch, cash.direction AS dir, cash.amount_usd AS amt
WHERE dir = 'credit' AND ch = 'cash' AND amt < 10000
RETURN account, f, cash
LIMIT 60
```

## 3. Accounts sharing a device with $account_id

The mule-network signal, and the one that is genuinely a graph problem. Each
member on its own is a person who received some transfers and sent some on;
what connects them is infrastructure, not payments. Measured 1s, 15 rows.

```cypher
MATCH (account:Account {account_id: $account_id})
      <-[:FROM]-(:Transaction)-[:VIA_DEVICE]->(d:Device)
WITH DISTINCT d LIMIT 3
MATCH (d)<-[v:VIA_DEVICE]-(t:Transaction)-[f:FROM]->(other:Account)
WITH d, v, t, f, other, t.txn_class AS class
WHERE class = 'p2p_transfer'
RETURN d, v, t, f, other
LIMIT 120
```

## 4. Household sharing an address with $account_id

Run this straight after phrase 3. It is the same shape — several accounts
linked by a shared attribute — and it is four people living at one address.
The dataset contains hundreds of these, which is what stops "shares
infrastructure" from being a detector on its own. Measured 1s, 4 rows.

```cypher
MATCH p = (account:Account {account_id: $account_id})
          <-[:OWNS]-(:Individual)-[:RESIDES_AT]->(:Address)
          <-[:RESIDES_AT]-(:Individual)-[:OWNS]->(:Account)
RETURN p
LIMIT 25
```

## 5. Wire chain through $account_id

Layering: money arriving at a company and leaving for another within days,
each hop looking like an ordinary commercial payment. Measured 2s, 1 path
carrying USD 94,000. Chains are rare by design — this is a handful of paths in
57M transactions, not a pattern you stumble into.

```cypher
MATCH p = (upstream:Account)<-[:FROM]-(t1:Transaction)
          -[:TO]->(hop:Account {account_id: $account_id})
          <-[:FROM]-(t2:Transaction)-[:TO]->(downstream:Account)
WITH p, t1, t2, t1.channel AS ch1, t2.channel AS ch2
WHERE ch1 = 'wire' AND ch2 = 'wire' AND t2.booked_at > t1.booked_at
RETURN p
LIMIT 20
```

## 6. Cards charged on device $device_id

Card-not-present fraud as cross-card reuse rather than per-card velocity: one
device against cards belonging to ten unrelated people. A single card's burst
is a holiday; this is not. Measured 1s, 80 rows.

```cypher
MATCH (d:Device {device_id: $device_id})
      <-[v:VIA_DEVICE]-(t:Transaction)-[oc:ON_CARD]->(c:Card)
WITH d, v, t, oc, c, t.channel AS ch
WHERE ch = 'card_cnp'
RETURN d, v, t, oc, c
LIMIT 80
```

## 7. Answer key for $account_id

**Returns nothing as `analyst`, and that is the demonstration.** The ring, its
labels and its case narrative are all in the database — the same query run as
`neo4j` or `scorer` returns the ring, its difficulty tier and a written
narrative of what the members did. The demo role is denied traverse and read
on the `:GroundTruth` marker label, so for `analyst` these nodes do not exist.

Add this phrase, run it as `analyst` to show the empty result, then run it in
Studio Query as `scorer` to reveal the answer.

```cypher
MATCH (l:TypologyLabel {subject_type: 'account', subject_id: $account_id})
      -[m:MEMBER_OF_RING]->(r:Ring)
OPTIONAL MATCH (n:CaseNarrative {ring_id: r.ring_id})
RETURN l, m, r, n
LIMIT 10
```

---

## Suggested order

1. **Phrase 1** on `ACC-000057796` — nine accounts paying one. "Why?"
2. **Phrase 2** on one of those feeders — cash, all of it under USD 10,000.
   Name the threshold; that is the whole typology.
3. **Phrase 3** on `ACC-000057796` — they all share a device. This is the
   point where a table would have stopped being useful.
4. **Phrase 4** on `ACC-000000021` — the same shape, entirely innocent. The
   dataset is built so the graph alone does not settle it.
5. **Phrase 5** and **6** if there is time — the other two typologies.
6. **Phrase 7** — the answer key is right there and the demo user cannot see
   it. Switch to `scorer` to read the narrative.

For the version of step 3 that finds the ring *without being told to look for
a device* — project the account/device graph, run connected components, sort
by size — see [`neo4j/gds/`](neo4j/gds). It needs Studio Query rather than
Bloom, and it is the stronger demo.
