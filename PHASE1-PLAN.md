# Phase 1 (MVP) Implementation Plan

Companion to [financial-crime-simulator-spec.md](financial-crime-simulator-spec.md). Covers spec §10.1 only.

**Scope:** single institution, 4 typologies (structuring, shell-layering, mule network, CNP card fraud), Parquet + Neo4j export, ~100K entities.

**Definition of done:** one command regenerates a byte-identical 100K-entity / 12-month dataset from a seed, loads into Neo4j, and a validation report shows the dataset is *not* trivially separable (§9.2) with a shipped data dictionary and typology doc.

---

## 1. Decisions taken up front

These are the choices that shape everything downstream. Three are deliberate deviations from the spec's suggestions — flagged as such.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Generate to Parquet first, load to Neo4j second.** Parquet is the canonical output; the graph is a derived view built with `neo4j-admin database import`. | Gives both required output formats (§7) from one pipeline, decouples simulation from DB availability, and is the only approach that survives Phase 3 scale. Cypher writes cannot absorb 30M+ transactions. |
| D2 | **Vectorized behavior models, not a per-agent step scheduler (Mesa).** *Deviation from §8.* | Each entity still gets its own behavior profile — the agent-based *model* is preserved — but streams are sampled in bulk with numpy/polars. Mesa at 100K entities × 365 steps is ~36M Python-level agent activations, minutes-to-hours per run, and buys nothing here: entities don't interact through mutable shared state. Network-level emergence comes from the counterparty-selection model (§4.3), not from a scheduler. Revisit only if a Phase 2 typology needs genuine agent feedback loops. |
| D3 | **Neo4j Enterprise + GDS + Enterprise Studio from M0.** | Phase 1 output is demoed to enterprise customers, so the demo surface (Studio Query/Bloom/Dashboards) and GDS algorithms are part of the deliverable, not a later addition. Enterprise RBAC is also what makes D5' below implementable. Compose modeled on `architecture/codekg`. |
| D4 | **Transactions are reified nodes** (`(:Transaction)` with `[:FROM]`/`[:TO]`, plus optional `[:VIA_DEVICE]`, `[:VIA_IP]`, `[:AT_MERCHANT]`). | A transaction carries multi-party context (device, IP, merchant, channel) and needs to be independently labelable. Relationship-based payments would force those dimensions onto properties and block per-transaction labels. Costs ~3× edges; acceptable given D1. |
| D5 | **Labels live in separate tables, never on entity/transaction rows.** | Lets the same dataset ship labeled and unlabeled, and makes label leakage into detector features structurally impossible rather than a discipline problem. |
| D5' | **In the graph, ground truth is a dedicated node label (`:GroundTruth`) and dedicated relationship types — never a property on a business node — and carries no constraints or indexes.** | The release ships *fully labeled* and uses Enterprise RBAC to hide answers from demo users. Neo4j deny-rules are cleanest at label/relationship-type granularity, and a deny on any one of a node's labels hides the whole node — so a single marker label is the entire control point. Property-level hiding would be spread across every node type and silently incomplete. The no-constraints part closes the one surface deny rules miss: `SHOW CONSTRAINTS` is not privilege-filtered, so a constraint on `:Ring` would name it. Nothing is lost — the importer enforces uniqueness at load time and ground truth is ~50K nodes against 30M transactions. PBAC (property-based access control) does not help here: it filters nodes by property value and has no effect on schema-token visibility. Enforced by tests at both the schema and live-database level. |
| D5'' | **Demo queries must run as the unprivileged `fincrime_demo` role.** | "Detection" that reads the answer key is not a demo. From M2, every shipped demo query runs in CI as `fincrime_demo` and fails the build if it errors or returns nothing. Scoring runs as `fincrime_admin`. |
| D6 | **Difficulty tiers are generator parameters, not post-hoc annotations.** | "Hard" must mean *generated with less signal* (tighter blending, more dispersion, smaller rings), otherwise the tier is a fiction and curriculum evaluation (§7) is meaningless. |
| D7 | **Minimal hard-negative set is in Phase 1.** *Scope addition vs §10; confirmed.* | §9.2 detectability calibration is the Phase 1 acceptance gate. With no hard negatives the dataset *will* be trivially separable, the gate passes vacuously, and we learn nothing about whether the design works until Phase 2. Four generators only (§5 below) — a fraction of the Phase 2 hard-negative library. |
| D8 | **Full determinism from a single master seed**, via `numpy.random.SeedSequence` spawning per-module and per-ring child streams. | §7 reproducible benchmarking. Adding a typology must not perturb the background population — separate streams mean it doesn't. |

