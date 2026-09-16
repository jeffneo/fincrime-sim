"""Synthetic name composition.

Names are built from phonetic fragments rather than sampled from name
frequency lists. A frequency list is not personal data, but composition gives a
stronger and much simpler guarantee for a dataset that will be distributed: the
generator has no corpus of real names to leak, so "no attribute is conditioned
on any real record" is true of the code, not just of the output.

The fragments are chosen to read as plausible English-language names. They will
occasionally collide with a real person's name by coincidence - with ~10^5
reachable surnames that is unavoidable and harmless, because every other
attribute of the row is drawn independently and describes nobody.
"""

from __future__ import annotations

import numpy as np

# Onset/nucleus/coda fragments. Kept deliberately small and phonotactically
# safe so concatenation always produces something pronounceable.
_GIVEN_HEAD = np.array(
    [
        "Al",
        "Ar",
        "Ben",
        "Bri",
        "Cal",
        "Car",
        "Dan",
        "Dar",
        "El",
        "Em",
        "Fen",
        "Gar",
        "Hal",
        "Har",
        "Ib",
        "Jen",
        "Jor",
        "Kal",
        "Kar",
        "Len",
        "Mar",
        "Mel",
        "Nor",
        "Ol",
        "Per",
        "Quin",
        "Ran",
        "Rhe",
        "Sam",
        "Sar",
        "Tal",
        "Tor",
        "Val",
        "Ver",
        "Wen",
        "Yar",
        "Zan",
        "Ade",
        "Bry",
        "Cor",
        "Dev",
        "Eri",
        "Fel",
        "Gre",
        "Ive",
        "Jul",
        "Kev",
        "Lor",
        "Mir",
        "Nev",
        "Oli",
        "Pri",
        "Rae",
        "Sol",
        "Tess",
        "Uma",
        "Vin",
    ]
)
_GIVEN_TAIL = np.array(
    [
        "a",
        "ah",
        "an",
        "ane",
        "ar",
        "as",
        "e",
        "ee",
        "el",
        "en",
        "er",
        "ia",
        "ian",
        "ic",
        "ie",
        "in",
        "is",
        "ity",
        "o",
        "on",
        "or",
        "us",
        "ya",
        "ette",
        "ina",
        "ley",
        "na",
        "ra",
        "ric",
        "son",
        "ton",
        "wen",
    ]
)

_SURNAME_HEAD = np.array(
    [
        "Ash",
        "Bar",
        "Black",
        "Bran",
        "Bren",
        "Bright",
        "Brook",
        "Burn",
        "Cald",
        "Car",
        "Chan",
        "Clay",
        "Cole",
        "Cran",
        "Dar",
        "Dun",
        "Eas",
        "Fair",
        "Fen",
        "Ford",
        "Gar",
        "Gil",
        "Glen",
        "Grant",
        "Hale",
        "Ham",
        "Harp",
        "Hay",
        "Holt",
        "Hurst",
        "Kend",
        "Lang",
        "Lark",
        "March",
        "Mead",
        "Mor",
        "Nash",
        "Nor",
        "Oak",
        "Pel",
        "Quar",
        "Rad",
        "Rams",
        "Ridge",
        "Rook",
        "Sel",
        "Shel",
        "Stan",
        "Stone",
        "Thorn",
        "Vance",
        "Wade",
        "Wal",
        "West",
        "Whit",
        "Wyn",
        "Yates",
    ]
)
_SURNAME_TAIL = np.array(
    [
        "berg",
        "bridge",
        "brook",
        "bury",
        "by",
        "croft",
        "dale",
        "den",
        "don",
        "field",
        "ford",
        "gate",
        "grove",
        "ham",
        "hill",
        "holm",
        "hurst",
        "land",
        "ley",
        "low",
        "mont",
        "more",
        "ridge",
        "shaw",
        "stead",
        "ston",
        "thwaite",
        "ton",
        "vale",
        "wood",
        "worth",
    ]
)

# A minority of surnames are a bare head, which keeps length varied rather than
# every name being exactly two fragments.
_BARE_SURNAME_RATE = 0.25


def given_names(rng: np.random.Generator, n: int) -> np.ndarray:
    return np.char.add(rng.choice(_GIVEN_HEAD, n), rng.choice(_GIVEN_TAIL, n))


