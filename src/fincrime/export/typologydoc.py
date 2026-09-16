"""The shipped typology document, generated rather than hand-maintained.

Two sources, both already the authority for something else: each generator's
module docstring, which is where the topology and the difficulty design are
argued, and ``config/typologies.yaml``, which is where the knob values live. A
hand-written document would be a third place for the same facts and would start
drifting the first time a tier was retuned.

The prose is therefore the generators' own. If a paragraph here reads oddly,
the fix is in the generator's docstring.
"""

from __future__ import annotations

import textwrap
from typing import Any

from ..config import RunConfig
from ..hard_negatives import HARD_NEGATIVES
from ..typologies import TYPOLOGIES


def _module_doc(cls: type) -> str:
    """The generator's module docstring, de-indented, minus its title line."""
    module = __import__(cls.__module__, fromlist=["__doc__"])
    doc = textwrap.dedent(module.__doc__ or "").strip()
    lines = doc.splitlines()
    # Drop the title line and the blank after it; the heading below replaces it.
    if lines and not lines[0].startswith(("*", "-")):
        lines = lines[1:]
    # The docstrings are written for Sphinx, where ``x`` is inline code. In
    # Markdown that renders as a literal double backtick.
    return "\n".join(lines).strip().replace("``", "`")


def _title(cls: type) -> str:
    module = __import__(cls.__module__, fromlist=["__doc__"])
    first = (module.__doc__ or "").strip().splitlines()[0] if module.__doc__ else cls.__name__
    return first.rstrip(".")


def _knob_table(tiers: dict[str, dict[str, Any]]) -> list[str]:
    """Tier knobs as a table, one row per knob, one column per tier."""
    if not tiers:
        return []
    names = list(tiers)
    keys: list[str] = []
    for tier in tiers.values():
        for key in tier:
            if key not in keys:
                keys.append(key)
    out = [
        "| Knob | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for key in keys:
        cells = []
        for name in names:
            value = tiers[name].get(key)
            cells.append("—" if value is None else f"`{value}`")
        out.append(f"| `{key}` | " + " | ".join(cells) + " |")
    out.append("")
    return out


def render(cfg: RunConfig) -> str:
    """Render the typology document as Markdown."""
    tcfg = cfg.typologies
    mix = tcfg.get("mix", {})
    prevalence = tcfg.get("prevalence", {})

    out: list[str] = [
        "# Typologies",
        "",
        "Generated from the generator docstrings and `config/typologies.yaml`.",
        "Do not edit by hand.",
        "",
        "## How difficulty works",
        "",
        "A difficulty tier is a **bundle of generator parameters**, never a",
        "label applied afterwards (PHASE1-PLAN.md D6). `hard` means the ring",
        "was built with less signal in it: amounts further from the threshold,",
        "timing more dispersed, rings smaller, infrastructure shared less, more",
        "genuine activity blended into the same accounts. That is what makes",
        "curriculum evaluation meaningful - an easy ring really is easier, and",
        "the validation report measures whether that holds.",
        "",
        "## Prevalence",
        "",
        f"- Illicit entities: **{prevalence.get('illicit_entity_fraction', 0):.1%}** "
        "of individuals.",
        f"- Hard negatives: **{prevalence.get('hard_negative_entity_fraction', 0):.1%}** "
        "- deliberately more common than crime, because a real alert queue is",
        "  dominated by legitimate oddities.",
        "",
        "A typology's entity budget is its share of the illicit total, and the",
        "ring count follows from that budget divided by the mean ring size. A",
        "typology built from large rings therefore gets few of them, which",
        "matters for any per-tier statistic - see PHASE1-PLAN.md §7a.",
        "",
        "| Typology | share of illicit entities | tier mix (easy/medium/hard) |",
        "|---|---|---|",
    ]
    for name, block in mix.items():
        tiers = block.get("tiers", {})
        tier_mix = " / ".join(f"{tiers.get(t, 0):.0%}" for t in ("easy", "medium", "hard"))
        out.append(f"| `{name}` | {block.get('share', 0):.0%} | {tier_mix} |")
    out.append("")

    out += ["---", "", "## Typologies", ""]
    for cls in TYPOLOGIES:
        spec = tcfg.get("typologies", {}).get(cls.name, {})
        out += [f"### `{cls.name}` — {_title(cls)}", "", _module_doc(cls), ""]
        knobs = _knob_table(spec.get("tiers", {}))
        if knobs:
            out += ["**Difficulty knobs**", ""] + knobs

    out += [
        "---",
        "",
        "## Hard negatives",
        "",
        "Legitimate patterns built to be mistaken for a typology. Each one",
        "produces the same surface shape as the typology it mimics and carries",
        "a distinguishing signal underneath - stated in its own section below.",
        "They are labelled with `polarity = 'hard_negative'` and with the",
        "typology they mimic, and they must never be scored as positives.",
        "",
    ]
    for cls in HARD_NEGATIVES:
        block = tcfg.get("hard_negatives", {}).get(cls.name, {})
        out += [
            f"### `{cls.name}` — mimics `{block.get('mimics', '?')}`",
            "",
            f"Share of the hard-negative budget: **{block.get('share', 0):.0%}**. "
            f"Labelled entities per instance: **{cls.entities_per_instance}**.",
            "",
            _module_doc(cls),
            "",
        ]

    return "\n".join(out) + "\n"
