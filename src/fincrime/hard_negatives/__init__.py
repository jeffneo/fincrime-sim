"""Hard negatives: legitimate entities built to trip a typology's headline signal.

The single most important realism control in the spec (§5.4). Most of the
difficulty in real AML is separating true suspicious patterns from
superficially similar legitimate ones, and a dataset without that separation
problem measures nothing — a detector trained on it learns the shortcut, not
the signal.

Each generator here produces the *same structure* as a typology, with a
legitimate explanation available somewhere in the graph. A restaurant whose
staff bank the day's takings produces the identical star of sub-threshold cash
deposits converging on one account that a smurfing ring does; what separates
them is the employment links, the matching card settlement, and the
seasonality — all of which a detector has to actually look at.

Labeled with ``polarity='hard_negative'`` against the typology they mimic, so
scoring can ask two different questions of the same subject: did the detector
find the crime, and did it avoid the look-alike.
"""

from __future__ import annotations

from .base import HARD_NEGATIVES, inject

__all__ = ["HARD_NEGATIVES", "inject"]