def surnames(rng: np.random.Generator, n: int) -> np.ndarray:
    head = rng.choice(_SURNAME_HEAD, n)
    tail = rng.choice(_SURNAME_TAIL, n)
    bare = rng.random(n) < _BARE_SURNAME_RATE
    return np.where(bare, head, np.char.add(head, tail))


def full_names(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(full_name, surname)``.

    The surname is returned separately so household and family generation can
    share it across related individuals — shared surnames at a shared address
    are what make a household look like a household rather than like a ring.
    """
    given = given_names(rng, n)
    sur = surnames(rng, n)
    return np.char.add(np.char.add(given, " "), sur), sur


# ---------------------------------------------------------------------------
# Company names
# ---------------------------------------------------------------------------

_COMPANY_QUALIFIER = np.array(
    [
        "Apex",
        "Atlas",
        "Beacon",
        "Bluewater",
        "Cardinal",
        "Cascade",
        "Cedar",
        "Clearpoint",
        "Copper",
        "Cornerstone",
        "Crestline",
        "Delta",
        "Eastgate",
        "Elm",
        "Ember",
        "Falcon",
        "Foundry",
        "Granite",
        "Harbor",
        "Heron",
        "Highland",
        "Ironwood",
        "Junction",
        "Keystone",
        "Lantern",
        "Laurel",
        "Lighthouse",
        "Meridian",
        "Northfield",
        "Oakline",
        "Orchard",
        "Pinnacle",
        "Quarry",
        "Redstone",
        "Ridgeway",
        "Riverbend",
        "Sable",
        "Sentinel",
        "Silverline",
        "Stonebridge",
        "Summit",
        "Tidewater",
        "Timber",
        "Trellis",
        "Vanguard",
        "Verdant",
        "Waypoint",
        "Westbrook",
        "Willow",
        "Windrow",
    ]
)
_COMPANY_NOUN = np.array(
    [
        "Advisors",
        "Associates",
        "Capital",
        "Consulting",
        "Distribution",
        "Enterprises",
        "Equipment",
        "Exports",
        "Group",
        "Holdings",
        "Imports",
        "Industries",
        "Logistics",
        "Management",
        "Manufacturing",
        "Partners",
        "Properties",
        "Services",
        "Solutions",
        "Supply",
        "Systems",
        "Trading",
        "Ventures",
        "Works",
    ]
)

_SUFFIX_BY_TYPE: dict[str, list[str]] = {
    "llc": ["LLC"],
    "corp": ["Inc.", "Corp."],
    "partnership": ["LLP", "Partners LP"],
    "trust": ["Trust"],
    "sole_prop": [""],
}


def company_names(rng: np.random.Generator, entity_types: np.ndarray) -> np.ndarray:
    """Compose company names, with a legal suffix matching each entity type.

    The suffix has to follow the type rather than be drawn independently: a
    trust named "... LLC" is the kind of internal inconsistency that a person
    reviewing generated typology subgraphs notices immediately and that makes
    a demo look synthetic.
    """
    n = len(entity_types)
    base = np.char.add(
        np.char.add(rng.choice(_COMPANY_QUALIFIER, n), " "),
        rng.choice(_COMPANY_NOUN, n),
    )
    suffixes = np.full(n, "", dtype=object)
    for etype, options in _SUFFIX_BY_TYPE.items():
        mask = entity_types == etype
        count = int(mask.sum())
        if count:
            suffixes[mask] = rng.choice(np.array(options), count)
    return np.array(
        [f"{b} {s}".strip() for b, s in zip(base, suffixes, strict=True)], dtype=object
    ).astype(str)


_MERCHANT_NOUN = np.array(
    [
        "Market",
        "Cafe",
        "Diner",
        "Grocers",
        "Pharmacy",
        "Hardware",
        "Outfitters",
        "Bakery",
        "Garage",
        "Cleaners",
        "Books",
        "Electronics",
        "Apparel",
        "Pet Supply",
        "Garden Center",
        "Bistro",
        "Deli",
        "Salon",
        "Fitness",
        "Optical",
        "Furnishings",
        "Toys",
        "Sporting Goods",
        "Wine Merchants",
        "Florist",
    ]
)


def merchant_names(rng: np.random.Generator, n: int) -> np.ndarray:
    return np.char.add(
        np.char.add(rng.choice(_COMPANY_QUALIFIER, n), " "), rng.choice(_MERCHANT_NOUN, n)
    )
