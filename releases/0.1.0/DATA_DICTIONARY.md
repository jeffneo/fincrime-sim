# Data Dictionary

Generated from `src/fincrime/schema.py`. Do not edit by hand.

## Reading this dataset

The canonical form is Parquet under `graph/` (business data) and
`ground_truth/` (labels). The Neo4j graph is derived from it.

**Ground truth is separated at three levels:** its own Parquet
directory, its own node labels, and the `:GroundTruth` marker label
that Neo4j RBAC denies to the `fincrime_demo` role. A detector built
on this dataset cannot reach a label by accident - it has to go
looking in a different directory or authenticate as a different user.

## Privacy

Every row is synthetic. No attribute is conditioned on any real
record. Identifiers are additionally drawn from ranges that can never
be validly issued, so no value here can collide with a real-world
identifier:

| Control | Guarantee | Columns |
|---|---|---|
| `invalid_checkdigit_iban` | IBAN with a deliberately invalid ISO 7064 check digit. | `account.iban` |
| `never_issued_ssn` | SSN area prefix 900-999, never issued by the SSA. | `individual.tax_id` |
| `reserved_555_range` | NANP 555-0100 to 555-0199, reserved for fictitious use. | `phone_number.e164` |
| `reserved_house_number` | House number >= 900000, far above anything US street addressing issues, so the line cannot coincide with a real deliverable address while city/state/ZIP stay geographically realistic. | `address.street` |
| `reserved_ipv4_space` | IPv4 from 240.0.0.0/4 (RFC 1112, reserved and never allocated) or 100.64.0.0/10 (RFC 6598 carrier-grade NAT, never publicly routed). RFC 5737 documentation ranges are deliberately NOT used: they offer only 762 addresses, which at 100K entities would force dozens of unrelated customers onto each IP and manufacture the exact shared-infrastructure signal the mule typology is detected by. | `ip_address.address` |
| `reserved_registration` | Registry format no real state registry issues. | `legal_entity.registration_number` |
| `reserved_rssd` | RSSD id above the Federal Reserve's issued range. | `institution.rssd_id` |
| `reserved_test_bin` | Luhn-valid PAN on a reserved test BIN, not routable on any network. | `card.pan_masked` |
| `synthetic_composition` | Composed from generated parts. Not sampled from any real record; any collision with a real name is coincidental and every other attribute of the row is independent of it. | `individual.full_name`, `legal_entity.legal_name`, `merchant.name` |
| `unassigned_ein` | EIN prefix outside the IRS's assigned campus ranges. | `legal_entity.tax_id` |

Ground-truth labels are research and engineering annotations. They are
not legal determinations and must not be presented as real SARs or
real case data.

## Business graph

### `:Institution` — table `institution`

A financial institution. Phase 1 simulates exactly one; the table exists now so Phase 2 multi-institution is an extension rather than a schema migration.

Labels: `:Institution`

| Column | Type | Key | Description |
|---|---|---|---|
| `institution_id` | string | PK | Synthetic institution id. |
| `name` | string |  | Institution name. |
| `country` | string |  | ISO 3166-1 alpha-2 country of domicile. |
| `rssd_id` | string |  | Federal Reserve RSSD-style identifier. |
| `control_policy` | string |  | Name of the control policy profile applied. |

### `:Jurisdiction` — table `jurisdiction`

A country or territory with a configurable AML risk rating, used to drive counterparty selection and layering destinations.

Labels: `:Jurisdiction`

| Column | Type | Key | Description |
|---|---|---|---|
| `code` | string | PK | ISO 3166-1 alpha-2 code. |
| `name` | string |  | Jurisdiction name. |
| `risk_rating` | string |  | Configured AML risk rating: low | medium | high. A simulation parameter, not an assertion about the real jurisdiction. |
| `is_secrecy_haven` | boolean |  | Configured financial-secrecy flag. |

### `:Address` — table `address`

A postal address. Shared-address links are a primary signal for mule and synthetic-identity rings, so addresses are first-class nodes rather than entity properties.

