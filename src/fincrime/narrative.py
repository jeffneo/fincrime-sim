"""Per-ring case narratives — the M6 stub (PHASE1-PLAN.md §9.3).

Template-driven, not generative. Every sentence is assembled from facts the
generator already recorded, so a narrative cannot drift from the data it
describes: the dates come from the labelled transactions, the counts from the
label table, the amounts from the ring totals. A Phase 3 implementation may
replace the templates with a model; the contract that stays is that a narrative
is *derived*, and regenerating the dataset regenerates it identically.

**Narratives are ground truth.** They say what the ring is and how to see it,
which is precisely the answer key. They land in ``ground_truth/`` as
``:CaseNarrative`` nodes carrying ``:GroundTruth``, so the existing deny rule
hides them from the demo role with no new rule (D5').

Look-alikes get narratives too, and theirs are the more useful half: a hard
negative's narrative is the *exoneration*, the paragraph explaining what an
investigator should find that closes the case. A demo that shows an analyst
reaching the same conclusion the narrative states is a better demo than one
that only finds crime.
"""

from __future__ import annotations

import polars as pl

#: Bumped when a template changes wording in a way that would alter a shipped
#: narrative. Recorded per row so a release can be traced to the text that
#: produced it.
TEMPLATE_VERSION = "m6-stub-1"

#: What the ring did, per typology. Placeholders are filled from `_facts`.
_ILLICIT: dict[str, str] = {
    "structuring": (
        "{members} labelled subjects moved {amount} through {txns} transactions "
        "between {first} and {last}. {smurf} accounts made cash deposits held "
        "below the USD 10,000 currency-transaction-report threshold and "
        "forwarded the proceeds to {collector} collecting account(s). "
        "The evasion is of 31 CFR 1010.311 and, because the deposits are split "
        "across days and locations, of the same-day aggregation in 1010.313."
    ),
    "shell_layering": (
        "A payment chain of {members} labelled subjects carried {amount} "
        "through {txns} hops between {first} and {last}. Funds entered at the "
        "first corporate account and left the institution at the last, each hop "
        "presenting as an ordinary commercial payment. Retention at each step "
        "is small, so the sum arriving at the end is close to the sum that "
        "entered at the start - which is the only thing the individual "
        "statements have in common."
    ),
    "mule_network": (
        "{members} labelled subjects handled {amount} across {txns} "
        "transactions between {first} and {last}. Funds arrived from outside "
        "the institution, were held briefly, and were forwarded to "
        "{collector} collecting account(s). The members are not connected to "
        "each other by any payment: they are connected by shared "
        "infrastructure, and that is what makes this a network rather than "
        "{members} unrelated customers."
    ),
    "cnp_fraud": (
        "{members} labelled subjects and {txns} card-not-present charges "
        "totalling {amount} between {first} and {last}. "
        "{compromised_card} card(s) belonging to unrelated cardholders were "
        "charged from shared device infrastructure. The cardholders are "
        "victims rather than participants and are labelled as such."
    ),
}

#: Why the look-alike is lawful, per hard-negative generator. This is the half
#: an investigator actually needs: the evidence that closes the case.
_LOOK_ALIKE: dict[str, str] = {
    "cash_intensive_business": (
        "NOT A RING. A cash-intensive business whose staff bank the takings: "
        "{members} labelled subjects, {amount} over {txns} transactions "
        "between {first} and {last}. It presents as structuring - several "
        "people making sub-threshold cash deposits that converge on one "
        "business account - and it is not. The deposits are lawful: a "
        "restaurant's daily take genuinely is a few thousand dollars. "
        "Exonerating evidence, all of it in the graph: the depositors are "
        "linked to the business by EMPLOYED_BY, the cash arriving tracks card "
        "settlement from the same trading days, deposits follow a weekly "
        "rhythm, suppliers are paid on a cadence, and the business was trading "
        "before the window opened."
    ),
    "treasury_hub": (
        "NOT A RING. A payroll and treasury account: {members} labelled "
        "subjects, {amount} over {txns} transactions between {first} and "
        "{last}. It presents as a mule network - extreme fan-in and fan-out, "
        "almost nothing retained - and the rapid-pass-through rule cannot tell "
        "the difference. Exonerating evidence: the counterparty set is stable "
        "fortnight after fortnight, the recipients are linked to the payer by "
        "EMPLOYED_BY, the amounts track declared salaries, and the "
        "participants share no device."
    ),
    "frequent_traveler": (
        "NOT A RING. A cardholder abroad: {members} labelled subjects, "
        "{amount} over {txns} transactions between {first} and {last}. It "
        "presents as card-not-present fraud - an unseen device, a foreign IP, "
        "merchant countries that do not match the home address, and a burst of "
        "spend - and a velocity or geo-mismatch rule cannot separate the two. "
        "Exonerating evidence: the merchant preferences are the cardholder's "
        "own and continue after the burst, and there is no cross-card device "
        "reuse. A fraudster has no history with the card, drains it, and stops."
    ),
    "holding_structure": (
        "NOT A RING. A group moving money between its own companies: "
        "{members} labelled subjects, {amount} over {txns} transactions "
        "between {first} and {last}. It presents as shell-company layering - "
        "multi-layer ownership and constant intra-group transfers. "
        "Exonerating evidence: at least one subsidiary is a real operating "
        "business with employees and outside trade, and the ownership is "
        "disclosed through BENEFICIAL_OWNER_OF rather than layered to obscure "
        "it."
    ),
}

