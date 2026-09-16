"""Reference taxonomies and behavioral archetypes.

Codes here are real public taxonomies (ISO 3166, NAICS 2022, ISO 18245 MCC).
The entities assigned to them are synthetic; a published code list is not
personal data and using the real ones is what makes the dataset joinable
against a customer's own reference data during a demo.

Jurisdiction risk ratings are **simulation parameters**, not assertions about
real countries. They are set in this file rather than sourced from any real
list so that nothing in the dataset can be read as a claim about a real place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Jurisdictions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    code: str
    name: str
    risk_rating: str
    is_secrecy_haven: bool
    #: Relative weight as a cross-border counterparty for ordinary business.
    trade_weight: float


#: Phase 1 is single-institution and US-domiciled, so the domestic jurisdiction
#: carries almost all activity; the rest exist to give cross-border payments
#: somewhere plausible to go, and to give the layering typology (M3) a ladder
#: of risk ratings to climb.
JURISDICTIONS: tuple[Jurisdiction, ...] = (
    Jurisdiction("US", "United States", "low", False, 100.0),
    Jurisdiction("CA", "Canada", "low", False, 8.0),
    Jurisdiction("GB", "United Kingdom", "low", False, 7.0),
    Jurisdiction("DE", "Germany", "low", False, 6.0),
    Jurisdiction("FR", "France", "low", False, 4.0),
    Jurisdiction("NL", "Netherlands", "low", False, 4.0),
    Jurisdiction("IE", "Ireland", "low", False, 3.0),
    Jurisdiction("JP", "Japan", "low", False, 4.0),
    Jurisdiction("AU", "Australia", "low", False, 3.0),
    Jurisdiction("SG", "Singapore", "medium", True, 5.0),
    Jurisdiction("HK", "Hong Kong SAR", "medium", True, 5.0),
    Jurisdiction("CH", "Switzerland", "medium", True, 4.0),
    Jurisdiction("LU", "Luxembourg", "medium", True, 3.0),
    Jurisdiction("AE", "United Arab Emirates", "medium", True, 3.0),
    Jurisdiction("MX", "Mexico", "medium", False, 5.0),
    Jurisdiction("BR", "Brazil", "medium", False, 3.0),
    Jurisdiction("IN", "India", "medium", False, 4.0),
    Jurisdiction("TR", "Turkey", "medium", False, 2.0),
    Jurisdiction("CY", "Cyprus", "high", True, 1.0),
    Jurisdiction("MT", "Malta", "high", True, 1.0),
    Jurisdiction("PA", "Panama", "high", True, 1.0),
    Jurisdiction("KY", "Cayman Islands", "high", True, 1.5),
    Jurisdiction("VG", "British Virgin Islands", "high", True, 1.5),
    Jurisdiction("BS", "Bahamas", "high", True, 1.0),
    Jurisdiction("SC", "Seychelles", "high", True, 0.8),
    Jurisdiction("BZ", "Belize", "high", True, 0.6),
)

DOMESTIC = "US"

# ---------------------------------------------------------------------------
# Industry (NAICS 2022) and merchant category (ISO 18245)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Industry:
    naics: str
    label: str
    #: Share of legal entities in this industry.
    weight: float
    #: Fraction of revenue that arrives as physical cash. The single most
    #: important firmographic parameter in the model: it is what makes the
    #: structuring typology (T1) hard to separate from a legitimate
    #: cash-intensive business (the M4 hard negative).
    cash_ratio: float
    median_revenue: float
    median_employees: int


INDUSTRIES: tuple[Industry, ...] = (
    Industry("722511", "Full-Service Restaurants", 0.08, 0.28, 950_000, 22),
    Industry("722513", "Limited-Service Restaurants", 0.06, 0.34, 700_000, 16),
    Industry("812310", "Coin-Operated Laundries", 0.02, 0.72, 240_000, 3),
    Industry("445110", "Supermarkets and Grocery Stores", 0.04, 0.22, 3_200_000, 34),
    Industry("447110", "Gasoline Stations with Convenience Stores", 0.03, 0.26, 2_400_000, 12),
    Industry("713990", "Other Amusement and Recreation", 0.02, 0.41, 480_000, 9),
    Industry("812111", "Barber Shops and Salons", 0.03, 0.38, 180_000, 5),
    Industry("541110", "Offices of Lawyers", 0.05, 0.01, 820_000, 7),
    Industry("541211", "Offices of Certified Public Accountants", 0.04, 0.01, 640_000, 6),
    Industry("541511", "Custom Computer Programming Services", 0.07, 0.00, 1_100_000, 9),
    Industry("541611", "Management Consulting Services", 0.06, 0.01, 740_000, 6),
    Industry("531210", "Offices of Real Estate Agents", 0.04, 0.02, 520_000, 5),
    Industry("523930", "Investment Advice", 0.02, 0.00, 1_400_000, 6),
    Industry("551112", "Offices of Other Holding Companies", 0.03, 0.00, 2_800_000, 2),
    Industry("423990", "Other Durable Goods Merchant Wholesalers", 0.05, 0.03, 4_100_000, 18),
    Industry("424490", "Other Grocery Merchant Wholesalers", 0.04, 0.05, 5_600_000, 24),
    Industry("484121", "General Freight Trucking, Long-Distance", 0.04, 0.02, 1_900_000, 14),
    Industry("236220", "Commercial Building Construction", 0.05, 0.06, 6_200_000, 28),
    Industry("238220", "Plumbing and HVAC Contractors", 0.05, 0.11, 1_300_000, 11),
    Industry("621111", "Offices of Physicians", 0.04, 0.02, 1_700_000, 12),
    Industry("611430", "Professional and Management Training", 0.02, 0.03, 380_000, 4),
    Industry("561720", "Janitorial Services", 0.03, 0.14, 420_000, 17),
    Industry("811111", "General Automotive Repair", 0.03, 0.24, 560_000, 7),
    Industry("453998", "All Other Miscellaneous Retail", 0.06, 0.19, 310_000, 5),
)

#: Industries treated as cash-intensive by the institution's own risk model.
#: A property of the business, not a crime label - most cash-intensive
#: businesses are entirely legitimate, which is the whole point.
CASH_INTENSIVE_THRESHOLD = 0.20


@dataclass(frozen=True, slots=True)
class MerchantCategory:
    mcc: str
    label: str
    weight: float
    #: Probability a transaction at this merchant is card-not-present.
    cnp_rate: float
    #: Log-normal parameters for the ticket size, in USD.
    amount_mu: float
    amount_sigma: float


MERCHANT_CATEGORIES: tuple[MerchantCategory, ...] = (
    MerchantCategory("5411", "Grocery Stores, Supermarkets", 0.14, 0.08, 3.9, 0.7),
    MerchantCategory("5812", "Eating Places, Restaurants", 0.12, 0.18, 3.4, 0.8),
    MerchantCategory("5814", "Fast Food Restaurants", 0.09, 0.22, 2.5, 0.6),
    MerchantCategory("5541", "Service Stations", 0.08, 0.04, 3.7, 0.5),
    MerchantCategory("5912", "Drug Stores and Pharmacies", 0.06, 0.12, 3.2, 0.9),
    MerchantCategory("5311", "Department Stores", 0.05, 0.35, 4.1, 0.9),
    MerchantCategory("5651", "Family Clothing Stores", 0.05, 0.42, 3.9, 0.8),
    MerchantCategory("5732", "Electronics Stores", 0.03, 0.55, 4.9, 1.0),
    MerchantCategory("5999", "Miscellaneous Retail", 0.07, 0.48, 3.6, 1.0),
    MerchantCategory("4121", "Taxicabs and Limousines", 0.04, 0.92, 3.0, 0.6),
    MerchantCategory("4899", "Cable and Streaming Services", 0.04, 1.00, 2.7, 0.4),
    MerchantCategory("5968", "Direct Marketing, Subscription", 0.04, 1.00, 3.0, 0.7),
    MerchantCategory("7011", "Lodging, Hotels", 0.03, 0.72, 5.2, 0.8),
    MerchantCategory("4511", "Airlines", 0.03, 0.95, 5.7, 0.8),
    MerchantCategory("5942", "Book Stores", 0.02, 0.58, 3.2, 0.7),
    MerchantCategory("7230", "Beauty and Barber Shops", 0.03, 0.10, 3.7, 0.6),
    MerchantCategory("8062", "Hospitals", 0.02, 0.30, 5.4, 1.2),
    MerchantCategory("5941", "Sporting Goods Stores", 0.03, 0.45, 4.0, 0.8),
    MerchantCategory("7299", "Miscellaneous Personal Services", 0.03, 0.40, 3.8, 0.9),
)

#: MCCs where card-not-present fraud concentrates (M3, T4). Listed here so the
#: typology draws from the same taxonomy the background does rather than
#: inventing its own merchant pool - a fraud ring that only ever touches
#: merchants no legitimate customer uses would be trivially separable.
CNP_FRAUD_PREFERRED_MCC = ("5732", "5999", "5968", "4511", "5311")


# ---------------------------------------------------------------------------
# Behavioral archetypes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetailArchetype:
    """Behavior profile for an individual.

    ``income_mu``/``income_sigma`` are log-normal parameters for annual income.
    Discretionary card spend is driven by ``card_txn_per_month``; the amount
    distribution comes from the merchant category, scaled by ``spend_scale`` so
    a high earner's grocery run is larger than a student's at the same MCC.
    """

    name: str
    weight: float
    income_mu: float
    income_sigma: float
    #: "biweekly" | "monthly" | "weekly" | "none"
    payroll_cadence: str
    card_txn_per_month: float
    cash_withdrawals_per_month: float
    spend_scale: float
    #: Probability of holding a savings account alongside the checking account.
    savings_rate: float
    #: Probability of holding a credit card as well as a debit card.
    credit_card_rate: float
    occupations: tuple[str, ...] = ()


RETAIL_ARCHETYPES: tuple[RetailArchetype, ...] = (
    RetailArchetype(
        "salaried_professional",
        0.30,
        11.25,
        0.45,
        "biweekly",
        34.0,
        1.4,
        1.0,
        0.62,
        0.58,
        ("Software Engineer", "Project Manager", "Accountant", "Nurse", "Teacher"),
    ),
    RetailArchetype(
        "hourly_worker",
        0.24,
        10.45,
        0.40,
        "weekly",
        22.0,
        4.2,
        0.55,
        0.24,
        0.21,
        ("Retail Associate", "Driver", "Warehouse Operative", "Care Assistant"),
    ),
    RetailArchetype(
        "high_earner",
        0.10,
        12.25,
        0.55,
        "monthly",
        46.0,
        0.9,
        2.4,
        0.86,
        0.88,
        ("Physician", "Attorney", "Finance Director", "Sales Director"),
    ),
    RetailArchetype(
        "self_employed",
        0.12,
        10.95,
        0.75,
        "none",
        28.0,
        3.1,
        0.95,
        0.44,
        0.42,
        ("Consultant", "Contractor", "Photographer", "Tradesperson"),
    ),
    RetailArchetype(
        "retired",
        0.14,
        10.30,
        0.45,
        "monthly",
        16.0,
        2.2,
        0.65,
        0.71,
        0.34,
        ("Retired",),
    ),
    RetailArchetype(
        "student",
        0.10,
        9.35,
        0.55,
        "none",
        19.0,
        2.6,
        0.32,
        0.18,
        0.14,
        ("Student",),
    ),
)


@dataclass(frozen=True, slots=True)
class BusinessArchetype:
    """Behavior profile for a legal entity."""

    name: str
    weight: float
    #: Inbound customer receipts per month.
    receipts_per_month: float
    #: Outbound supplier payments per month.
    supplier_payments_per_month: float
    #: Whether the business runs payroll through this institution.
    runs_payroll: bool
    #: Multiplier on the industry's cash ratio.
    cash_multiplier: float
    #: Probability of any cross-border activity at all.
    cross_border_rate: float
    naics_filter: tuple[str, ...] = field(default=())


BUSINESS_ARCHETYPES: tuple[BusinessArchetype, ...] = (
    BusinessArchetype("retail_smb", 0.34, 62.0, 18.0, True, 1.0, 0.04),
    BusinessArchetype("professional_services", 0.26, 14.0, 11.0, True, 0.3, 0.09),
    BusinessArchetype("wholesale_trade", 0.16, 28.0, 22.0, True, 0.6, 0.34),
    BusinessArchetype("construction_trades", 0.12, 9.0, 16.0, True, 1.1, 0.03),
    # Holding companies are low-volume and mostly intra-group. They are also
    # the structural twin of a layering chain, which is why the M4 hard
    # negative is built on this archetype.
    BusinessArchetype("holding_company", 0.06, 2.0, 3.0, False, 0.0, 0.22),
    BusinessArchetype("logistics", 0.06, 19.0, 15.0, True, 0.4, 0.18),
)


# ---------------------------------------------------------------------------
# Transaction classes
# ---------------------------------------------------------------------------

#: Purpose classes and the channel each normally uses. Kept as data so the
#: typology generators can emit the same classes as the background does -
#: an illicit transfer that carries a txn_class no legitimate transaction ever
#: uses would be a giveaway feature.
TXN_CLASSES: dict[str, str] = {
    "payroll": "ach",
    "rent": "ach",
    "utilities": "ach",
    "subscription": "card_cnp",
    "retail": "card_present",
    "retail_online": "card_cnp",
    "cash_withdrawal": "cash",
    "cash_deposit": "cash",
    "p2p_transfer": "p2p",
    "internal_transfer": "internal",
    "supplier_payment": "ach",
    "customer_receipt": "ach",
    "card_settlement": "ach",
    "wire_out": "wire",
    "wire_in": "wire",
    "loan_payment": "ach",
    "tax_payment": "ach",
    "insurance": "ach",
}