Labels: `:Address`

| Column | Type | Key | Description |
|---|---|---|---|
| `address_id` | string | PK | Synthetic address id. |
| `street` | string |  | Street line. The house number is always >= 900000, far above any number US street addressing issues, so the line cannot coincide with a real deliverable address. |
| `city` | string |  | City name. |
| `state` | string |  | US state code. |
| `postcode` | string |  | Real 5-digit ZIP. Public geographic data, not personal data - the address is made unreal by its house number instead, which keeps the geographic distribution realistic. |
| `country` | string |  | ISO 3166-1 alpha-2 country. |
| `is_cmra` | boolean |  | Commercial mail-receiving agency (drop box). |

### `:PhoneNumber` — table `phone_number`

A telephone number. Shared-phone links carry the same signal weight as shared addresses and devices.

Labels: `:PhoneNumber`

| Column | Type | Key | Description |
|---|---|---|---|
| `phone_id` | string | PK | Synthetic phone id. |
| `e164` | string |  | E.164 number using the NANP 555-01xx range reserved for fictitious use, so no row can ring a real subscriber. |
| `line_type` | string |  | mobile | landline | voip. |

### `:Individual` — table `individual`

A natural person. All attributes are generated; none is conditioned on any real record.

Labels: `:Individual`

| Column | Type | Key | Description |
|---|---|---|---|
| `individual_id` | string | PK | Synthetic person id. |
| `full_name` | string |  | Name composed from synthetic name parts. Any collision with a real person's name is coincidental and all other attributes are independent of it. |
| `date_of_birth` | date |  | Date of birth. |
| `tax_id` | string |  | SSN-format identifier using a 900-999 area prefix, which the SSA has never issued and never will. |
| `occupation` | string |  | Occupation label. |
| `naics_employer` | string |  | NAICS code of employer industry. |
| `income_band` | string |  | Annual income band. |
| `annual_income` | double |  | Annual income in account currency. |
| `kyc_tier` | long |  | Institution KYC tier, 1 (basic) to 3 (enhanced). |
| `onboarded_on` | date |  | Date the customer relationship opened. |
| `is_pep` | boolean |  | Politically exposed person flag as recorded by KYC. |
| `residency_country` | string | FK | ISO 3166-1 alpha-2 country of residence. |
| `behavior_archetype` | string |  | Behavioral profile driving normal activity. |

### `:LegalEntity` — table `legal_entity`

A company, trust, or other legal person, including shells. Whether an entity is a shell is ground truth and is NOT recorded here.

Labels: `:LegalEntity`

| Column | Type | Key | Description |
|---|---|---|---|
| `entity_id` | string | PK | Synthetic entity id. |
| `legal_name` | string |  | Company name composed synthetically. |
| `registration_number` | string |  | Registry number in a format no real registry issues. |
| `tax_id` | string |  | EIN-format identifier using an unassigned prefix. |
| `entity_type` | string |  | llc | corp | trust | partnership | sole_prop. |
| `naics` | string |  | NAICS industry code. |
| `incorporated_on` | date |  | Date of incorporation. |
| `incorporation_country` | string | FK | ISO 3166-1 alpha-2 country of incorporation. |
| `declared_annual_revenue` | double |  | Revenue as declared at onboarding. |
| `declared_employee_count` | long |  | Employee count as declared. |
| `kyc_tier` | long |  | Institution KYC tier, 1 to 3. |
| `onboarded_on` | date |  | Date the customer relationship opened. |
| `is_cash_intensive` | boolean |  | Cash-intensive business per its NAICS code. |
| `behavior_archetype` | string |  | Behavioral profile driving normal activity. |

### `:Account` — table `account`

A deposit, savings, or brokerage account at an institution. Cards are modeled separately and hang off an account.

Labels: `:Account`

