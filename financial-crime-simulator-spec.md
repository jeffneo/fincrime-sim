# Synthetic Financial Crime Simulator — Product & Technical Spec

## 1. Purpose

Build a simulator that generates large-scale, richly-labeled synthetic datasets representing the full financial ecosystem — legitimate activity plus embedded financial-crime typologies — for use in:

- Training and benchmarking AML/fraud detection models (rules, ML, graph-based)
- Stress-testing detection systems and investigator workflows without exposing real customer data
- Academic and regulatory research
- Sales engineering / proof-of-concept demos for detection platforms

The core design principle: **the crime is a statistical and structural signature embedded in an otherwise realistic population of normal financial behavior**, not a hand-scripted "story." Realism comes from the background noise being genuinely hard to separate from the signal, and from the signal patterns matching what's documented in real typology literature (FATF, FinCEN, Egmont Group, academic AML literature) rather than being invented.

## 2. Non-Goals / Guardrails

- Not a tool for evading detection — typologies are modeled at the pattern/statistical level (entity/transaction structure, timing, network topology), not as an operational playbook with exploitable specifics (no real institution-specific control bypass techniques, no live vulnerability details).
- No real PII. All entities, names, addresses, and identifiers are generated, not sampled from real people or leaked datasets.
- Ground truth in the output is a research/engineering label, not a legal determination — outputs should never be labeled or distributed as if they were real SARs or real case data.

## 3. Scope: Crime Typologies Covered

| Category | Typologies |
|---|---|
| Money laundering | Structuring/smurfing, layering through shell companies, round-tripping, trade-based laundering (over/under-invoicing), cash-intensive business co-mingling, money mule networks, cuckoo smurfing |
| Fraud | Card-not-present fraud, account takeover, synthetic identity fraud, first-party/bust-out fraud, application fraud, romance/pig-butchering scam flows, business email compromise payment redirection |
| Market abuse | Wash trading, spoofing/layering (order-book), insider trading networks, pump-and-dump coordination |
| Sanctions & terrorist financing | Sanctioned-entity obfuscation via intermediaries, small-value high-frequency transfers, dual-use trade financing patterns |
| Corruption | Shell company beneficial-ownership obfuscation, bribery payment structuring, politically-exposed-person (PEP) proximity networks |
| Tax evasion | Offshore layering, invoice mills |

Each typology is implemented as a **generator module** producing a subgraph/time-series pattern that gets embedded into the broader simulated population at a configurable prevalence rate, so the dataset mirrors realistic base rates (crime is rare relative to legitimate volume — configurable, but defaults should reflect real-world class imbalance, e.g. <0.5% of entities/transactions).

## 4. Data Model

### 4.1 Entities
- **Individuals**: demographics, KYC attributes, risk indicators, device/IP fingerprints, employment/income profile
- **Legal entities**: businesses, shell companies, trusts, with beneficial ownership chains (including layered/obscured ownership)
- **Accounts**: bank accounts, cards, wallets, brokerage accounts — each owned by one or more entities, at one or more institutions
- **Devices / channels**: IPs, device IDs, session metadata (for cyber-enabled fraud and account takeover)
- **External context**: merchants, counterparties, jurisdictions (with configurable risk ratings), correspondent banking relationships

### 4.2 Relationships (graph-native)
- Ownership/control (individual→account, individual→legal entity, entity→entity beneficial ownership)
- Transactional (payer→payee, with amount, timestamp, channel, currency, memo)
- Shared-attribute links (shared device, IP, address, phone — key signal for mule/synthetic-identity rings)
- Employment, family, and known-associate links (for PEP and network-proximity typologies)

### 4.3 Temporal layer
All entities and relationships are versioned over simulated time (account opening, ownership changes, address changes) so the dataset supports temporal graph analysis, not just a static snapshot.

## 5. Simulation Architecture

**Agent-based + generative statistical layer**, chosen over pure rule-based transaction scripting because realism depends on emergent behavior, not scripted sequences.

1. **Population generator**: creates individuals and legal entities with realistic demographic/firmographic distributions (calibrated against public census/business-registry aggregate statistics, not real records).
2. **Behavioral agents**: each entity runs a lightweight behavior model (income, spending habits, business cycle) that generates its "normal" transaction stream — this is the background noise.
3. **Typology injection engine**: selects a subset of entities/accounts and overlays a typology generator, which:
   - Builds the required subgraph (e.g., a layering network of N shell companies)
   - Generates transactions consistent with that typology's known structural/timing signature
   - Blends the illicit transactions into the entity's normal activity stream so they aren't trivially separable (e.g., a mule account still has some genuine-looking transactions)
