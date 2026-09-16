"""Synthetic identifier generation.

Every identifier is drawn from a range that can never be validly issued, so no
generated value can collide with a real-world identifier (spec §9.4). Each
generator has a matching ``is_*`` predicate that the privacy audit runs over
the finished dataset — the guarantee is verified against the data, not just
asserted in a docstring.

Two ranges are chosen against the obvious option, for the same reason: a pool
too small for the population forces unrelated customers to share values, and
shared identifiers are precisely what mule and synthetic-identity detection
keys on. A privacy control must not manufacture a crime signal.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Tax identifiers
# ---------------------------------------------------------------------------

#: The SSA has never issued, and has stated it will never issue, an SSN with an
#: area number above 899.
SSN_AREA_MIN, SSN_AREA_MAX = 900, 999


def ssns(rng: np.random.Generator, n: int) -> np.ndarray:
    area = rng.integers(SSN_AREA_MIN, SSN_AREA_MAX + 1, n)
    group = rng.integers(1, 100, n)
    serial = rng.integers(1, 10_000, n)
    return np.char.add(
        np.char.add(
            np.char.add(area.astype("U3"), "-"),
            np.char.add(np.char.zfill(group.astype("U2"), 2), "-"),
        ),
        np.char.zfill(serial.astype("U4"), 4),
    )


def is_synthetic_ssn(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return False
    return SSN_AREA_MIN <= int(parts[0]) <= SSN_AREA_MAX


#: The IRS has never used campus prefix 00 for an EIN.
EIN_PREFIX = "00"


def eins(rng: np.random.Generator, n: int) -> np.ndarray:
    serial = rng.integers(0, 10_000_000, n)
    return np.char.add(f"{EIN_PREFIX}-", np.char.zfill(serial.astype("U7"), 7))


def is_synthetic_ein(value: str) -> bool:
    return value.startswith(f"{EIN_PREFIX}-")


# ---------------------------------------------------------------------------
# Institution and company registry
# ---------------------------------------------------------------------------

#: Federal Reserve RSSD ids in use are well below 9,000,000.
RSSD_MIN = 9_000_000


def rssd(rng: np.random.Generator) -> str:
    return str(int(rng.integers(RSSD_MIN, RSSD_MIN + 1_000_000)))


def is_synthetic_rssd(value: str) -> bool:
    return value.isdigit() and int(value) >= RSSD_MIN


#: No US state registry issues numbers in this shape.
REGISTRATION_PREFIX = "SYN"


def registrations(rng: np.random.Generator, n: int) -> np.ndarray:
    serial = rng.integers(0, 100_000_000, n)
    return np.char.add(f"{REGISTRATION_PREFIX}-", np.char.zfill(serial.astype("U8"), 8))


def is_synthetic_registration(value: str) -> bool:
    return value.startswith(f"{REGISTRATION_PREFIX}-")


# ---------------------------------------------------------------------------
# Accounts and cards
# ---------------------------------------------------------------------------

#: ISO 7064 mod-97-10 never produces check digits "00" - a valid IBAN's check
#: digits are always 02-98. Hard-coding 00 makes every generated IBAN fail
#: validation by construction rather than by luck.
IBAN_CHECK_DIGITS = "00"
IBAN_COUNTRY = "ZZ"


def ibans(rng: np.random.Generator, n: int) -> np.ndarray:
    bban = rng.integers(0, 10**16, n)
    return np.char.add(f"{IBAN_COUNTRY}{IBAN_CHECK_DIGITS}", np.char.zfill(bban.astype("U16"), 16))


def is_synthetic_iban(value: str) -> bool:
    return value.startswith(f"{IBAN_COUNTRY}{IBAN_CHECK_DIGITS}")


#: 999999 is not an issued IBAN/IIN bank identification number on any card
#: network, so a PAN on this BIN is not routable even though it passes Luhn.
#: Luhn validity is kept deliberately: downstream tooling often validates PANs,
#: and a dataset that fails those checks is annoying for no privacy gain.
TEST_BIN = "999999"


def _luhn_check_digit(partial: np.ndarray) -> np.ndarray:
    """Luhn check digit for an array of digit strings, vectorized."""
    digits = np.array([[int(c) for c in s] for s in partial], dtype=np.int64)
    # Double every second digit from the right of the final number, i.e. every
    # digit at even index counting from the left of this odd-length partial.
    weights = np.ones(digits.shape[1], dtype=np.int64)
    weights[-1::-2] = 2
    scaled = digits * weights
    scaled = np.where(scaled > 9, scaled - 9, scaled)
    return (10 - scaled.sum(axis=1) % 10) % 10


def pans(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(full_pan, masked_pan)``.

    Only the masked form is stored (``card.pan_masked``); the full number is
    returned so the generator can derive a stable card id from it without a
    second draw.
    """
    account_part = rng.integers(0, 10**9, n)
    partial = np.char.add(TEST_BIN, np.char.zfill(account_part.astype("U9"), 9))
    check = _luhn_check_digit(partial)
    full = np.char.add(partial, check.astype("U1"))
    masked = np.char.add(f"{TEST_BIN[:4]}********", np.array([s[-4:] for s in full]))
    return full, masked