| Column | Type | Key | Description |
|---|---|---|---|
| `account_id` | string | PK | Synthetic account id. |
| `iban` | string |  | IBAN-format string with a deliberately invalid check digit, so it can never validate as a real account. |
| `institution_id` | string | FK | Owning institution. |
| `account_type` | string |  | checking | savings | brokerage | escrow. |
| `currency` | string |  | ISO 4217 currency code. |
| `opened_on` | date |  | Account opening date. |
| `closed_on` | date |  | Account closing date, null if open. |
| `status` | string |  | open | dormant | closed | frozen. |
| `opening_balance` | double |  | Balance at the start of the simulated window. |
| `monitoring_segment` | string |  | Segment the institution monitors it under. |

### `:Card` — table `card`

A payment card issued against an account. Separate from Account because card-not-present fraud operates at PAN level.

Labels: `:Card`

| Column | Type | Key | Description |
|---|---|---|---|
| `card_id` | string | PK | Synthetic card id. |
| `pan_masked` | string |  | Masked PAN. The underlying number is Luhn-valid but issued from a reserved test BIN, so it is not routable on any network. |
| `account_id` | string | FK | Funding account. |
| `card_type` | string |  | debit | credit | prepaid. |
| `network` | string |  | Card network label. |
| `issued_on` | date |  | Issue date. |
| `expires_on` | date |  | Expiry date. |
| `status` | string |  | active | blocked | expired | reissued. |
| `credit_limit` | double |  | Credit limit, null for debit. |

### `:Merchant` — table `merchant`

A merchant accepting card payments, as a transaction counterparty.

Labels: `:Merchant`

| Column | Type | Key | Description |
|---|---|---|---|
| `merchant_id` | string | PK | Synthetic merchant id. |
| `name` | string |  | Merchant name composed synthetically. |
| `mcc` | string |  | ISO 18245 merchant category code. |
| `country` | string | FK | ISO 3166-1 alpha-2 country. |
| `channel_mix` | string |  | card_present | cnp | hybrid. |
| `acquirer_risk_tier` | string |  | Acquirer-assigned risk tier. |

### `:Device` — table `device`

A client device used to initiate transactions. Device sharing across unrelated customers is a primary mule and fraud-ring signal.

Labels: `:Device`

| Column | Type | Key | Description |
|---|---|---|---|
| `device_id` | string | PK | Synthetic device id. |
| `fingerprint` | string |  | Device fingerprint hash. |
| `device_type` | string |  | mobile | desktop | tablet. |
| `os` | string |  | Operating system family and version. |
| `first_seen` | datetime |  | First time the device appeared. |
| `is_emulator` | boolean |  | Device presents as an emulator. |

### `:IpAddress` — table `ip_address`

A source IP observed on a session.

Labels: `:IpAddress`

| Column | Type | Key | Description |
|---|---|---|---|
| `ip_id` | string | PK | Synthetic IP id. |
| `address` | string |  | IPv4 address from 240.0.0.0/4, reserved by RFC 1112 and never allocated or routed to a real host. Shared/NAT addresses come from 100.64.0.0/10 (RFC 6598 carrier-grade NAT), also never publicly routed. |
| `asn` | long |  | Autonomous system number. |
| `country` | string |  | Geolocated country, ISO 3166-1 alpha-2. |
| `is_proxy` | boolean |  | Known proxy, VPN, or Tor exit. |

### `:Transaction` — table `transaction`

A single movement of value, reified as a node so it can carry multi-party context (device, IP, merchant) and be labeled independently of the accounts involved (PHASE1-PLAN.md D4).

Labels: `:Transaction`

| Column | Type | Key | Description |
|---|---|---|---|
| `txn_id` | string | PK | Synthetic transaction id. |
| `amount` | double |  | Signed amount in `currency`. |
| `currency` | string |  | ISO 4217 currency code. |
| `amount_usd` | double |  | Amount converted to USD for thresholding. |
| `booked_at` | datetime |  | Booking timestamp, UTC. |
| `value_date` | date |  | Value date. |
| `channel` | string |  | cash | ach | wire | card_present | card_cnp | p2p | internal | check. |
| `direction` | string |  | debit | credit, relative to the originating account. |
| `memo` | string |  | Free-text payment reference. |
| `txn_class` | string |  | Purpose class, e.g. payroll, retail, transfer. |
| `ctr_reportable` | boolean |  | Institution determined this transaction meets the USD 10,000 currency-transaction-report threshold. An institution control output, not a crime label. |
| `declined` | boolean |  | Authorization declined. |

