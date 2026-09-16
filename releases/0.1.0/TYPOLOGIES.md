# Typologies

Generated from the generator docstrings and `config/typologies.yaml`.
Do not edit by hand.

## How difficulty works

A difficulty tier is a **bundle of generator parameters**, never a
label applied afterwards (PHASE1-PLAN.md D6). `hard` means the ring
was built with less signal in it: amounts further from the threshold,
timing more dispersed, rings smaller, infrastructure shared less, more
genuine activity blended into the same accounts. That is what makes
curriculum evaluation meaningful - an easy ring really is easier, and
the validation report measures whether that holds.

## Prevalence

- Illicit entities: **0.4%** of individuals.
- Hard negatives: **2.0%** - deliberately more common than crime, because a real alert queue is
  dominated by legitimate oddities.

A typology's entity budget is its share of the illicit total, and the
ring count follows from that budget divided by the mean ring size. A
typology built from large rings therefore gets few of them, which
matters for any per-tier statistic - see PHASE1-PLAN.md §7a.

| Typology | share of illicit entities | tier mix (easy/medium/hard) |
|---|---|---|
| `structuring` | 30% | 30% / 50% / 20% |
| `shell_layering` | 25% | 30% / 50% / 20% |
| `mule_network` | 25% | 30% / 50% / 20% |
| `cnp_fraud` | 20% | 30% / 50% / 20% |

---

## Typologies

### `structuring` — T1 — structuring / smurfing

The typology: cash that cannot be deposited in one piece without triggering a
currency transaction report is broken into pieces that each stay under the
threshold, deposited by several people across several points, and consolidated
into one account.

**Topology** — a star. N smurf accounts feed one collector, which moves the
consolidated total out. This is the shape FATF and FinCEN describe, and it is
what `neo4j/typology_checks.cypher` recovers.

**What makes it hard.** The obvious signal — deposits clustered just under
USD 10,000 — is only present in the easy tier. The hard tier deposits well
under the threshold, disperses over months, varies amounts widely, and blends
a majority of genuine activity into the same accounts. At that point the only
thing separating it from a legitimate cash business is the *structure*: money
converging on an account whose owner has no commercial reason to receive it.
That is deliberate, and it is why the graph matters more than the amounts.

**What it must not do.** The generator reads the CTR threshold and the
same-day aggregation rule from the institution config, so it evades the control
the institution actually enforces. Depositing 9,900 twice in one day would be
aggregated to 19,800 and reported — a generator that ignored aggregation would
produce a pattern the bank catches for free, making the typology look harder to
evade than it is.

**Difficulty knobs**

| Knob | easy | medium | hard |
|---|---|---|---|
| `deposit_points` | `[6, 12]` | `[4, 8]` | `[2, 4]` |
| `threshold_headroom_pct` | `[2, 8]` | `[8, 25]` | `[25, 55]` |
| `aggregation_window_days` | `[3, 10]` | `[10, 30]` | `[30, 75]` |
| `legit_activity_share` | `[0.0, 0.2]` | `[0.3, 0.6]` | `[0.6, 0.85]` |
| `amount_jitter_cv` | `0.05` | `0.18` | `0.35` |
| `reuse_same_branch` | `True` | `False` | `False` |

### `shell_layering` — T2 — layering through shell companies

Placed funds are moved through a chain of corporate entities to break the audit
trail between origin and destination. Each hop looks like an ordinary
commercial payment; the chain as a whole does not.

**Topology** — a directed path, with fan-in at the source and fan-out at the
sink. That is the shape FATF and Egmont describe and what
`neo4j/typology_checks.cypher` looks for: money entering one end and leaving
the other within days, through entities that trade with nobody else.

**What makes it hard.** The easy tier is a chain of empty shells sharing
directors and a registered address, passing round numbers along within hours
and retaining nothing. The hard tier is short, slow, retains a real fraction at
each hop, uses entities with their own genuine trading activity, and shares
almost nothing between them. At that point the only thing left is that the
money entering the first entity and leaving the last are the same money — and
a legitimate holding group (the M4 hard negative) moves funds between its own
entities constantly for entirely ordinary reasons.