### Scale presets

| Preset | Entities | Window | Transactions | Use |
|---|---|---|---|---|
| `dev` | 10K | 3 months | 1.37M | Iteration; every milestone's smoke test |
| `mvp` | 100K | 12 months | 55.4M | Phase 1 release target |

Measured at M1, not estimated. The planning figure was ~30M on an assumed 25
txn/entity-month; the archetypes actually produce ~43, which is the right
number for an active current account carrying payroll, rent, utilities, card
spend, cash and P2P. The consequence is a 1.8GB Parquet release rather than
~1GB — worth knowing before M6, and dialable through the archetype rates in
`reference.py` if the release should be smaller.

---

## 2. Repository layout

```
fincrime/
  pyproject.toml                  # uv-managed; polars, pyarrow, numpy, typer, pydantic, lightgbm, neo4j
  config/
    scale-dev.yaml  scale-mvp.yaml
    typologies.yaml               # prevalence, difficulty-tier knob bundles
    institution.yaml              # limits, KYC tiers, monitoring thresholds
  src/fincrime/
    cli.py                        # generate | load | validate | report | manifest
    rng.py  schema.py             # seed hierarchy; Arrow schemas = single source of truth
    population/                   # individuals, legal_entities, accounts, merchants, devices, addresses
    behavior/                     # profiles, retail, business, counterparty, calendar
    typologies/                   # base.py + structuring, shell_layering, mule_network, cnp_fraud
    hard_negatives/               # cash_business, treasury_hub, travel_card, holding_structure
    institution/controls.py       # thresholds, CTR filing, KYC tiering
    export/                       # parquet, neo4j_import, datadict
    validate/                     # stats, detectability, privacy, report
  neo4j/
    docker-compose.yml  constraints.cypher  typology_checks.cypher
  tests/
  out/                            # gitignored
```

`schema.py` is authoritative: Parquet schemas, Neo4j import headers, and the shipped data dictionary are all generated from it, so they cannot drift.

---

## 3. Data model (Phase 1 subset)

**Nodes.** `Individual`, `LegalEntity`, `Account`, `Card`, `Transaction`, `Device`, `IpAddress`, `Address`, `PhoneNumber`, `Merchant`, `Institution`, `Jurisdiction`.

**Relationships.** `OWNS` (entity→account, with `from`/`to`/`share`), `BENEFICIAL_OWNER_OF` (entity→entity, `pct`), `DIRECTOR_OF`, `FROM`/`TO` (transaction↔account), `AT_MERCHANT`, `VIA_DEVICE`, `VIA_IP`, `RESIDES_AT`, `HAS_PHONE`, `EMPLOYED_BY`, `ASSOCIATE_OF`.

Shared-attribute links (`RESIDES_AT`, `HAS_PHONE`, `VIA_DEVICE`) are load-bearing, not decoration — they are the primary signal for mule and synthetic-identity rings (§4.2) and the main reason the graph export has value over the flat log.

**Temporal.** Phase 1 carries `valid_from`/`valid_to` on ownership and address edges and honors them at generation time, but does not implement the full versioning/bitemporal query layer (spec puts that in Phase 2). The columns exist now so Phase 2 is an extension, not a migration.

**Privacy controls** (§9.4), designed in rather than audited on:
- Identifiers are drawn from ranges that can never be validly issued — SSN-like values in the never-issued `900–999` area prefix, IBANs with deliberately invalid check digits, card PANs in a reserved test BIN passing Luhn but not routable.
- Names/addresses are composed synthetically; no attribute is conditioned on any real record, so a coincidental name collision describes no real person. Stated explicitly in the shipped data dictionary.
- Generation runs with no network access; enforced by a test.