def is_synthetic_pan(value: str) -> bool:
    return value.startswith(TEST_BIN[:4])


# ---------------------------------------------------------------------------
# Contact and network
# ---------------------------------------------------------------------------

#: NANP reserves 555-0100 through 555-0199 in every area code for fictitious
#: use. 100 numbers per area code, so the area code carries the variety.
PHONE_EXCHANGE = "555"
PHONE_LINE_MIN, PHONE_LINE_MAX = 100, 199

#: Geographic NANP area codes. Real codes, so the numbers look plausible; the
#: 555-01xx line range is what makes them unreachable.
_AREA_CODES = np.array(
    [212, 213, 305, 312, 404, 415, 512, 602, 617, 702, 713, 773, 804, 813, 917, 972]
)


def phones(rng: np.random.Generator, n: int) -> np.ndarray:
    area = rng.choice(_AREA_CODES, n)
    line = rng.integers(PHONE_LINE_MIN, PHONE_LINE_MAX + 1, n)
    return np.char.add(
        np.char.add("+1", area.astype("U3")),
        np.char.add(PHONE_EXCHANGE, np.char.zfill(line.astype("U4"), 4)),
    )


def is_synthetic_phone(value: str) -> bool:
    if not value.startswith("+1") or len(value) != 12:
        return False
    return value[5:8] == PHONE_EXCHANGE and PHONE_LINE_MIN <= int(value[8:]) <= PHONE_LINE_MAX


#: 240.0.0.0/4 - RFC 1112 "reserved for future use". Never allocated, never
#: routed, and 268M addresses wide.
#:
#: RFC 5737 documentation ranges (192.0.2.0/24 and friends) are the textbook
#: choice and are wrong here: 762 usable addresses across a 100K-entity
#: population would put dozens of unrelated customers behind each IP. Shared
#: infrastructure across unrelated accounts is the headline mule-network
#: signal, so that pool would bake a false positive into every entity in the
#: dataset.
RESERVED_IP_BASE = 240 << 24
RESERVED_IP_SPAN = 1 << 28

#: 100.64.0.0/10 - RFC 6598 carrier-grade NAT. Also never publicly routed, and
#: genuinely shared in the real world, so it models mobile carrier egress where
#: legitimate address sharing is expected rather than suspicious.
CGNAT_IP_BASE = (100 << 24) | (64 << 16)
CGNAT_IP_SPAN = 1 << 22


def _to_dotted(packed: np.ndarray) -> np.ndarray:
    octets = [(packed >> shift) & 0xFF for shift in (24, 16, 8, 0)]
    out = octets[0].astype("U3")
    for octet in octets[1:]:
        out = np.char.add(np.char.add(out, "."), octet.astype("U3"))
    return out


def ipv4(rng: np.random.Generator, n: int, *, cgnat_fraction: float = 0.25) -> np.ndarray:
    is_cgnat = rng.random(n) < cgnat_fraction
    packed = np.where(
        is_cgnat,
        CGNAT_IP_BASE + rng.integers(0, CGNAT_IP_SPAN, n),
        RESERVED_IP_BASE + rng.integers(0, RESERVED_IP_SPAN, n),
    )
    return _to_dotted(packed)


def is_synthetic_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    packed = (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(parts[3])
    in_reserved = RESERVED_IP_BASE <= packed < RESERVED_IP_BASE + RESERVED_IP_SPAN
    in_cgnat = CGNAT_IP_BASE <= packed < CGNAT_IP_BASE + CGNAT_IP_SPAN
    return in_reserved or in_cgnat


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

#: US street addressing does not reach six-digit house numbers outside a
#: handful of grid-numbered suburbs, and never reaches 900000. Putting the
#: guarantee in the house number lets city/state/ZIP stay real, which keeps the
#: geographic distribution - and therefore the plausibility of the population -
#: intact.
HOUSE_NUMBER_MIN = 900_000


def house_numbers(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.integers(HOUSE_NUMBER_MIN, HOUSE_NUMBER_MIN + 100_000, n)


def is_synthetic_house_number(street_line: str) -> bool:
    head = street_line.split(" ", 1)[0]
    return head.isdigit() and int(head) >= HOUSE_NUMBER_MIN


#: Maps each ``id_control`` name in schema.py to its verifier, so the privacy
#: audit can walk the schema and check every declared guarantee against the
#: generated values without a hand-maintained list.
VERIFIERS = {
    "never_issued_ssn": is_synthetic_ssn,
    "unassigned_ein": is_synthetic_ein,
    "reserved_rssd": is_synthetic_rssd,
    "reserved_registration": is_synthetic_registration,
    "invalid_checkdigit_iban": is_synthetic_iban,
    "reserved_test_bin": is_synthetic_pan,
    "reserved_555_range": is_synthetic_phone,
    "reserved_ipv4_space": is_synthetic_ip,
    "reserved_house_number": is_synthetic_house_number,
    # Names and company names are composed, not drawn from a reserved range,
    # so there is no range predicate to check. The guarantee is structural -
    # no attribute is conditioned on any real record - and is asserted by
    # tests/test_privacy.py against the generator, not the output.
    "synthetic_composition": None,
}