## Ground truth

Hidden from the `fincrime_demo` role. Scoring runs as `fincrime_admin`.

### `:Ring` — table `ring`

One injected typology instance - a criminal ring, network, or scheme. GROUND TRUTH: hidden from the demo role.

Labels: `:Ring`, `:GroundTruth`

| Column | Type | Key | Description |
|---|---|---|---|
| `ring_id` | string | PK | Ring id. |
| `typology` | string |  | Typology generator that produced it. |
| `polarity` | string |  | illicit | hard_negative. A hard negative carries the typology it MIMICS, so this column is the only thing on the node that separates a ring from its look-alike - at the mvp preset the table holds 45 illicit rings and 703 look-alikes, and a scoring query that matches on `typology` alone counts all 748 as positives. TypologyLabel.polarity says the same thing per label; this makes it true of the ring as well. |
| `difficulty_tier` | string |  | easy | medium | hard. Determined by the generator parameters used, not assigned after the fact (D6). |
| `knob_digest` | string |  | Hash of the exact knob values used. |
| `injected_from` | date |  | Start of the injection window. |
| `injected_to` | date |  | End of the injection window. |
| `member_count` | long |  | Number of labeled subjects in the ring. |
| `illicit_txn_count` | long |  | Number of labeled transactions. |
| `illicit_amount_usd` | double |  | Total labeled value moved, USD. |

### `:TypologyLabel` — table `typology_label`

One label attached to one subject (entity, account, card, or transaction). Multi-valued by design: a subject may carry several labels, including being both a hard negative and a ring member. GROUND TRUTH: hidden from the demo role.

Labels: `:TypologyLabel`, `:GroundTruth`

| Column | Type | Key | Description |
|---|---|---|---|
| `label_id` | string | PK | Label id. |
| `ring_id` | string | FK | Ring this label belongs to. |
| `subject_id` | string |  | Id of the labeled node. |
| `subject_type` | string |  | individual | legal_entity | account | card | transaction. |
| `typology` | string |  | Typology name. |
| `role` | string |  | Role within the typology, e.g. smurf, collector, shell_tier2, mule, fraudster_device, victim. |
| `polarity` | string |  | illicit | hard_negative. Hard negatives are legitimate subjects deliberately built to resemble the typology (D7). |
| `confidence` | double |  | 0-1 ambiguity score. Below 1.0 where the generator itself produced a genuinely ambiguous case (spec §7). |
| `difficulty_tier` | string |  | easy | medium | hard. |

### `:CaseNarrative` — table `case_narrative`

Template-generated investigation summary for one ring. M6 stub: template-driven, not a generative implementation (that is Phase 3). GROUND TRUTH: hidden from the demo role.

Labels: `:CaseNarrative`, `:GroundTruth`

| Column | Type | Key | Description |
|---|---|---|---|
| `narrative_id` | string | PK | Narrative id. |
| `ring_id` | string | FK | Ring described. |
| `summary` | string |  | Narrative text. |
| `template_version` | string |  | Template version used. |

## Relationships