---

## 4. Typologies

Each implements one interface: `select_hosts()` → `build_subgraph()` → `emit_transactions()` → `emit_labels()`. Difficulty knobs listed are the D6 parameters.

**T1 Structuring / smurfing.** Multiple deposit points (branches, ATMs, accounts) → one collector. Cash deposits held below the CTR threshold with deliberate dispersion, short aggregation window. *Signature:* star topology, leading-digit distribution deviating from Benford, sub-threshold clustering. *Knobs:* headroom below threshold, deposit-point count, time dispersion, share of genuine activity blended in.

**T2 Shell-company layering.** Chain of shells with shared directors/addresses, rapid pass-through, in/out ratio ≈1.0, near-zero balance retention, no genuine trading activity. Single-institution constraint means cross-border hops terminate at a synthetic correspondent counterparty placeholder. *Signature:* directed path with fan-in at source, fan-out at sink; ownership chain depth. *Knobs:* chain length, pass-through latency, round-number bias, decoy legitimate activity per shell.

**T3 Money mule network.** Recently-opened accounts on shared devices/IPs/phones; many unrelated inbound payers, forwarding to a small collector set, then cash-out. *Signature:* fan-in/fan-out asymmetry, account age, pass-through velocity, device-sharing community recoverable by Louvain. *Knobs:* ring size, device-sharing rate, retention fraction, recruitment ramp.

**T4 Card-not-present fraud.** Compromised PANs at CNP merchants; small-amount testing auth followed by escalation, velocity bursts, device/IP and geography mismatched to cardholder. *Signature:* one fraudster device touching many cards, inter-arrival burstiness, MCC concentration. *Knobs:* cards per device, test-auth presence, amount ratio vs. cardholder's normal, burst compression.

**Blending (§5.3).** Every illicit actor also emits its normal behavioral stream for the whole window — a mule has a salary and a grocery habit. Illicit transactions are interleaved into that stream, not appended as a separable block.

---

## 5. Hard negatives (per D7)

Legitimate entities engineered to trip each typology's headline signal:

| Generator | Mimics | Distinguishing signal a good detector must find |
|---|---|---|
| Cash-intensive business (restaurant, laundromat) | T1 | Stable seasonality, matching card-sales ratio, consistent supplier payments |
| Payroll / treasury hub account | T3 | Stable counterparty set over time, employment links, no device sharing |
| Frequent traveler with new device | T4 | Continuity of merchant preferences, no cross-card device reuse |
| Legitimate holding-company structure | T2 | Real operating subsidiary with employees and trading activity; ownership is disclosed, not layered |

---

## 6. Milestones

Each exits on a runnable check, and every milestone runs at `dev` scale.