**Single-institution constraint.** Phase 1 simulates one bank, so a hop that
would leave it terminates at an external counterparty rather than being
dropped: the transaction exists with no `TO` edge, exactly as the bank would
see it. Phase 2's second institution replaces the placeholder.

**Difficulty knobs**

| Knob | easy | medium | hard |
|---|---|---|---|
| `chain_length` | `[4, 6]` | `[3, 5]` | `[2, 3]` |
| `pass_through_hours` | `[1, 24]` | `[24, 120]` | `[120, 600]` |
| `retention_pct` | `[0.0, 0.02]` | `[0.02, 0.08]` | `[0.08, 0.2]` |
| `round_amount_bias` | `0.8` | `0.35` | `0.05` |
| `shared_director_rate` | `0.9` | `0.5` | `0.15` |
| `shared_address_rate` | `0.9` | `0.4` | `0.1` |
| `decoy_activity_per_shell` | `[0, 2]` | `[5, 20]` | `[30, 90]` |

### `mule_network` — T3 — money mule network

Recruited individuals receive funds from unrelated payers and forward them to a
small set of collectors, keeping a cut. The accounts are often recently opened
and the ring frequently operates from shared infrastructure.

**Topology** — fan-in to many mules, fan-out to few collectors, with the mule
layer connected to each other only through that shared infrastructure. The
community structure is what GDS finds, and it is the reason this typology is
the best demonstration of why a graph matters: no mule looks remarkable alone.

**What makes it hard.** The easy tier shares one device across the whole ring,
opens every account days before use, and forwards within hours retaining almost
nothing. The hard tier shares no device at all, uses accounts a year old,
forwards over weeks, and keeps a third of the money — at which point a mule is
a person who received some transfers and sent some on. The device-sharing
signal, which is the headline one, is deliberately absent from the hard tier.

The background has to carry legitimate device sharing for this to be a real
problem, and it does: households share devices, which produces hundreds of
small shared-device clusters that any community-detection demo will surface
alongside the rings.

**Difficulty knobs**

| Knob | easy | medium | hard |
|---|---|---|---|
| `ring_size` | `[6, 12]` | `[5, 10]` | `[3, 6]` |
| `collectors` | `[1, 2]` | `[2, 4]` | `[3, 6]` |
| `device_sharing_rate` | `[0.7, 1.0]` | `[0.3, 0.6]` | `[0.0, 0.15]` |
| `account_age_days_at_first_use` | `[1, 20]` | `[20, 120]` | `[120, 400]` |
| `retention_pct` | `[0.0, 0.05]` | `[0.05, 0.12]` | `[0.12, 0.3]` |
| `forward_latency_hours` | `[1, 12]` | `[12, 72]` | `[72, 400]` |
| `recruitment_ramp_days` | `[5, 15]` | `[15, 60]` | `[60, 180]` |

### `cnp_fraud` — T4 — card-not-present fraud

Compromised card numbers used online. The operator tests each card with a small
authorization first, then escalates if it clears, and works through a batch in
a burst before the issuer catches up.

**Topology** — a fraudster's device touching many cards that have nothing else
in common. That cross-card device reuse is the strongest available signal and
is exactly what the graph makes visible: each card in isolation shows a spend
burst, which thousands of legitimate travellers also show.

**What makes it hard.** The easy tier puts forty cards on one device, tests
every one with a sub-dollar auth, and drains them in an afternoon at the same
few merchant categories. The hard tier uses one or two cards per device, skips
the test auth entirely, spends inside the cardholder's own normal range, and
spreads over weeks. At that point the only thing left is that the merchants are
ones the cardholder never uses — and the frequent-traveler hard negative (M4)
looks exactly like that for a fortnight every year.

Declines matter here. The background declines ~1.2% of card traffic, which is
what gives a card-testing burst somewhere to hide: a run of small failed
authorizations is unremarkable in a log that already contains them.

**Difficulty knobs**