#: Appended to every narrative. States the tier and what it cost the generator
#: to produce it, because "hard" has to mean something specific (D6).
_TIER_NOTE: dict[str, str] = {
    "easy": (
        "Difficulty tier: easy - generated with the signal left in "
        "(tight timing, obvious amounts, heavy infrastructure sharing)."
    ),
    "medium": "Difficulty tier: medium - signal partly blended into normal activity.",
    "hard": (
        "Difficulty tier: hard - generated with the headline signal removed. "
        "Expect a competent baseline to miss this one; that is the point of "
        "the tier, not a defect in the injection."
    ),
}


def _money(value: float) -> str:
    return f"USD {value:,.0f}"


def build(ring: pl.DataFrame, labels: pl.DataFrame, txn_facts: pl.DataFrame) -> pl.DataFrame:
    """One narrative per ring, in ring_id order.

    ``txn_facts`` needs ``txn_id`` and ``booked_at``: the activity dates come
    from the labelled transactions rather than from the ring's
    ``injected_from``/``injected_to``, which are the whole simulation window
    for most generators and would make every narrative say the same thing.
    """
    schema = {
        "narrative_id": pl.String,
        "ring_id": pl.String,
        "summary": pl.String,
        "template_version": pl.String,
    }
    if ring.height == 0:
        return pl.DataFrame(schema=schema)

    # Activity dates per ring, from the labelled transactions.
    dates = (
        labels.filter(pl.col("subject_type") == "transaction")
        .join(txn_facts, left_on="subject_id", right_on="txn_id", how="inner")
        .group_by("ring_id")
        .agg(
            pl.col("booked_at").min().dt.date().alias("first"),
            pl.col("booked_at").max().dt.date().alias("last"),
        )
        if labels.height and txn_facts.height
        else pl.DataFrame(schema={"ring_id": pl.String, "first": pl.Date, "last": pl.Date})
    )

    # Role counts per ring, over SUBJECT labels only - a role count that
    # includes transaction labels would report 647 mules for a ring of eleven.
    role_counts = (
        labels.filter(pl.col("subject_type") != "transaction")
        .group_by("ring_id", "role")
        .agg(pl.len().alias("n"))
        if labels.height
        else pl.DataFrame(schema={"ring_id": pl.String, "role": pl.String, "n": pl.UInt32})
    )
    roles_by_ring: dict[str, dict[str, int]] = {}
    for ring_id, role, count in role_counts.iter_rows():
        roles_by_ring.setdefault(str(ring_id), {})[str(role)] = int(count)

    subjects = (
        labels.filter(pl.col("subject_type") != "transaction")
        .group_by("ring_id")
        .agg(pl.col("subject_id").n_unique().alias("subject_count"))
        if labels.height
        else pl.DataFrame(schema={"ring_id": pl.String, "subject_count": pl.UInt32})
    )

    joined = ring.join(dates, on="ring_id", how="left").join(subjects, on="ring_id", how="left")

    rows: list[dict[str, str]] = []
    for row in joined.sort("ring_id").iter_rows(named=True):
        ring_id = str(row["ring_id"])
        roles = roles_by_ring.get(ring_id, {})
        facts = {
            "members": row.get("subject_count") or row["member_count"],
            "txns": f"{row['illicit_txn_count']:,}",
            "amount": _money(row["illicit_amount_usd"]),
            "first": row["first"] or row["injected_from"],
            "last": row["last"] or row["injected_to"],
            # Role counts the templates may refer to. Defaulted so a template
            # naming a role a generator did not emit renders rather than
            # raising halfway through a release build.
            "smurf": roles.get("smurf", 0),
            "collector": roles.get("collector", 0) or roles.get("mule_collector", 0),
            "compromised_card": roles.get("compromised_card", 0),
        }

        if row["polarity"] == "hard_negative":
            # A look-alike's role IS its generator name, which is what selects
            # the exoneration text; the typology column names what it mimics.
            generator = next(iter(roles), "")
            template = _LOOK_ALIKE.get(generator)
            if template is None:
                template = (
                    "NOT A RING. A legitimate pattern that resembles "
                    "{typology}: {members} labelled subjects, {amount} over "
                    "{txns} transactions between {first} and {last}."
                )
                facts["typology"] = row["typology"]
        else:
            template = _ILLICIT[row["typology"]]

        tier_note = _TIER_NOTE.get(row["difficulty_tier"], "")
        summary = " ".join(part for part in (template.format(**facts), tier_note) if part)
        rows.append(
            {
                "narrative_id": f"CASE-{ring_id}",
                "ring_id": ring_id,
                "summary": summary,
                "template_version": TEMPLATE_VERSION,
            }
        )

    return pl.DataFrame(rows, schema=schema)