| | Milestone | Exit criteria |
|---|---|---|
| **M0** ✅ | Foundation: uv project, CLI skeleton, `rng.py`, `schema.py`, Neo4j Enterprise + GDS + Studio compose, constraints, RBAC roles, test suite | **Met.** `generate --scale dev` emits 33 schema-valid Parquet tables; `make all` bulk-loads and applies 15 constraints / 29 indexes; GDS 2026.07.0 and Studio reachable; 110 unit tests and 9 live RBAC tests green |
| **M1** ✅ | Population + background behavior | **Met.** 10K entities / 3 months / 1.37M txns, zero typologies. `fincrime validate` runs 15 statistical and privacy checks, each with a stated band and reason, and passes at both `dev` and `mvp` scale. Generation streams per month: `mvp` builds 55.4M transactions in 2.9 min at 9.0GB peak |
| **M2** ✅ | Institution controls + **T1 structuring** end-to-end, **plus the detectability harness** | **Met.** 15 rings / 314 labels at `mvp`; graph loads with ground truth; a structure-only Cypher query recovers the stars *as the demo role*; both baselines run. Calibration: rules recall 0.00 / 0.46 / 0.60 at 1/5/10% budgets, GBM AUC-PR 0.42 (376× lift) |
| **M3** ✅ | T2, T3, T4 + difficulty tiers | **Met.** All four typologies inject at the configured prevalence, produce the documented topology, and are recovered by their Cypher checks. Every typology now clears the GBM learnability floor — shell layering and CNP fraud only with graph features (0.143 and 0.200 against 0.020 tabular), which is the dataset's point rather than a shortfall. Tier separation clears 0.15 for structuring (0.36) and mule networks (0.56); shell layering (0.07) and CNP tier power remain open in §7a |
| **M4** ✅ | Hard negatives + blending | **Met.** Four generators; 537 look-alikes against 112 positives at `mvp`. Look-alikes outnumber true positives 4:1 *inside the alert queue*. Difficulty tiers separate cleanly: recall 0.59 easy / 0.45 medium / 0.15 hard |
| **M5** ◐ | Validation + calibration loop | **Partially met.** 27 of 31 checks pass. The GBM floor and the mule look-alike ratio are resolved; four items remain, each with a stated cause and two of them needing a dataset-defining decision rather than a knob — §7a |
| **M6** ✅ | MVP release at `mvp` scale + case-narrative stub | **Met.** `make release SCALE=mvp` assembles `releases/0.1.0/` — 47 files, 7.2GB: Parquet, a 4.3GB Neo4j dump, generated data dictionary and typology doc, 1,199 case narratives, the demo query set, RBAC Cypher, and a manifest listing every file with its SHA-256 alongside the seed / config digest / schema digest / git SHA that produced it. Gated on `make release-check`: all six demo queries return rows as `analyst` in 1–8s and the 10 live RBAC tests pass, so a payload cannot be cut from a graph whose answer key is readable |

**The harness moves to M2 deliberately.** Detectability calibration is the schedule risk, not the typology code — deferring all of it to M5 means discovering at M5 that four typologies need re-tuning. One typology measured early de-risks the other three.

---

## 7. Acceptance criteria

Starting targets. M5 measures them; expect to revise the bands once with justification, and record the revision.

**Realism**
- Prevalence: ≤0.5% of entities labeled; illicit transactions ≤0.1% of volume.
- Benford MAD: legitimate transactions < 0.006 (conformant); structuring deposits > 0.015.
- Amount distribution fits log-normal per archetype; account-degree tail fits a power law.

**Detectability (the gate)**
- Bank-style rules baseline at a **5%** alert budget: overall recall **0.15–0.45**.
  Revised from 1% at M2, as anticipated. 1% is 1,000 alerts a year for 100,000
  customers — four a business day, which no AML function of that size would be
  staffed for. Recall is now reported across 1/5/10% budgets so the choice of
  operating point is visible rather than load-bearing.
- GBM baseline AUC-PR per typology: **0.25–0.70**. No typology above **0.85** (too easy) or below **0.10** (unlearnable).
- Legitimate look-alikes per true positive **inside** the alert queue: **≥2**.
  Revised at M4 from "≥25% of the queue". A share is bounded by how many hard
  negatives exist relative to the budget — 537 look-alikes cannot fill a
  quarter of a 5,000-alert queue however well they are built, so the original
  figure was unreachable by arithmetic rather than by quality. The ratio
  expresses what the check was for: whether a detector has to work to tell them
  apart. Real AML queues run 10:1 or worse.
- **Tier separation**: easy-tier recall minus hard-tier recall **≥0.15**. Added
  at M4 as the informative gate for D6. Aggregate recall is mostly the tier
  mix, and a dataset whose tiers were decoration would report the same number.

**Engineering**
- Same seed + config → byte-identical Parquet, verified in CI.
- Cypher topology checks recover **≥95%** of injected ring members per typology.
- `mvp` generation completes in < 30 min on one workstation; Neo4j import < 20 min.
- Data dictionary and typology doc generated from `schema.py`, not hand-maintained.

---

## 7a. Calibration state (M5)

Measured at `mvp`, seed 20260915, 63–113 positives per typology. Two GBM
columns: identical model and folds, the second with `graph_features` joined on.