| Knob | easy | medium | hard |
|---|---|---|---|
| `cards_per_device` | `[10, 40]` | `[3, 10]` | `[1, 2]` |
| `test_auth` | `True` | `True` | `False` |
| `test_auth_amount_usd` | `[0.5, 4.0]` | `[1.0, 12.0]` | — |
| `burst_window_hours` | `[1, 6]` | `[6, 72]` | `[72, 500]` |
| `txns_per_card` | `[4, 15]` | `[2, 6]` | `[1, 3]` |
| `amount_ratio_vs_normal` | `[4.0, 20.0]` | `[1.5, 5.0]` | `[0.8, 2.0]` |
| `geo_mismatch_rate` | `1.0` | `0.6` | `0.15` |
| `mcc_concentration` | `0.9` | `0.5` | `0.2` |

---

## Hard negatives

Legitimate patterns built to be mistaken for a typology. Each one
produces the same surface shape as the typology it mimics and carries
a distinguishing signal underneath - stated in its own section below.
They are labelled with `polarity = 'hard_negative'` and with the
typology they mimic, and they must never be scored as positives.

### `cash_intensive_business` — mimics `structuring`

Share of the hard-negative budget: **35%**. Labelled entities per instance: **4**.

**Why this is the hard case.** A restaurant that sends three staff members to
the bank with the day's cash produces exactly the subgraph a smurfing ring
produces: several individuals making sub-threshold cash deposits, each
forwarding to one business account. Same star, same channel, same
under-threshold amounts, same convergence. The Cypher pattern that recovers a
structuring ring recovers this too, and it should — that is what makes the
pattern alone insufficient.

**What separates them, all of it visible in the graph.** The depositors are
employees of the business (`EMPLOYED_BY`). The cash arriving is proportional
to card settlement from the same trading days, because a real restaurant takes
both. Deposits follow the week — heavy Monday after a weekend, light midweek.
Suppliers get paid on a cadence. The business has been trading since before the
window opened. A smurf collector has none of that.

Sub-threshold deposits here are *not* evasion. A restaurant's daily take
genuinely is a few thousand dollars, and a manager who banks each day rather
than weekly is doing the ordinary thing, not structuring. The amounts look the
same; the reason differs.

### `treasury_hub` — mimics `mule_network`

Share of the hard-negative budget: **25%**. Labelled entities per instance: **1**.

A company that runs payroll for its staff and sweeps cash between its own
accounts has extreme fan-in and fan-out and retains almost nothing: money
arrives and leaves within days, over and over. That is the textbook mule
signature, and the `rapid_pass_through` rule cannot tell the difference.

What separates it: the counterparty set is *stable*. The same people are paid
every fortnight, and they are linked by `EMPLOYED_BY`. A mule ring's inbound
counterparties are unrelated to each other and change constantly, and its
members share devices. Stability over time is the feature, and it only exists
because the background runs for a full window.

### `frequent_traveler` — mimics `cnp_fraud`

Share of the hard-negative budget: **25%**. Labelled entities per instance: **1**.

Someone abroad presents every headline fraud signal at once: a device the bank
has not seen, a foreign IP, merchant countries that do not match their home
address, and a burst of spend compressed into a few days. A velocity or
geo-mismatch rule cannot separate this from a stolen card.

What separates it, and the reason M1 built merchant loyalty at all: the
traveler keeps their own merchant preferences. They book the same airline, and
the card keeps being used afterwards at the merchants they always used. A
fraudster has no history with the card, drains it, and stops. Continuity across
the burst is the feature — which only exists because each customer has a
personal merchant set spanning the whole window.

### `holding_structure` — mimics `shell_layering`

Share of the hard-negative budget: **15%**. Labelled entities per instance: **3**.

A group with a holding company and operating subsidiaries moves money between
its own entities constantly: management charges, intra-group loans, cash
pooling. Ownership runs through several layers. On the ownership graph alone
that is indistinguishable from a layering chain, and a query looking for
chained `BENEFICIAL_OWNER_OF` hops finds both.

What separates it: the subsidiaries actually trade. They have employees, real
customer receipts, supplier payments and payroll — the background gives them
all of that because they are ordinary population members. A shell has an
ownership chain and nothing underneath it. Ownership is also disclosed here,
which the layering typology (M3) will vary.