| Type | From | To | Ground truth | Description |
|---|---|---|---|---|
| `OWNS` | `individual` | `account` |  | Individual account ownership, including joint ownership. |
| `OWNS` | `legal_entity` | `account` |  | Legal-entity account ownership. |
| `BENEFICIAL_OWNER_OF` | `individual` | `legal_entity` |  | Individual beneficial ownership of a legal entity. |
| `BENEFICIAL_OWNER_OF` | `legal_entity` | `legal_entity` |  | Entity-to-entity beneficial ownership. Chains of these are what obscure ultimate beneficial ownership in layering typologies - and also what legitimate holding structures look like. |
| `DIRECTOR_OF` | `individual` | `legal_entity` |  | Directorship. Shared directors across otherwise unconnected entities is a shell-network signal. |
| `EMPLOYED_BY` | `individual` | `legal_entity` |  | Employment, which drives payroll transaction streams. |
| `ASSOCIATE_OF` | `individual` | `individual` |  | Family or known-associate link, used for PEP proximity and recruitment networks. |
| `RESIDES_AT` | `individual` | `address` |  | Individual address history. |
| `REGISTERED_AT` | `legal_entity` | `address` |  | Registered address of a legal entity. Many entities at one address is a shell-network signal - and also a real serviced-office signal. |
| `HAS_PHONE` | `individual` | `phone_number` |  | Individual contact number. |
| `FROM` | `transaction` | `account` |  | Originating account of a transaction. |
| `TO` | `transaction` | `account` |  | Beneficiary account of a transaction. Absent for cash withdrawals and for payments leaving the simulated institution. |
| `AT_MERCHANT` | `transaction` | `merchant` |  | Merchant leg of a card transaction. |
| `ON_CARD` | `transaction` | `card` |  | Card used, for card transactions. |
| `VIA_DEVICE` | `transaction` | `device` |  | Device that initiated the transaction, where the channel has one. |
| `VIA_IP` | `transaction` | `ip_address` |  | Source IP of the initiating session. |
| `LABELS_SUBJECT` | `typology_label` | `transaction` | yes | Links a label to the node it labels. The end is polymorphic - a label can point at an individual, entity, account, card, or transaction. A neo4j-admin END_ID column can only name one ID space, so the exporter fans this one table out into a file per subject type. GROUND TRUTH: hidden from the demo role. |
| `MEMBER_OF_RING` | `typology_label` | `ring` | yes | Ring membership. GROUND TRUTH: hidden from the demo role. |

### `OWNS` (individual → account) properties

| Column | Type | Description |
|---|---|---|
| `share` | double | Ownership share, 0-1. |
| `is_primary` | boolean | Primary account holder. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `OWNS` (legal_entity → account) properties

| Column | Type | Description |
|---|---|---|
| `share` | double | Ownership share, 0-1. |
| `is_primary` | boolean | Primary account holder. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `BENEFICIAL_OWNER_OF` (individual → legal_entity) properties

| Column | Type | Description |
|---|---|---|
| `pct` | double | Beneficial ownership percentage, 0-100. |
| `is_disclosed` | boolean | Disclosed to the institution at onboarding. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `BENEFICIAL_OWNER_OF` (legal_entity → legal_entity) properties

| Column | Type | Description |
|---|---|---|
| `pct` | double | Beneficial ownership percentage, 0-100. |
| `is_disclosed` | boolean | Disclosed to the institution at onboarding. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `DIRECTOR_OF` (individual → legal_entity) properties

| Column | Type | Description |
|---|---|---|
| `role` | string | Board role. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `EMPLOYED_BY` (individual → legal_entity) properties

| Column | Type | Description |
|---|---|---|
| `role` | string | Job role. |
| `annual_salary` | double | Gross annual salary. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `ASSOCIATE_OF` (individual → individual) properties

| Column | Type | Description |
|---|---|---|
| `relation` | string | family | household | known_associate. |
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `RESIDES_AT` (individual → address) properties

| Column | Type | Description |
|---|---|---|
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `REGISTERED_AT` (legal_entity → address) properties

| Column | Type | Description |
|---|---|---|
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `HAS_PHONE` (individual → phone_number) properties

| Column | Type | Description |
|---|---|---|
| `valid_from` | date | Simulated date this relationship became effective. Phase 1 writes and honors these columns but does not implement bitemporal querying. |
| `valid_to` | date | Simulated date this relationship ceased, or null if still effective at the end of the simulated window. |

### `LABELS_SUBJECT` (typology_label → transaction) properties

| Column | Type | Description |
|---|---|---|
| `subject_type` | string | Which node table `end_id` refers to: individual | legal_entity | account | card | transaction. A routing column for the exporter's fan-out, not emitted as a relationship property. |