| Typology | rings | pos | look-alikes | rules recall @1/5/10% | tier recall e/m/h | GBM tabular | GBM +graph |
|---|---|---|---|---|---|---|---|
| structuring | 15 | 112 | 547 | 0.08 / 0.49 / 0.62 | 0.59 / 0.49 / 0.23 | 0.413 | **0.439** |
| mule_network | 11 | 113 | 450 | 0.05 / 0.49 / 0.61 | 0.68 / 0.48 / 0.12 | 0.728 | **0.728** |
| shell_layering | 19 | 82 | 275 | 0.18 / 0.39 / 0.61 | 0.41 / 0.39 / 0.33 | 0.020 | **0.143** |
| cnp_fraud | 6 | 63 | 450 | 0.00 / 0.06 / 0.19 | 0.07 / 0.05 / 0.00 | 0.020 | **0.200** |

27 of 31 checks pass.

### Resolved

**Every typology now clears the 0.10 GBM floor, and the two that needed the
graph to get there are the dataset's argument.** Shell layering goes 0.020 →
0.143 and CNP fraud 0.020 → 0.200 when topology is added to an otherwise
identical model; structuring and mule networks, which are customer-level
phenomena, barely move. That is the result the spec is for: a year of
per-customer aggregates cannot see a path through several companies or a burst
on one card, and no amount of tuning the tabular feature set will change it.
The two numbers are reported side by side rather than folded into one so the
tabular baseline stays interpretable — see `validate/detectability.py`.

**The mule look-alike ratio.** Was 0.11 against a target of 2. The cause was
not the host pool, as first assumed, but two bugs: the hard-negative retry
loops keyed their RNG stream on the success counter, so one failed attempt
redrew the identical stream and failed identically until the retry budget ran
out (treasury_hub shipped 5 instances of an intended 75); and treasury_hub
declared `entities_per_instance = 6` while labelling only 1, spending six times
the prevalence budget per instance. Both are now regression-tested.

**The device-sharing signal was noise.** `behavior._sessions` drew a uniformly
random device from the whole pool for every entity transaction, so 1,000 dev
companies' 196,258 payments landed across all 7,926 devices and the median
device carried 26 unrelated accounts. Businesses now bank from one to three
stable devices; the median device carries 1 account and the 99th percentile 3.
The same fault existed in IP assignment, where uniform roaming put a median of
25 owners on every address; home IP now follows the device, which already
encodes household sharing, and roaming goes to one of two stable alternates.
Median owners per IP is 6 — left there deliberately, because carrier-grade NAT
really does put many subscribers behind one address. Devices are the sharp
signal; IPs are honestly noisy.

### Open

1. **Shell-layering tiers do not separate** (0.07 against ≥0.15). Unchanged and
   still the most interesting item. The knobs that should do the work —
   round-number bias, shared directors, decoy activity — are graph properties,
   while the rule that actually catches these chains is the
   high-risk-jurisdiction wire, which every tier trips equally. Worth noting
   that tier separation is measured against the *rules* queue, so it partly
   measures what the rules happen to key on rather than how hard the ring is.
   Measuring it against the graph-augmented model would be a truer test of D6 —
   but changing the metric because the current one fails is how a gate stops
   meaning anything, so it needs deciding on its merits, not here.
2. **Layering look-alikes do not compete** (0.91 against ≥2). 275 holding
   structures exist and the population ratio is 3.4:1, but they reach the alert
   queue far less often than layering rings do. A multinational group really
   does pay foreign subsidiaries, so giving holding structures some
   cross-border intra-group payments would make them trip the same
   high-risk-jurisdiction wire rule that catches the rings. Realism-justified
   and untried.
3. **CNP tier separation is unmeasurable at this base rate, and that is a real
   trade rather than a bug.** A fixed entity budget divided by instance size
   buys the ring count, so cnp_fraud's large card batches buy only 6 rings —
   two per tier. Shrinking them was tried and measured: `cards_per_device`
   [10,40] → [6,15] took AUC-PR from 0.170 to **0.012**, because unlike a mule
   ring, this typology's signal *is* the number of cards behind one device.
   Reverted. Getting both detectability and tier power needs a larger share of
   the illicit budget for this typology, or a higher base rate than 0.4% — a
   dataset-defining choice, not a knob.
