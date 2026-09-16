"""Population generation: who exists, and how they are connected.

Produces every node table except ``transaction``, plus the static relationship
tables. The output is the substrate the behavior model (``behavior.py``) then
generates transaction streams over.

Everything is vectorized over numpy arrays rather than looping per entity
(PHASE1-PLAN.md D2). The agent-based model is preserved in that each entity
carries its own archetype and parameters; what is not preserved is a
per-entity, per-step scheduler, which at 100K entities buys nothing because
entities do not interact through mutable shared state.

The structural realism that matters here is **shared attributes**. Households
share addresses and devices; families share surnames; serviced offices host
many unrelated companies. Every one of those is a legitimate reason for two
customers to look connected, and each is the direct twin of a signal the
typologies rely on — so the background has to contain them in quantity or the
detection problem becomes trivial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

from . import identifiers as ids
from . import names as nm
from .config import RunConfig
from .reference import (
    BUSINESS_ARCHETYPES,
    CASH_INTENSIVE_THRESHOLD,
    DOMESTIC,
    INDUSTRIES,
    JURISDICTIONS,
    MERCHANT_CATEGORIES,
    RETAIL_ARCHETYPES,
)
from .rng import StreamRegistry

# US cities used for address geography. Real public geographic data; the
# addresses are made unreal by their house numbers (identifiers.py).
_CITIES: tuple[tuple[str, str, str], ...] = (
    ("New York", "NY", "10001"),
    ("Los Angeles", "CA", "90001"),
    ("Chicago", "IL", "60601"),
    ("Houston", "TX", "77001"),
    ("Phoenix", "AZ", "85001"),
    ("Philadelphia", "PA", "19101"),
    ("San Antonio", "TX", "78201"),
    ("San Diego", "CA", "92101"),
    ("Dallas", "TX", "75201"),
    ("Austin", "TX", "78701"),
    ("Jacksonville", "FL", "32201"),
    ("Columbus", "OH", "43201"),
    ("Charlotte", "NC", "28201"),
    ("Indianapolis", "IN", "46201"),
    ("Seattle", "WA", "98101"),
    ("Denver", "CO", "80201"),
    ("Boston", "MA", "02101"),
    ("Nashville", "TN", "37201"),
    ("Portland", "OR", "97201"),
    ("Miami", "FL", "33101"),
)

_STREETS = np.array(
    [
        "Oak",
        "Maple",
        "Cedar",
        "Pine",
        "Elm",
        "Walnut",
        "Chestnut",
        "Birch",
        "Willow",
        "Aspen",
        "Juniper",
        "Sycamore",
        "Laurel",
        "Magnolia",
        "Poplar",
        "Hawthorn",
        "Alder",
        "Rowan",
    ]
)
_STREET_TYPES = np.array(["St", "Ave", "Rd", "Blvd", "Ln", "Way", "Dr", "Ct"])

_DEVICE_OS = np.array(["iOS 18", "iOS 19", "Android 15", "Android 16", "Windows 11", "macOS 15"])
_DEVICE_TYPES = np.array(["mobile", "mobile", "mobile", "desktop", "tablet"])


@dataclass(slots=True)
class Population:
    """Generated population, as tables plus the arrays behavior generation needs.

    The tables are what gets written; the extra arrays are working state that
    would otherwise have to be recovered by joining tables back together for
    every transaction stream.
    """

    tables: dict[str, pl.DataFrame]

    # --- working state for behavior.py ---
    #: Per-account: owner index, owner kind (0 individual, 1 entity), archetype
    #: index into RETAIL_ARCHETYPES or BUSINESS_ARCHETYPES.
    account_owner: np.ndarray
    account_owner_is_entity: np.ndarray
    account_archetype: np.ndarray
    account_ids: np.ndarray
    account_type: np.ndarray
    account_opened: np.ndarray
    #: Per-individual annual income, used to scale spend.
    individual_income: np.ndarray
    #: Per-entity annual revenue and cash ratio.
    entity_revenue: np.ndarray
    entity_cash_ratio: np.ndarray
    #: Primary checking account index per owner, or -1.
    individual_primary_account: np.ndarray
    entity_primary_account: np.ndarray
    #: Card table index -> funding account index.
    card_account: np.ndarray
    card_ids: np.ndarray
    #: Device and IP pools, and each individual's device.
    device_ids: np.ndarray
    ip_ids: np.ndarray
    individual_device: np.ndarray
    #: Employment: individual -> entity index, or -1 for external/none.
    employer: np.ndarray
    merchant_ids: np.ndarray
    merchant_mcc_idx: np.ndarray
    #: Popularity weight per merchant, for counterparty selection.
    merchant_weight: np.ndarray


def _weights(items, attr: str = "weight") -> np.ndarray:
    w = np.array([getattr(i, attr) for i in items], dtype=float)
    return w / w.sum()


def _pad(prefix: str, n: int, width: int = 9) -> np.ndarray:
    return np.char.add(f"{prefix}-", np.char.zfill(np.arange(n).astype("U12"), width))


def _null_dates(n: int) -> pl.Series:
    """An all-null Date column of length ``n``.

    Explicitly typed because Polars infers a bare all-``None`` array as Binary,
    which then fails the cast to date32 on write with a message that points at
    the schema rather than at the column that produced it.
    """
    return pl.Series([None] * n, dtype=pl.Date)


def _random_dates(rng: np.random.Generator, n: int, start: date, end: date) -> np.ndarray:
    """Uniform dates in ``[start, end]`` as numpy date64."""
    span = (end - start).days
    offsets = rng.integers(0, max(span, 1) + 1, n)
    return np.datetime64(start, "D") + offsets.astype("timedelta64[D]")


def build(cfg: RunConfig, rng: StreamRegistry) -> Population:
    """Generate the full population."""
    tables: dict[str, pl.DataFrame] = {}
    n_ind = cfg.individuals
    n_ent = cfg.legal_entities

    tables["jurisdiction"] = _jurisdictions()
    tables["institution"], institution_id = _institution(cfg, rng.get("population", "institution"))

    addresses, household_of_individual, entity_address = _addresses(
        cfg, rng.get("population", "address"), n_ind, n_ent
    )
    tables["address"] = addresses

    individuals, ind_state = _individuals(
        cfg, rng.get("population", "individual"), n_ind, household_of_individual
    )
    tables["individual"] = individuals

    entities, ent_state = _entities(cfg, rng.get("population", "legal_entity"), n_ent)
    tables["legal_entity"] = entities

    phones, ind_phone = _phones(rng.get("population", "phone"), n_ind)
    tables["phone_number"] = phones

    devices, ind_device = _devices(
        cfg, rng.get("population", "device"), n_ind, household_of_individual
    )
    tables["device"] = devices

    tables["ip_address"] = _ips(rng.get("population", "ip"), n_ind)

    accounts, acct_state = _accounts(
        cfg, rng.get("population", "account"), institution_id, ind_state, ent_state
    )
    tables["account"] = accounts

    cards, card_state = _cards(cfg, rng.get("population", "card"), acct_state)
    tables["card"] = cards

    merchants, merch_state = _merchants(cfg, rng.get("population", "merchant"), n_ind)
    tables["merchant"] = merchants

    # --- relationships ---
    employer = _assign_employers(rng.get("population", "employment"), ind_state, ent_state)

    tables["individual_resides_at"] = _edges(
        ind_state["id"], addresses["address_id"].to_numpy()[household_of_individual], ind_state
    )
    tables["entity_registered_at"] = _edges(
        ent_state["id"], addresses["address_id"].to_numpy()[entity_address], ent_state
    )
    tables["individual_has_phone"] = _edges(
        ind_state["id"], phones["phone_id"].to_numpy(), ind_state
    )

    tables["individual_owns_account"] = _ownership_edges(acct_state, ind_state, is_entity=False)
    tables["entity_owns_account"] = _ownership_edges(acct_state, ent_state, is_entity=True)

    tables["employed_by"] = _employment_edges(
        rng.get("population", "employment_detail"), ind_state, ent_state, employer
    )
    tables["individual_bo_of_entity"], tables["entity_bo_of_entity"], tables["director_of"] = (
        _ownership_structures(rng.get("population", "ownership"), ind_state, ent_state)
    )
    tables["associate_of"] = _household_associates(household_of_individual, ind_state["id"])

    return Population(
        tables=tables,
        account_owner=acct_state["owner"],
        account_owner_is_entity=acct_state["owner_is_entity"],
        account_archetype=acct_state["archetype"],
        account_ids=acct_state["id"],
        account_type=acct_state["type"],
        account_opened=acct_state["opened"],
        individual_income=ind_state["income"],
        entity_revenue=ent_state["revenue"],
        entity_cash_ratio=ent_state["cash_ratio"],
        individual_primary_account=acct_state["primary_for_individual"],
        entity_primary_account=acct_state["primary_for_entity"],
        card_account=card_state["account_idx"],
        card_ids=card_state["id"],
        device_ids=devices["device_id"].to_numpy(),
        ip_ids=tables["ip_address"]["ip_id"].to_numpy(),
        individual_device=ind_device,
        employer=employer,
        merchant_ids=merch_state["id"],
        merchant_mcc_idx=merch_state["mcc_idx"],
        merchant_weight=merch_state["weight"],
    )


# ---------------------------------------------------------------------------
# Static reference tables
# ---------------------------------------------------------------------------


def _jurisdictions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "code": [j.code for j in JURISDICTIONS],
            "name": [j.name for j in JURISDICTIONS],
            "risk_rating": [j.risk_rating for j in JURISDICTIONS],
            "is_secrecy_haven": [j.is_secrecy_haven for j in JURISDICTIONS],
        }
    )


def _institution(cfg: RunConfig, rng: np.random.Generator) -> tuple[pl.DataFrame, str]:
    inst = cfg.institution["institution"]
    institution_id = "INST-000000001"
    return (
        pl.DataFrame(
            {
                "institution_id": [institution_id],
                "name": [inst["name"]],
                "country": [inst["country"]],
                "rssd_id": [ids.rssd(rng)],
                "control_policy": [inst["control_policy"]],
            }
        ),
        institution_id,
    )


# ---------------------------------------------------------------------------
# Addresses and households
# ---------------------------------------------------------------------------


def _addresses(
    cfg: RunConfig, rng: np.random.Generator, n_ind: int, n_ent: int
) -> tuple[pl.DataFrame, np.ndarray, np.ndarray]:
    """Build addresses, assigning individuals to households and entities to sites.

    Households are the reason two unrelated-looking customers share an address
    legitimately. Serviced offices are the same thing for companies, and they
    are the direct twin of the shell-network "many entities, one address"
    signal — so the background needs plenty of both.
    """
    # Household sizes 1-4, skewed small, matching US household composition
    # closely enough that shared-address counts look right.
    sizes = rng.choice([1, 2, 3, 4], n_ind, p=[0.28, 0.35, 0.22, 0.15])
    household_of_individual = np.repeat(np.arange(len(sizes)), np.minimum(sizes, n_ind))[:n_ind]
    n_households = int(household_of_individual[-1]) + 1 if n_ind else 0

    # Most companies have their own registered address; a minority sit in
    # serviced offices shared by many unrelated companies.
    serviced_office_count = max(1, n_ent // 40)
    own_site = rng.random(n_ent) > 0.18
    n_entity_sites = int(own_site.sum())

    n_addr = n_households + n_entity_sites + serviced_office_count
    house = ids.house_numbers(rng, n_addr)
    street = np.char.add(
        np.char.add(house.astype("U7"), " "),
        np.char.add(
            np.char.add(rng.choice(_STREETS, n_addr), " "), rng.choice(_STREET_TYPES, n_addr)
        ),
    )
    city_idx = rng.integers(0, len(_CITIES), n_addr)
    cities = np.array([c[0] for c in _CITIES])
    states = np.array([c[1] for c in _CITIES])
    zips = np.array([c[2] for c in _CITIES])

    # A commercial mail-receiving agency is a legitimate service that also
    # shows up in fraud. Flagged so detection can use it as a weak signal.
    is_cmra = rng.random(n_addr) < 0.03
    is_cmra[n_households + n_entity_sites :] = True  # serviced offices

    addresses = pl.DataFrame(
        {
            "address_id": _pad("ADDR", n_addr),
            "street": street,
            "city": cities[city_idx],
            "state": states[city_idx],
            "postcode": zips[city_idx],
            "country": np.full(n_addr, DOMESTIC),
            "is_cmra": is_cmra,
        }
    )

    entity_address = np.empty(n_ent, dtype=np.int64)
    entity_address[own_site] = n_households + np.arange(n_entity_sites)
    shared = ~own_site
    entity_address[shared] = (
        n_households + n_entity_sites + rng.integers(0, serviced_office_count, int(shared.sum()))
    )
    return addresses, household_of_individual, entity_address


# ---------------------------------------------------------------------------
# Individuals
# ---------------------------------------------------------------------------


def _individuals(
    cfg: RunConfig, rng: np.random.Generator, n: int, household: np.ndarray
) -> tuple[pl.DataFrame, dict]:
    arch_idx = rng.choice(len(RETAIL_ARCHETYPES), n, p=_weights(RETAIL_ARCHETYPES))
    arch = [RETAIL_ARCHETYPES[i] for i in range(len(RETAIL_ARCHETYPES))]

    mu = np.array([a.income_mu for a in arch])[arch_idx]
    sigma = np.array([a.income_sigma for a in arch])[arch_idx]
    income = np.round(rng.lognormal(mu, sigma), 2)

    # Family members share a surname. Without this, a household reads as a set
    # of unrelated people at one address - which is exactly a mule-ring shape.
    _, first_in_household = np.unique(household, return_index=True)
    household_surnames = nm.surnames(rng, len(first_in_household))
    given = nm.given_names(rng, n)
    surname = household_surnames[household]
    # A minority of adults at an address are unrelated housemates.
    unrelated = rng.random(n) < 0.2
    surname = np.where(unrelated, nm.surnames(rng, n), surname)
    full_name = np.char.add(np.char.add(given, " "), surname)

    ages = np.clip(rng.normal(44, 16, n), 18, 92)
    # Students and retirees are drawn from their own age bands rather than the
    # general curve, or the archetype stops being coherent.
    student = arch_idx == [a.name for a in arch].index("student")
    retired = arch_idx == [a.name for a in arch].index("retired")
    ages = np.where(student, rng.uniform(18, 26, n), ages)
    ages = np.where(retired, rng.uniform(64, 92, n), ages)
    dob = np.datetime64(cfg.window.start, "D") - (ages * 365.25).astype("timedelta64[D]")

    # Drawn per archetype in bulk rather than per individual: a Python-level
    # rng.choice per row is ~90K calls at the mvp preset, which dominates
    # population generation for no benefit.
    occupations = np.full(n, "Unspecified", dtype=object)
    for i, a in enumerate(arch):
        if not a.occupations:
            continue
        mask = arch_idx == i
        count = int(mask.sum())
        if count:
            occupations[mask] = rng.choice(np.array(a.occupations), count)
    occupations = occupations.astype(str)

    # Onboarding predates the window for most customers; recent joiners matter
    # because account age is a mule signal (M3), so the tail is deliberate.
    onboard_span_days = 3650
    onboarded = np.datetime64(cfg.window.start, "D") - rng.integers(0, onboard_span_days, n).astype(
        "timedelta64[D]"
    )

    # KYC tier follows income and PEP status, the way a real CDD model would.
    is_pep = rng.random(n) < 0.004
    kyc = np.ones(n, dtype=np.int64)
    kyc[income > 60_000] = 2
    kyc[(income > 400_000) | is_pep] = 3

    df = pl.DataFrame(
        {
            "individual_id": _pad("IND", n),
            "full_name": full_name,
            "date_of_birth": dob,
            "tax_id": ids.ssns(rng, n),
            "occupation": occupations,
            "naics_employer": np.full(n, ""),
            "income_band": np.select(
                [income < 30_000, income < 75_000, income < 150_000, income < 400_000],
                ["<30k", "30-75k", "75-150k", "150-400k"],
                ">400k",
            ),
            "annual_income": income,
            "kyc_tier": kyc,
            "onboarded_on": onboarded,
            "is_pep": is_pep,
            "residency_country": np.full(n, DOMESTIC),
            "behavior_archetype": np.array([a.name for a in arch])[arch_idx],
        }
    )
    state = {
        "id": df["individual_id"].to_numpy(),
        "archetype": arch_idx,
        "income": income,
        "onboarded": onboarded,
        "kyc": kyc,
    }
    return df, state


def _phones(rng: np.random.Generator, n: int) -> tuple[pl.DataFrame, np.ndarray]:
    df = pl.DataFrame(
        {
            "phone_id": _pad("PHN", n),
            "e164": ids.phones(rng, n),
            "line_type": rng.choice(
                np.array(["mobile", "landline", "voip"]), n, p=[0.82, 0.12, 0.06]
            ),
        }
    )
    return df, np.arange(n)


def _devices(
    cfg: RunConfig, rng: np.random.Generator, n_ind: int, household: np.ndarray
) -> tuple[pl.DataFrame, np.ndarray]:
    """One device per individual, with household members sometimes sharing one.

    Device sharing inside a household is the legitimate twin of the mule-ring
    shared-device signal. Around a fifth of individuals share rather than own,
    which puts enough shared-device pairs in the background that the signal
    has to be about *who* shares, not *that* they share.
    """
    shares_household_device = rng.random(n_ind) < 0.22
    device_of_individual = np.arange(n_ind)
    # Point sharers at the first member of their household.
    _, first_idx = np.unique(household, return_index=True)
    device_of_individual = np.where(
        shares_household_device, first_idx[household], device_of_individual
    )
    owned = np.unique(device_of_individual)
    remap = -np.ones(n_ind, dtype=np.int64)
    remap[owned] = np.arange(len(owned))
    device_of_individual = remap[device_of_individual]

    n_dev = len(owned)
    df = pl.DataFrame(
        {
            "device_id": _pad("DEV", n_dev),
            "fingerprint": np.array(
                [f"fp_{v:016x}" for v in rng.integers(0, 2**63, n_dev, dtype=np.int64)]
            ),
            "device_type": rng.choice(_DEVICE_TYPES, n_dev),
            "os": rng.choice(_DEVICE_OS, n_dev),
            "first_seen": _random_dates(rng, n_dev, cfg.window.start, cfg.window.end).astype(
                "datetime64[us]"
            ),
            "is_emulator": rng.random(n_dev) < 0.005,
        }
    )
    return df, device_of_individual


def _ips(rng: np.random.Generator, n_ind: int) -> pl.DataFrame:
    # Fewer IPs than individuals: home broadband is stable and shared within a
    # household, mobile sessions come and go. 0.7 per individual puts realistic
    # reuse in the background without the artificial concentration a small
    # reserved range would force (identifiers.py).
    n_ip = max(16, int(n_ind * 0.7))
    return pl.DataFrame(
        {
            "ip_id": _pad("IP", n_ip),
            "address": ids.ipv4(rng, n_ip),
            "asn": rng.choice(np.array([7922, 7018, 20115, 22773, 6389, 21928]), n_ip),
            "country": np.full(n_ip, DOMESTIC),
            "is_proxy": rng.random(n_ip) < 0.02,
        }
    )


# ---------------------------------------------------------------------------
# Legal entities
# ---------------------------------------------------------------------------


def _entities(cfg: RunConfig, rng: np.random.Generator, n: int) -> tuple[pl.DataFrame, dict]:
    ind_idx = rng.choice(len(INDUSTRIES), n, p=_weights(INDUSTRIES))
    industries = list(INDUSTRIES)
    arch_idx = rng.choice(len(BUSINESS_ARCHETYPES), n, p=_weights(BUSINESS_ARCHETYPES))

    # A holding company is an "Offices of Other Holding Companies" business by
    # definition; letting the two be drawn independently would produce holding
    # companies classified as restaurants.
    holding = arch_idx == [a.name for a in BUSINESS_ARCHETYPES].index("holding_company")
    ind_idx = np.where(holding, [i.naics for i in industries].index("551112"), ind_idx)

    entity_type = rng.choice(
        np.array(["llc", "corp", "partnership", "trust", "sole_prop"]),
        n,
        p=[0.46, 0.28, 0.09, 0.05, 0.12],
    )
    entity_type = np.where(holding, "corp", entity_type)

    median_rev = np.array([i.median_revenue for i in industries])[ind_idx]
    revenue = np.round(median_rev * rng.lognormal(0.0, 0.85, n), 2)
    median_emp = np.array([i.median_employees for i in industries])[ind_idx]
    employees = np.maximum(1, rng.poisson(median_emp)).astype(np.int64)

    cash_ratio = (
        np.array([i.cash_ratio for i in industries])[ind_idx]
        * np.array([a.cash_multiplier for a in BUSINESS_ARCHETYPES])[arch_idx]
    )

    incorporated = np.datetime64(cfg.window.start, "D") - rng.integers(180, 9000, n).astype(
        "timedelta64[D]"
    )
    onboarded = incorporated + rng.integers(0, 400, n).astype("timedelta64[D]")
    onboarded = np.minimum(onboarded, np.datetime64(cfg.window.start, "D"))

    kyc = np.full(n, 2, dtype=np.int64)
    kyc[revenue > 10_000_000] = 3
    kyc[cash_ratio > CASH_INTENSIVE_THRESHOLD] = 3

    df = pl.DataFrame(
        {
            "entity_id": _pad("LE", n),
            "legal_name": nm.company_names(rng, entity_type),
            "registration_number": ids.registrations(rng, n),
            "tax_id": ids.eins(rng, n),
            "entity_type": entity_type,
            "naics": np.array([i.naics for i in industries])[ind_idx],
            "incorporated_on": incorporated,
            "incorporation_country": np.full(n, DOMESTIC),
            "declared_annual_revenue": revenue,
            "declared_employee_count": employees,
            "kyc_tier": kyc,
            "onboarded_on": onboarded,
            "is_cash_intensive": cash_ratio > CASH_INTENSIVE_THRESHOLD,
            "behavior_archetype": np.array([a.name for a in BUSINESS_ARCHETYPES])[arch_idx],
        }
    )
    state = {
        "id": df["entity_id"].to_numpy(),
        "archetype": arch_idx,
        "revenue": revenue,
        "cash_ratio": cash_ratio,
        "employees": employees,
        "onboarded": onboarded,
        "is_holding": holding,
    }
    return df, state


# ---------------------------------------------------------------------------
# Accounts and cards
# ---------------------------------------------------------------------------


def _accounts(
    cfg: RunConfig,
    rng: np.random.Generator,
    institution_id: str,
    ind_state: dict,
    ent_state: dict,
) -> tuple[pl.DataFrame, dict]:
    n_ind = len(ind_state["id"])
    n_ent = len(ent_state["id"])

    # Every customer has a primary checking account; savings and secondary
    # business accounts follow the archetype.
    savings_rate = np.array([a.savings_rate for a in RETAIL_ARCHETYPES])[ind_state["archetype"]]
    has_savings = rng.random(n_ind) < savings_rate
    ent_second = rng.random(n_ent) < 0.35

    owners = np.concatenate(
        [
            np.arange(n_ind),
            np.arange(n_ind)[has_savings],
            np.arange(n_ent),
            np.arange(n_ent)[ent_second],
        ]
    )
    is_entity = np.concatenate(
        [
            np.zeros(n_ind, bool),
            np.zeros(int(has_savings.sum()), bool),
            np.ones(n_ent, bool),
            np.ones(int(ent_second.sum()), bool),
        ]
    )
    acct_type = np.concatenate(
        [
            np.full(n_ind, "checking"),
            np.full(int(has_savings.sum()), "savings"),
            np.full(n_ent, "checking"),
            np.full(int(ent_second.sum()), "savings"),
        ]
    )
    n = len(owners)

    # `owners` holds individual indices in one half and entity indices in the
    # other, so owner attributes have to be gathered per segment - indexing
    # either source array with the whole vector runs off the end of it.
    def _gather(ind_values: np.ndarray, ent_values: np.ndarray) -> np.ndarray:
        out = np.empty(n, dtype=ind_values.dtype)
        out[~is_entity] = ind_values[owners[~is_entity]]
        out[is_entity] = ent_values[owners[is_entity]]
        return out

    owner_onboarded = _gather(ind_state["onboarded"], ent_state["onboarded"])
    opened = owner_onboarded + rng.integers(0, 120, n).astype("timedelta64[D]")
    opened = np.minimum(opened, np.datetime64(cfg.window.end, "D"))

    income_or_rev = _gather(ind_state["income"], ent_state["revenue"])
    # Opening balance scales with income but with heavy dispersion - a
    # log-normal on a fraction of annual income, floored so no account starts
    # empty enough to make every outflow an overdraft.
    balance = np.round(np.maximum(50.0, income_or_rev * rng.lognormal(-3.2, 1.0, n) * 0.1), 2)
    balance = np.where(acct_type == "savings", balance * 3.0, balance)

    archetype = _gather(ind_state["archetype"], ent_state["archetype"])

    df = pl.DataFrame(
        {
            "account_id": _pad("ACC", n),
            "iban": ids.ibans(rng, n),
            "institution_id": np.full(n, institution_id),
            "account_type": acct_type,
            "currency": np.full(n, "USD"),
            "opened_on": opened,
            "closed_on": _null_dates(n),
            "status": np.full(n, "open"),
            "opening_balance": balance,
            "monitoring_segment": np.where(is_entity, "commercial", "retail"),
        }
    )

    primary_for_individual = -np.ones(n_ind, dtype=np.int64)
    primary_for_individual[owners[:n_ind]] = np.arange(n_ind)
    ent_start = n_ind + int(has_savings.sum())
    primary_for_entity = -np.ones(n_ent, dtype=np.int64)
    primary_for_entity[owners[ent_start : ent_start + n_ent]] = np.arange(
        ent_start, ent_start + n_ent
    )

    state = {
        "id": df["account_id"].to_numpy(),
        "owner": owners,
        "owner_is_entity": is_entity,
        "type": acct_type,
        "opened": opened,
        "archetype": archetype,
        "primary_for_individual": primary_for_individual,
        "primary_for_entity": primary_for_entity,
    }
    return df, state


def _cards(cfg: RunConfig, rng: np.random.Generator, acct: dict) -> tuple[pl.DataFrame, dict]:
    checking = np.flatnonzero((acct["type"] == "checking") & ~acct["owner_is_entity"])
    # Debit on essentially every retail checking account, credit on a subset.
    debit = checking
    credit_rate = np.array([a.credit_card_rate for a in RETAIL_ARCHETYPES])[
        acct["archetype"][checking]
    ]
    credit = checking[rng.random(len(checking)) < credit_rate]

    account_idx = np.concatenate([debit, credit])
    card_type = np.concatenate([np.full(len(debit), "debit"), np.full(len(credit), "credit")])
    n = len(account_idx)

    issued = acct["opened"][account_idx] + rng.integers(0, 40, n).astype("timedelta64[D]")
    expires = issued + np.timedelta64(365 * 4, "D")
    _, masked = ids.pans(rng, n)

    limit = np.where(card_type == "credit", np.round(rng.lognormal(8.6, 0.7, n), -2), np.nan)

    df = pl.DataFrame(
        {
            "card_id": _pad("CRD", n),
            "pan_masked": masked,
            "account_id": acct["id"][account_idx],
            "card_type": card_type,
            "network": rng.choice(np.array(["visa", "mastercard", "amex"]), n, p=[0.5, 0.42, 0.08]),
            "issued_on": issued,
            "expires_on": expires,
            "status": np.full(n, "active"),
            "credit_limit": limit,
        }
    )
    return df, {"id": df["card_id"].to_numpy(), "account_idx": account_idx}


def _merchants(cfg: RunConfig, rng: np.random.Generator, n_ind: int) -> tuple[pl.DataFrame, dict]:
    # Roughly one merchant per customer. A bank sees tens of thousands of
    # distinct merchants, most of them appearing a handful of times. Sizing the
    # pool at a quarter of the customer base instead gave every merchant
    # hundreds of transactions and no long tail at all - the bottom of the
    # distribution matters as much as the top, because a real alert queue is
    # full of low-volume merchants nobody recognizes.
    n = max(64, int(n_ind * 1.2))
    mcc_idx = rng.choice(len(MERCHANT_CATEGORIES), n, p=_weights(MERCHANT_CATEGORIES))
    cats = list(MERCHANT_CATEGORIES)

    # Zipf-Mandelbrot rather than plain Zipf: the rank offset q flattens the
    # top of the distribution. Plain 1/rank^s put 8% of all card volume through
    # a single merchant, which no real market does - even the largest US
    # retailer is a few percent - and a hub that size distorts every centrality
    # and community result computed over the merchant graph.
    zipf_s, zipf_q = 1.05, 15.0
    weight = 1.0 / np.power(np.arange(1, n + 1) + zipf_q, zipf_s)
    rng.shuffle(weight)

    cnp = rng.random(n) < np.array([c.cnp_rate for c in cats])[mcc_idx]
    df = pl.DataFrame(
        {
            "merchant_id": _pad("MER", n),
            "name": nm.merchant_names(rng, n),
            "mcc": np.array([c.mcc for c in cats])[mcc_idx],
            "country": np.where(
                rng.random(n) < 0.04,
                rng.choice(np.array([j.code for j in JURISDICTIONS if j.code != DOMESTIC]), n),
                DOMESTIC,
            ),
            "channel_mix": np.where(cnp, "cnp", "card_present"),
            "acquirer_risk_tier": rng.choice(
                np.array(["low", "standard", "elevated"]), n, p=[0.55, 0.38, 0.07]
            ),
        }
    )
    return df, {"id": df["merchant_id"].to_numpy(), "mcc_idx": mcc_idx, "weight": weight}


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def _edges(start: np.ndarray, end: np.ndarray, _state: dict) -> pl.DataFrame:
    """A plain start/end edge table with open-ended validity."""
    n = len(start)
    return pl.DataFrame(
        {
            "start_id": start,
            "end_id": end,
            "valid_from": _null_dates(n),
            "valid_to": _null_dates(n),
        }
    )


def _ownership_edges(acct: dict, owner_state: dict, *, is_entity: bool) -> pl.DataFrame:
    mask = acct["owner_is_entity"] == is_entity
    idx = np.flatnonzero(mask)
    n = len(idx)
    return pl.DataFrame(
        {
            "start_id": owner_state["id"][acct["owner"][idx]],
            "end_id": acct["id"][idx],
            "share": np.ones(n),
            "is_primary": np.ones(n, dtype=bool),
            "valid_from": acct["opened"][idx],
            "valid_to": _null_dates(n),
        }
    )


def _assign_employers(rng: np.random.Generator, ind: dict, ent: dict) -> np.ndarray:
    """Assign each salaried individual an employer, or -1 for external.

    Only a minority work for a company that also banks here; the rest receive
    payroll from outside the institution. Forcing every individual onto a
    simulated employer would give 1,000 companies 9,000 staff between them and
    make payroll fan-out wildly unrealistic.
    """
    n_ind = len(ind["id"])
    employer = -np.ones(n_ind, dtype=np.int64)
    cadence = np.array([a.payroll_cadence for a in RETAIL_ARCHETYPES])[ind["archetype"]]
    salaried = np.flatnonzero(cadence != "none")

    runs_payroll = np.array([a.runs_payroll for a in BUSINESS_ARCHETYPES])[ent["archetype"]]
    employing = np.flatnonzero(runs_payroll)
    if len(employing) == 0:
        return employer

    internal = salaried[rng.random(len(salaried)) < 0.35]
    # Weight by declared headcount so big employers get more staff.
    w = ent["employees"][employing].astype(float)
    employer[internal] = rng.choice(employing, len(internal), p=w / w.sum())
    return employer


def _employment_edges(
    rng: np.random.Generator, ind: dict, ent: dict, employer: np.ndarray
) -> pl.DataFrame:
    idx = np.flatnonzero(employer >= 0)
    n = len(idx)
    return pl.DataFrame(
        {
            "start_id": ind["id"][idx],
            "end_id": ent["id"][employer[idx]],
            "role": rng.choice(
                np.array(["Staff", "Senior Staff", "Manager", "Director"]),
                n,
                p=[0.58, 0.24, 0.13, 0.05],
            ),
            "annual_salary": np.round(ind["income"][idx], 2),
            "valid_from": _null_dates(n),
            "valid_to": _null_dates(n),
        }
    )


def _ownership_structures(
    rng: np.random.Generator, ind: dict, ent: dict
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Beneficial ownership and directorships.

    Most companies are owned directly by one or two individuals. A minority sit
    under a holding company — a legitimate multi-layer ownership chain, and the
    structural twin of the shell-layering typology, which is why the background
    must contain real ones.
    """
    n_ent = len(ent["id"])
    n_ind = len(ind["id"])

    # Entity-to-entity: subsidiaries of holding companies.
    holdings = np.flatnonzero(ent["is_holding"])
    ent_ent_child: list[int] = []
    ent_ent_parent: list[int] = []
    if len(holdings):
        candidates = np.flatnonzero(~ent["is_holding"])
        n_sub = min(len(candidates), int(n_ent * 0.08))
        subs = rng.choice(candidates, n_sub, replace=False)
        ent_ent_child = list(subs)
        ent_ent_parent = list(rng.choice(holdings, n_sub))

    owned_by_entity = set(ent_ent_child)

    # Individual beneficial owners for everything not owned by a parent.
    direct = np.array([i for i in range(n_ent) if i not in owned_by_entity], dtype=np.int64)
    n_owners = rng.choice([1, 2, 3], len(direct), p=[0.62, 0.28, 0.10])
    bo_entity = np.repeat(direct, n_owners)
    bo_individual = rng.integers(0, n_ind, len(bo_entity))
    # Split percentages evenly within each company, which is the common case
    # and keeps the totals summing to 100.
    pct = np.concatenate([np.full(k, 100.0 / k) for k in n_owners])

    individual_bo = pl.DataFrame(
        {
            "start_id": ind["id"][bo_individual],
            "end_id": ent["id"][bo_entity],
            "pct": np.round(pct, 2),
            "is_disclosed": np.ones(len(bo_entity), dtype=bool),
            "valid_from": _null_dates(len(bo_entity)),
            "valid_to": _null_dates(len(bo_entity)),
        }
    )
    entity_bo = pl.DataFrame(
        {
            "start_id": ent["id"][np.array(ent_ent_parent, dtype=np.int64)]
            if ent_ent_parent
            else np.array([], dtype=object),
            "end_id": ent["id"][np.array(ent_ent_child, dtype=np.int64)]
            if ent_ent_child
            else np.array([], dtype=object),
            "pct": np.full(len(ent_ent_child), 100.0),
            "is_disclosed": np.ones(len(ent_ent_child), dtype=bool),
            "valid_from": _null_dates(len(ent_ent_child)),
            "valid_to": _null_dates(len(ent_ent_child)),
        }
    )

    # Directors: the beneficial owners, plus an occasional outside director.
    director = pl.DataFrame(
        {
            "start_id": individual_bo["start_id"],
            "end_id": individual_bo["end_id"],
            "role": np.where(rng.random(len(bo_entity)) < 0.3, "Managing Director", "Director"),
            "valid_from": _null_dates(len(bo_entity)),
            "valid_to": _null_dates(len(bo_entity)),
        }
    )
    return individual_bo, entity_bo, director


def _household_associates(household: np.ndarray, ind_ids: np.ndarray) -> pl.DataFrame:
    """Link household members to each other.

    Gives the graph an explicit "these people are connected for an innocent
    reason" edge. Without it, every shared-address pair looks like an
    undeclared association.
    """
    order = np.argsort(household, kind="stable")
    sorted_house = household[order]
    boundaries = np.flatnonzero(np.diff(sorted_house)) + 1
    groups = np.split(order, boundaries)

    starts: list[int] = []
    ends: list[int] = []
    for g in groups:
        if len(g) < 2:
            continue
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                starts.append(g[i])
                ends.append(g[j])
    n = len(starts)
    return pl.DataFrame(
        {
            "start_id": ind_ids[np.array(starts, dtype=np.int64)]
            if n
            else np.array([], dtype=object),
            "end_id": ind_ids[np.array(ends, dtype=np.int64)] if n else np.array([], dtype=object),
            "relation": np.full(n, "household"),
            "valid_from": _null_dates(n),
            "valid_to": _null_dates(n),
        }
    )
