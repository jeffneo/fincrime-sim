"""Canonical dataset schema.

This module is authoritative. Parquet schemas, ``neo4j-admin import`` CSV
headers, the Cypher constraint file, and the shipped data dictionary are all
generated from the tables declared here, so they cannot drift from each other
or from the data.

Two invariants are enforced by tests rather than convention:

1. Ground truth lives only in tables marked ``ground_truth=True``, whose graph
   nodes carry the ``:GroundTruth`` marker label. No business table may contain
   a label-shaped column (PHASE1-PLAN.md D5/D5').
2. Every identifier column is drawn from a range that can never be validly
   issued in the real world, so no row can collide with a real-world
   identifier (spec §9.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import pyarrow as pa

# ---------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------


class ColType(StrEnum):
    """Column type, mapped to both Arrow and neo4j-admin import types."""

    STRING = "string"
    LONG = "long"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    STRING_LIST = "string[]"


_ARROW: dict[ColType, pa.DataType] = {
    ColType.STRING: pa.string(),
    ColType.LONG: pa.int64(),
    ColType.DOUBLE: pa.float64(),
    ColType.BOOLEAN: pa.bool_(),
    ColType.DATE: pa.date32(),
    # Neo4j datetimes carry an offset; microsecond UTC keeps Parquet readers
    # and cypher round-trips agreeing without a timezone-inference step.
    ColType.DATETIME: pa.timestamp("us", tz="UTC"),
    ColType.STRING_LIST: pa.list_(pa.string()),
}

# neo4j-admin import header type names. Identical strings for most types;
# spelled out so a future divergence is a one-line change here.
_NEO4J: dict[ColType, str] = {
    ColType.STRING: "string",
    ColType.LONG: "long",
    ColType.DOUBLE: "double",
    ColType.BOOLEAN: "boolean",
    ColType.DATE: "date",
    ColType.DATETIME: "datetime",
    ColType.STRING_LIST: "string[]",
}


@dataclass(frozen=True, slots=True)
class Col:
    name: str
    type: ColType
    doc: str
    #: Part of the node/edge key. Exactly one per node table.
    key: bool = False
    #: Foreign key into another node table, as ``"table_name"``.
    ref: str | None = None
    #: Synthetic-identifier control applied to this column, for the privacy
    #: audit to verify. See ``identifiers.py``.
    id_control: str | None = None
    #: True if the column is indexed in Neo4j.
    indexed: bool = False

    @property
    def arrow(self) -> pa.Field:
        return pa.field(self.name, _ARROW[self.type], nullable=not self.key)


@dataclass(frozen=True, slots=True)
class NodeTable:
    name: str
    label: str
    doc: str
    columns: list[Col]
    #: Extra Neo4j labels beyond ``label``. Ground-truth tables get
    #: ``GroundTruth``, which is the single RBAC control point.
    extra_labels: list[str] = field(default_factory=list)
    ground_truth: bool = False

    @property
    def labels(self) -> list[str]:
        return [self.label, *self.extra_labels]

    @property
    def key(self) -> Col:
        keys = [c for c in self.columns if c.key]
        if len(keys) != 1:
            raise ValueError(f"node table {self.name!r} must have exactly 1 key, found {len(keys)}")
        return keys[0]

    def arrow_schema(self) -> pa.Schema:
        return pa.schema([c.arrow for c in self.columns])

    def import_header(self) -> list[str]:
        """neo4j-admin import header row.

        The key column is declared in an ID space named after the label, so
        ids only have to be unique within a node type rather than globally.
        """
        out = []
        for c in self.columns:
            if c.key:
                out.append(f"{c.name}:ID({self.label})")
            else:
                out.append(f"{c.name}:{_NEO4J[c.type]}")
        out.append(":LABEL")
        return out


@dataclass(frozen=True, slots=True)
class EdgeTable:
    name: str
    rel_type: str
    start: str  # node table name
    end: str  # node table name
    doc: str
    columns: list[Col] = field(default_factory=list)
    ground_truth: bool = False

    def arrow_schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("start_id", pa.string(), nullable=False),
                pa.field("end_id", pa.string(), nullable=False),
                *[c.arrow for c in self.columns],
            ]
        )

    def import_header(self, nodes: dict[str, NodeTable]) -> list[str]:
        return [
            f"start_id:START_ID({nodes[self.start].label})",
            f"end_id:END_ID({nodes[self.end].label})",
            *[f"{c.name}:{_NEO4J[c.type]}" for c in self.columns],
            ":TYPE",
        ]


# ---------------------------------------------------------------------------
# Shared column fragments
# ---------------------------------------------------------------------------


def _valid_from() -> Col:
    return Col(
        "valid_from",
        ColType.DATE,
        "Simulated date this relationship became effective. Phase 1 writes and "
        "honors these columns but does not implement bitemporal querying.",
    )


def _valid_to() -> Col:
    return Col(
        "valid_to",
        ColType.DATE,
        "Simulated date this relationship ceased, or null if still effective at "
        "the end of the simulated window.",
    )


# ---------------------------------------------------------------------------
# Node tables - business graph
# ---------------------------------------------------------------------------

NODE_TABLES: list[NodeTable] = [
    NodeTable(
        name="institution",
        label="Institution",
        doc="A financial institution. Phase 1 simulates exactly one; the table "
        "exists now so Phase 2 multi-institution is an extension rather than "
        "a schema migration.",
        columns=[
            Col("institution_id", ColType.STRING, "Synthetic institution id.", key=True),
            Col("name", ColType.STRING, "Institution name."),
            Col("country", ColType.STRING, "ISO 3166-1 alpha-2 country of domicile."),
            Col(
                "rssd_id",
                ColType.STRING,
                "Federal Reserve RSSD-style identifier.",
                id_control="reserved_rssd",
            ),
            Col("control_policy", ColType.STRING, "Name of the control policy profile applied."),
        ],
    ),
    NodeTable(
        name="jurisdiction",
        label="Jurisdiction",
        doc="A country or territory with a configurable AML risk rating, used to "
        "drive counterparty selection and layering destinations.",
        columns=[
            Col("code", ColType.STRING, "ISO 3166-1 alpha-2 code.", key=True),
            Col("name", ColType.STRING, "Jurisdiction name."),
            Col(
                "risk_rating",
                ColType.STRING,
                "Configured AML risk rating: low | medium | high. A simulation "
                "parameter, not an assertion about the real jurisdiction.",
            ),
            Col("is_secrecy_haven", ColType.BOOLEAN, "Configured financial-secrecy flag."),
        ],
    ),
    NodeTable(
        name="address",
        label="Address",
        doc="A postal address. Shared-address links are a primary signal for "
        "mule and synthetic-identity rings, so addresses are first-class "
        "nodes rather than entity properties.",
        columns=[
            Col("address_id", ColType.STRING, "Synthetic address id.", key=True),
            Col(
                "street",
                ColType.STRING,
                "Street line, composed synthetically.",
                id_control="synthetic_composition",
            ),
            Col("city", ColType.STRING, "City name."),
            Col("state", ColType.STRING, "US state code."),
            Col(
                "postcode",
                ColType.STRING,
                "5-digit ZIP drawn from unassigned ranges.",
                id_control="unassigned_zip",
            ),
            Col("country", ColType.STRING, "ISO 3166-1 alpha-2 country."),
            Col("is_cmra", ColType.BOOLEAN, "Commercial mail-receiving agency (drop box)."),
        ],
    ),
    NodeTable(
        name="phone_number",
        label="PhoneNumber",
        doc="A telephone number. Shared-phone links carry the same signal weight "
        "as shared addresses and devices.",
        columns=[
            Col(
                "phone_id",
                ColType.STRING,
                "Synthetic phone id.",
                key=True,
            ),
            Col(
                "e164",
                ColType.STRING,
                "E.164 number using the NANP 555-01xx range reserved for "
                "fictitious use, so no row can ring a real subscriber.",
                id_control="reserved_555_range",
                indexed=True,
            ),
            Col("line_type", ColType.STRING, "mobile | landline | voip."),
        ],
    ),
    NodeTable(
        name="individual",
        label="Individual",
        doc="A natural person. All attributes are generated; none is conditioned "
        "on any real record.",
        columns=[
            Col("individual_id", ColType.STRING, "Synthetic person id.", key=True),
            Col(
                "full_name",
                ColType.STRING,
                "Name composed from synthetic name parts. Any collision with a "
                "real person's name is coincidental and all other attributes "
                "are independent of it.",
                id_control="synthetic_composition",
            ),
            Col("date_of_birth", ColType.DATE, "Date of birth."),
            Col(
                "tax_id",
                ColType.STRING,
                "SSN-format identifier using a 900-999 area prefix, which the "
                "SSA has never issued and never will.",
                id_control="never_issued_ssn",
                indexed=True,
            ),
            Col("occupation", ColType.STRING, "Occupation label."),
            Col("naics_employer", ColType.STRING, "NAICS code of employer industry."),
            Col("income_band", ColType.STRING, "Annual income band."),
            Col("annual_income", ColType.DOUBLE, "Annual income in account currency."),
            Col("kyc_tier", ColType.LONG, "Institution KYC tier, 1 (basic) to 3 (enhanced)."),
            Col("onboarded_on", ColType.DATE, "Date the customer relationship opened."),
            Col("is_pep", ColType.BOOLEAN, "Politically exposed person flag as recorded by KYC."),
            Col(
                "residency_country",
                ColType.STRING,
                "ISO 3166-1 alpha-2 country of residence.",
                ref="jurisdiction",
            ),
            Col(
                "behavior_archetype", ColType.STRING, "Behavioral profile driving normal activity."
            ),
        ],
    ),
    NodeTable(
        name="legal_entity",
        label="LegalEntity",
        doc="A company, trust, or other legal person, including shells. Whether "
        "an entity is a shell is ground truth and is NOT recorded here.",
        columns=[
            Col("entity_id", ColType.STRING, "Synthetic entity id.", key=True),
            Col(
                "legal_name",
                ColType.STRING,
                "Company name composed synthetically.",
                id_control="synthetic_composition",
            ),
            Col(
                "registration_number",
                ColType.STRING,
                "Registry number in a format no real registry issues.",
                id_control="reserved_registration",
                indexed=True,
            ),
            Col(
                "tax_id",
                ColType.STRING,
                "EIN-format identifier using an unassigned prefix.",
                id_control="unassigned_ein",
            ),
            Col("entity_type", ColType.STRING, "llc | corp | trust | partnership | sole_prop."),
            Col("naics", ColType.STRING, "NAICS industry code."),
            Col("incorporated_on", ColType.DATE, "Date of incorporation."),
            Col(
                "incorporation_country",
                ColType.STRING,
                "ISO 3166-1 alpha-2 country of incorporation.",
                ref="jurisdiction",
            ),
            Col("declared_annual_revenue", ColType.DOUBLE, "Revenue as declared at onboarding."),
            Col("declared_employee_count", ColType.LONG, "Employee count as declared."),
            Col("kyc_tier", ColType.LONG, "Institution KYC tier, 1 to 3."),
            Col("onboarded_on", ColType.DATE, "Date the customer relationship opened."),
            Col(
                "is_cash_intensive", ColType.BOOLEAN, "Cash-intensive business per its NAICS code."
            ),
            Col(
                "behavior_archetype", ColType.STRING, "Behavioral profile driving normal activity."
            ),
        ],
    ),
    NodeTable(
        name="account",
        label="Account",
        doc="A deposit, savings, or brokerage account at an institution. Cards "
        "are modeled separately and hang off an account.",
        columns=[
            Col("account_id", ColType.STRING, "Synthetic account id.", key=True),
            Col(
                "iban",
                ColType.STRING,
                "IBAN-format string with a deliberately invalid check digit, so "
                "it can never validate as a real account.",
                id_control="invalid_checkdigit_iban",
                indexed=True,
            ),
            Col("institution_id", ColType.STRING, "Owning institution.", ref="institution"),
            Col("account_type", ColType.STRING, "checking | savings | brokerage | escrow."),
            Col("currency", ColType.STRING, "ISO 4217 currency code."),
            Col("opened_on", ColType.DATE, "Account opening date.", indexed=True),
            Col("closed_on", ColType.DATE, "Account closing date, null if open."),
            Col("status", ColType.STRING, "open | dormant | closed | frozen."),
            Col("opening_balance", ColType.DOUBLE, "Balance at the start of the simulated window."),
            Col("monitoring_segment", ColType.STRING, "Segment the institution monitors it under."),
        ],
    ),
    NodeTable(
        name="card",
        label="Card",
        doc="A payment card issued against an account. Separate from Account "
        "because card-not-present fraud operates at PAN level.",
        columns=[
            Col("card_id", ColType.STRING, "Synthetic card id.", key=True),
            Col(
                "pan_masked",
                ColType.STRING,
                "Masked PAN. The underlying number is Luhn-valid but issued from "
                "a reserved test BIN, so it is not routable on any network.",
                id_control="reserved_test_bin",
            ),
            Col("account_id", ColType.STRING, "Funding account.", ref="account"),
            Col("card_type", ColType.STRING, "debit | credit | prepaid."),
            Col("network", ColType.STRING, "Card network label."),
            Col("issued_on", ColType.DATE, "Issue date."),
            Col("expires_on", ColType.DATE, "Expiry date."),
            Col("status", ColType.STRING, "active | blocked | expired | reissued."),
            Col("credit_limit", ColType.DOUBLE, "Credit limit, null for debit."),
        ],
    ),
    NodeTable(
        name="merchant",
        label="Merchant",
        doc="A merchant accepting card payments, as a transaction counterparty.",
        columns=[
            Col("merchant_id", ColType.STRING, "Synthetic merchant id.", key=True),
            Col(
                "name",
                ColType.STRING,
                "Merchant name composed synthetically.",
                id_control="synthetic_composition",
            ),
            Col("mcc", ColType.STRING, "ISO 18245 merchant category code.", indexed=True),
            Col("country", ColType.STRING, "ISO 3166-1 alpha-2 country.", ref="jurisdiction"),
            Col("channel_mix", ColType.STRING, "card_present | cnp | hybrid."),
            Col("acquirer_risk_tier", ColType.STRING, "Acquirer-assigned risk tier."),
        ],
    ),
    NodeTable(
        name="device",
        label="Device",
        doc="A client device used to initiate transactions. Device sharing across "
        "unrelated customers is a primary mule and fraud-ring signal.",
        columns=[
            Col("device_id", ColType.STRING, "Synthetic device id.", key=True),
            Col("fingerprint", ColType.STRING, "Device fingerprint hash.", indexed=True),
            Col("device_type", ColType.STRING, "mobile | desktop | tablet."),
            Col("os", ColType.STRING, "Operating system family and version."),
            Col("first_seen", ColType.DATETIME, "First time the device appeared."),
            Col("is_emulator", ColType.BOOLEAN, "Device presents as an emulator."),
        ],
    ),
    NodeTable(
        name="ip_address",
        label="IpAddress",
        doc="A source IP observed on a session.",
        columns=[
            Col("ip_id", ColType.STRING, "Synthetic IP id.", key=True),
            Col(
                "address",
                ColType.STRING,
                "IPv4 address drawn from RFC 5737 documentation ranges "
                "(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), which are "
                "reserved and never routed to a real host.",
                id_control="rfc5737_documentation_range",
                indexed=True,
            ),
            Col("asn", ColType.LONG, "Autonomous system number."),
            Col("country", ColType.STRING, "Geolocated country, ISO 3166-1 alpha-2."),
            Col("is_proxy", ColType.BOOLEAN, "Known proxy, VPN, or Tor exit."),
        ],
    ),
    NodeTable(
        name="transaction",
        label="Transaction",
        doc="A single movement of value, reified as a node so it can carry "
        "multi-party context (device, IP, merchant) and be labeled "
        "independently of the accounts involved (PHASE1-PLAN.md D4).",
        columns=[
            Col("txn_id", ColType.STRING, "Synthetic transaction id.", key=True),
            Col("amount", ColType.DOUBLE, "Signed amount in `currency`."),
            Col("currency", ColType.STRING, "ISO 4217 currency code."),
            Col("amount_usd", ColType.DOUBLE, "Amount converted to USD for thresholding."),
            Col("booked_at", ColType.DATETIME, "Booking timestamp, UTC.", indexed=True),
            Col("value_date", ColType.DATE, "Value date."),
            Col(
                "channel",
                ColType.STRING,
                "cash | ach | wire | card_present | card_cnp | p2p | internal | check.",
                indexed=True,
            ),
            Col(
                "direction", ColType.STRING, "debit | credit, relative to the originating account."
            ),
            Col("memo", ColType.STRING, "Free-text payment reference."),
            Col("txn_class", ColType.STRING, "Purpose class, e.g. payroll, retail, transfer."),
            Col(
                "ctr_reportable",
                ColType.BOOLEAN,
                "Institution determined this transaction meets the USD 10,000 "
                "currency-transaction-report threshold. An institution control "
                "output, not a crime label.",
            ),
            Col("declined", ColType.BOOLEAN, "Authorization declined."),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Node tables - ground truth
#
# These carry the :GroundTruth marker label. neo4j/nes-setup.cypher denies the
# demo role traverse and read on that label, which hides every node below in a
# single rule.
# ---------------------------------------------------------------------------

GROUND_TRUTH_TABLES: list[NodeTable] = [
    NodeTable(
        name="ring",
        label="Ring",
        doc="One injected typology instance - a criminal ring, network, or scheme. "
        "GROUND TRUTH: hidden from the demo role.",
        extra_labels=["GroundTruth"],
        ground_truth=True,
        columns=[
            Col("ring_id", ColType.STRING, "Ring id.", key=True),
            Col("typology", ColType.STRING, "Typology generator that produced it.", indexed=True),
            Col(
                "difficulty_tier",
                ColType.STRING,
                "easy | medium | hard. Determined by the generator parameters "
                "used, not assigned after the fact (D6).",
            ),
            Col("knob_digest", ColType.STRING, "Hash of the exact knob values used."),
            Col("injected_from", ColType.DATE, "Start of the injection window."),
            Col("injected_to", ColType.DATE, "End of the injection window."),
            Col("member_count", ColType.LONG, "Number of labeled subjects in the ring."),
            Col("illicit_txn_count", ColType.LONG, "Number of labeled transactions."),
            Col("illicit_amount_usd", ColType.DOUBLE, "Total labeled value moved, USD."),
        ],
    ),
    NodeTable(
        name="typology_label",
        label="TypologyLabel",
        doc="One label attached to one subject (entity, account, card, or "
        "transaction). Multi-valued by design: a subject may carry several "
        "labels, including being both a hard negative and a ring member. "
        "GROUND TRUTH: hidden from the demo role.",
        extra_labels=["GroundTruth"],
        ground_truth=True,
        columns=[
            Col("label_id", ColType.STRING, "Label id.", key=True),
            Col("ring_id", ColType.STRING, "Ring this label belongs to.", ref="ring"),
            Col("subject_id", ColType.STRING, "Id of the labeled node.", indexed=True),
            Col(
                "subject_type",
                ColType.STRING,
                "individual | legal_entity | account | card | transaction.",
            ),
            Col("typology", ColType.STRING, "Typology name.", indexed=True),
            Col(
                "role",
                ColType.STRING,
                "Role within the typology, e.g. smurf, collector, shell_tier2, "
                "mule, fraudster_device, victim.",
            ),
            Col(
                "polarity",
                ColType.STRING,
                "illicit | hard_negative. Hard negatives are legitimate subjects "
                "deliberately built to resemble the typology (D7).",
                indexed=True,
            ),
            Col(
                "confidence",
                ColType.DOUBLE,
                "0-1 ambiguity score. Below 1.0 where the generator itself "
                "produced a genuinely ambiguous case (spec §7).",
            ),
            Col("difficulty_tier", ColType.STRING, "easy | medium | hard."),
        ],
    ),
    NodeTable(
        name="case_narrative",
        label="CaseNarrative",
        doc="Template-generated investigation summary for one ring. M6 stub: "
        "template-driven, not a generative implementation (that is Phase 3). "
        "GROUND TRUTH: hidden from the demo role.",
        extra_labels=["GroundTruth"],
        ground_truth=True,
        columns=[
            Col("narrative_id", ColType.STRING, "Narrative id.", key=True),
            Col("ring_id", ColType.STRING, "Ring described.", ref="ring"),
            Col("summary", ColType.STRING, "Narrative text."),
            Col("template_version", ColType.STRING, "Template version used."),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Edge tables
# ---------------------------------------------------------------------------

EDGE_TABLES: list[EdgeTable] = [
    EdgeTable(
        name="individual_owns_account",
        rel_type="OWNS",
        start="individual",
        end="account",
        doc="Individual account ownership, including joint ownership.",
        columns=[
            Col("share", ColType.DOUBLE, "Ownership share, 0-1."),
            Col("is_primary", ColType.BOOLEAN, "Primary account holder."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="entity_owns_account",
        rel_type="OWNS",
        start="legal_entity",
        end="account",
        doc="Legal-entity account ownership.",
        columns=[
            Col("share", ColType.DOUBLE, "Ownership share, 0-1."),
            Col("is_primary", ColType.BOOLEAN, "Primary account holder."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="individual_bo_of_entity",
        rel_type="BENEFICIAL_OWNER_OF",
        start="individual",
        end="legal_entity",
        doc="Individual beneficial ownership of a legal entity.",
        columns=[
            Col("pct", ColType.DOUBLE, "Beneficial ownership percentage, 0-100."),
            Col("is_disclosed", ColType.BOOLEAN, "Disclosed to the institution at onboarding."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="entity_bo_of_entity",
        rel_type="BENEFICIAL_OWNER_OF",
        start="legal_entity",
        end="legal_entity",
        doc="Entity-to-entity beneficial ownership. Chains of these are what "
        "obscure ultimate beneficial ownership in layering typologies - and "
        "also what legitimate holding structures look like.",
        columns=[
            Col("pct", ColType.DOUBLE, "Beneficial ownership percentage, 0-100."),
            Col("is_disclosed", ColType.BOOLEAN, "Disclosed to the institution at onboarding."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="director_of",
        rel_type="DIRECTOR_OF",
        start="individual",
        end="legal_entity",
        doc="Directorship. Shared directors across otherwise unconnected "
        "entities is a shell-network signal.",
        columns=[Col("role", ColType.STRING, "Board role."), _valid_from(), _valid_to()],
    ),
    EdgeTable(
        name="employed_by",
        rel_type="EMPLOYED_BY",
        start="individual",
        end="legal_entity",
        doc="Employment, which drives payroll transaction streams.",
        columns=[
            Col("role", ColType.STRING, "Job role."),
            Col("annual_salary", ColType.DOUBLE, "Gross annual salary."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="associate_of",
        rel_type="ASSOCIATE_OF",
        start="individual",
        end="individual",
        doc="Family or known-associate link, used for PEP proximity and recruitment networks.",
        columns=[
            Col("relation", ColType.STRING, "family | household | known_associate."),
            _valid_from(),
            _valid_to(),
        ],
    ),
    EdgeTable(
        name="individual_resides_at",
        rel_type="RESIDES_AT",
        start="individual",
        end="address",
        doc="Individual address history.",
        columns=[_valid_from(), _valid_to()],
    ),
    EdgeTable(
        name="entity_registered_at",
        rel_type="REGISTERED_AT",
        start="legal_entity",
        end="address",
        doc="Registered address of a legal entity. Many entities at one address "
        "is a shell-network signal - and also a real serviced-office signal.",
        columns=[_valid_from(), _valid_to()],
    ),
    EdgeTable(
        name="individual_has_phone",
        rel_type="HAS_PHONE",
        start="individual",
        end="phone_number",
        doc="Individual contact number.",
        columns=[_valid_from(), _valid_to()],
    ),
    EdgeTable(
        name="txn_from",
        rel_type="FROM",
        start="transaction",
        end="account",
        doc="Originating account of a transaction.",
    ),
    EdgeTable(
        name="txn_to",
        rel_type="TO",
        start="transaction",
        end="account",
        doc="Beneficiary account of a transaction. Absent for cash withdrawals "
        "and for payments leaving the simulated institution.",
    ),
    EdgeTable(
        name="txn_at_merchant",
        rel_type="AT_MERCHANT",
        start="transaction",
        end="merchant",
        doc="Merchant leg of a card transaction.",
    ),
    EdgeTable(
        name="txn_on_card",
        rel_type="ON_CARD",
        start="transaction",
        end="card",
        doc="Card used, for card transactions.",
    ),
    EdgeTable(
        name="txn_via_device",
        rel_type="VIA_DEVICE",
        start="transaction",
        end="device",
        doc="Device that initiated the transaction, where the channel has one.",
    ),
    EdgeTable(
        name="txn_via_ip",
        rel_type="VIA_IP",
        start="transaction",
        end="ip_address",
        doc="Source IP of the initiating session.",
    ),
    # --- Ground truth edges. Denied to the demo role by relationship type. ---
    EdgeTable(
        name="label_subject",
        rel_type="LABELS_SUBJECT",
        start="typology_label",
        # Polymorphic. `end` names the largest target table so the declared
        # schema is well-formed; the real target is per-row and is resolved
        # from `subject_type` below.
        end="transaction",
        doc="Links a label to the node it labels. The end is polymorphic - a "
        "label can point at an individual, entity, account, card, or "
        "transaction. A neo4j-admin END_ID column can only name one ID "
        "space, so the exporter fans this one table out into a file per "
        "subject type. GROUND TRUTH: hidden from the demo role.",
        ground_truth=True,
        columns=[
            Col(
                "subject_type",
                ColType.STRING,
                "Which node table `end_id` refers to: individual | legal_entity "
                "| account | card | transaction. A routing column for the "
                "exporter's fan-out, not emitted as a relationship property.",
            ),
        ],
    ),
    EdgeTable(
        name="ring_member",
        rel_type="MEMBER_OF_RING",
        start="typology_label",
        end="ring",
        doc="Ring membership. GROUND TRUTH: hidden from the demo role.",
        ground_truth=True,
    ),
]


# ---------------------------------------------------------------------------
# Lookups and guards
# ---------------------------------------------------------------------------

ALL_NODE_TABLES: list[NodeTable] = [*NODE_TABLES, *GROUND_TRUTH_TABLES]

NODES_BY_NAME: dict[str, NodeTable] = {t.name: t for t in ALL_NODE_TABLES}
EDGES_BY_NAME: dict[str, EdgeTable] = {t.name: t for t in EDGE_TABLES}

#: The marker label that the RBAC deny rule in neo4j/nes-setup.cypher targets.
GROUND_TRUTH_LABEL = "GroundTruth"

#: Substrings that indicate a column is leaking ground truth. A business table
#: containing any of these fails tests/test_schema.py. The point is to make
#: D5' a build failure rather than a code-review habit.
LEAKY_COLUMN_TOKENS = frozenset(
    {
        "typology",
        "ring_id",
        "is_illicit",
        "illicit",
        "is_fraud",
        "is_laundering",
        "is_shell",
        "is_mule",
        "difficulty",
        "label",
        "ground_truth",
        "suspicious",
        "polarity",
    }
)

#: Business columns whose names contain a leaky token but are legitimately part
#: of the business graph. Each needs a reason, because every entry is a hole in
#: the D5' guard.
LEAK_GUARD_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("individual", "income_band"): "'band' is not a leak token; present for clarity only",
    ("merchant", "acquirer_risk_tier"): (
        "An acquirer's own commercial risk tier is data a real bank sees, not a "
        "crime label. It must not correlate perfectly with any typology - "
        "enforced at M5 by checking its mutual information against labels."
    ),
    ("phone_number", "line_type"): "'line_type' matches no token; listed for symmetry",
}


def business_tables() -> list[NodeTable]:
    return [t for t in ALL_NODE_TABLES if not t.ground_truth]


def ground_truth_tables() -> list[NodeTable]:
    return [t for t in ALL_NODE_TABLES if t.ground_truth]


def id_controls() -> dict[str, list[tuple[str, str]]]:
    """Map each identifier control to the ``(table, column)`` pairs using it.

    Consumed by the privacy audit (``fincrime validate --privacy``), which
    checks that generated values actually fall inside the reserved range each
    control claims.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for t in ALL_NODE_TABLES:
        for c in t.columns:
            if c.id_control:
                out.setdefault(c.id_control, []).append((t.name, c.name))
    return out