4. **The rules baseline is nearly blind to CNP fraud** (0.06 at a 5% budget,
   against a 0.10–0.60 band). `card_velocity_txn_per_hour` and
   `cnp_amount_ratio` exist but fire on almost none of it. Either the rule set
   needs a CNP rule that works the way a real issuer's does, or the band is
   wrong for a typology whose detection is a card-network problem rather than
   an AML-monitoring one.

Two items recorded here earlier were wrong and are withdrawn. The mule
shared-device signal is **not** thin in a short window: a ring's transactions
span 32 days, all carry a device, and only 2 distinct devices appear across the
whole ring — what looked like a 12-month smear was the collector's own
background p2p traffic. And CNP device reuse looked unselective because of the
random-device bug above, not because of the query. Over a window containing a
ring the T3 check returns 6 hits, all illicit, no false positives.

## 7b. Query performance at mvp scale — resolved

The demo query set in `neo4j/demo/` returns in **1–9 seconds** against 57.3M
transactions, as the demo role, on the laptop container. Full measurements in
[PERFORMANCE-NOTES.md](PERFORMANCE-NOTES.md).

The earlier diagnosis — that the constraint was page cache against a 28GB store
— was wrong, and was disproved by pushing the graph to a 32GB Aura instance,
where four of six queries still did not return in five minutes. The real causes
were both in the queries: composite indexes that ended in `amount_usd` could
not serve the time window, so a month-scoped query read `booked_at` off 3.3M
scattered nodes; and the structuring query nested two index seeks instead of
hash-joining them on the account they share. Two indexes ending in `booked_at`
and one `USING JOIN` hint closed it.

Left over from that work, in priority order for M6:

1. **A year-scoped T1 exhausts the 4G transaction memory pool.** Batch shape,
   not demo shape, but it needs either a larger pool or a two-pass form.
2. **The Docker VM disk** has ~13GB free of 59GB after pruning. Each
   additional Transaction index costs ~3.8GB.

GDS at mvp scale is now measured and is the strongest demo in the set: a
bipartite Account↔Device projection is 16 MiB and three seconds when scoped
the way the Cypher demos are, WCC runs in one to three seconds, and sorted by
size the top components are the mule rings with households below them — found
with no threshold and no ground truth, as the demo role. The full-year
unscoped projection costs 730s for the same answer, and a native tripartite
projection would need 6,726 MiB against a 4G heap. Detail in
[PERFORMANCE-NOTES.md](PERFORMANCE-NOTES.md); the walkthrough is
`neo4j/gds/` and ships in the release.

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Detectability calibration overruns** — the knobs interact and tuning is iterative. Highest-probability schedule risk. | Harness at M2 (one typology) rather than M5 (four). Treat band-hitting as its own milestone with explicit time. |
| **D2 deviation is wrong** — a Phase 2 typology needs true agent feedback. | Behavior profiles stay per-entity and addressable, so a scheduler can be introduced for specific typologies without rewriting the population layer. |
| **Neo4j import at 30M+ transactions** hits header/ID or memory issues late. | Exercise the full import path at M2, not M6. Size-scaled dry run before the `mvp` build. |
| **Hard negatives don't actually confuse** — a detector separates them easily, gate passes vacuously. | The ≥25%-of-alerts criterion makes this a measured, failable condition rather than an assumption. |
| **Ambiguous-label semantics** — an entity that is both hard negative and typology host. | Labels are multi-valued with a confidence score from the start (§7); no single-label assumption anywhere in the schema. |

---

## 9. Resolved questions

1. **Jurisdiction: US.** $10K CTR threshold, SAR semantics, NAICS industry codes, MCC merchant codes, never-issued `900–999` SSN area prefixes. Calibration constants live in `config/institution.yaml`.
2. **Hard negatives: in Phase 1** (D7).
3. **Case narratives: stub at M6.** Template-driven per-ring summary, not a Phase 3 generative implementation.
4. **Release: `releases/<version>/` in this repo, fully labeled.** Ground truth is present in the dump and hidden from demo users by RBAC (D5'); the admin role runs scoring. Demo queries may not read it (D5'').