4. **Noise & adversarial realism layer**: injects false positives (legitimate but unusual behavior that resembles a typology — e.g., a real small business with irregular cash flow) so models trained on the dataset must learn genuine distinguishing signal rather than shortcut features. This is the single most important realism control — the majority of real-world AML/fraud detection difficulty is separating true suspicious patterns from superficially similar legitimate ones.
5. **Institution layer**: simulates one or more "banks," each with configurable control policies (transaction limits, KYC tiers, monitoring thresholds), so the dataset can be used to test how typologies present differently under different control environments.

## 6. Realism Requirements

- **Statistical calibration**: transaction amount distributions, timing patterns, network degree distributions calibrated to published aggregate statistics (e.g., Benford's-law conformance for legitimate transactions, deviation for structured amounts).
- **Network topology matching**: illicit subgraphs should match topologies described in FATF/Egmont typology reports and peer-reviewed AML network studies (e.g., star topology for smurfing, layered chain-of-shells for layering) rather than arbitrary graphs.
- **Label realism**: include "hard negative" entities that are risky-looking but legitimate, and "hard positive" typologies that are deliberately low-signal, so precision/recall on the dataset is meaningfully below 100% for a well-built detector — a dataset that's trivially separable isn't useful.
- **Concept drift**: typology patterns should be able to evolve over simulated time (criminals adapt to detection), supporting research on model decay.
- **Scale**: target configurable population sizes from ~10K entities (fast iteration) to 50M+ entities / billions of transactions (large-scale graph benchmarking), with linear-ish scaling via distributed generation.

## 7. Output & Labeling

- **Graph export**: property graph (nodes/edges with full attribute history) — primary format, since most of the analytic value is relational.
- **Flat transaction logs**: CSV/Parquet for teams using tabular ML pipelines.
- **Ground-truth labels**: per-entity and per-transaction typology label, confidence/ambiguity score, and a "difficulty tier" (easy/medium/hard-to-detect) so the dataset supports curriculum-style evaluation.
- **Case narratives**: optional auto-generated synthetic "investigation summary" per illicit ring, useful for testing case-management / narrative-generation tools downstream.
- **Data dictionary & typology documentation**: shipped alongside every dataset release so users know exactly what was injected and why, supporting reproducible benchmarking.

## 8. Suggested Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Graph store | **Neo4j** (Enterprise, for multi-database + scaled clustering during prototyping) | Native fit for entity-relationship modeling, typology subgraph queries, and downstream graph algorithms (community detection, centrality) used both to generate and to validate typology structure |
| Simulation engine | **Python** (agent-based simulation, e.g. Mesa or custom), managed with **uv** | Fast iteration, rich ecosystem for statistical distribution fitting and ML-based validation |
| Bulk load / ETL | Neo4j `neo4j-admin import` / APOC for large batch loads | Needed at scale (tens of millions of nodes) |
| Validation | Python + a held-out detector model (e.g., GNN or gradient-boosted baseline) trained/tested against the dataset to confirm it isn't trivially separable | Closes the loop on the "hard negative/positive" realism requirement in §6 |
| Distribution | Parquet + graph dump (Neo4j dump / GraphML) versioned releases | Reproducibility across research/eval runs |

## 9. Validation Plan

1. **Statistical fidelity check**: compare simulated aggregate distributions (amounts, degree distributions, timing) against published real-world benchmarks.
2. **Detectability calibration**: run baseline detectors against the dataset; target realistic precision/recall (not near-100%) confirming the noise/hard-negative design is working.
3. **Typology fidelity review**: subject-matter review of generated typology subgraphs against FATF/Egmont typology descriptions.
4. **Privacy audit**: confirm zero overlap with real entity data (no accidental sampling from real sources).

## 10. Phased Delivery Plan

1. **Phase 1 (MVP)**: single-institution simulation, 3–4 core typologies (structuring, shell-layering, mule network, card fraud), tabular + graph export, ~100K entities.
2. **Phase 2**: full typology library from §3, multi-institution/jurisdiction simulation, temporal versioning, hard-negative injection.
3. **Phase 3**: scale-out to 10M+ entities, concept drift, auto-generated case narratives, benchmark leaderboard against baseline detectors.

---

*Next step, if useful: I can prototype Phase 1 — a Neo4j-backed graph model with a Python (uv-managed) generation engine for a couple of core typologies — to validate the approach before committing to the full build.*
