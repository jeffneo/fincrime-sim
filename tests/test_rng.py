"""Random-stream determinism and independence.

Stream independence (PHASE1-PLAN.md D8) is the property that lets typologies be
added without perturbing the background population. If these tests fail, no two
dataset versions are comparable and every calibration number from M5 onward is
measuring a moving target.
"""

from __future__ import annotations

import numpy as np
import pytest

from fincrime.rng import streams

SEED = 20260915


def test_same_path_same_seed_is_reproducible():
    a = streams(SEED).fresh("population", "individual")
    b = streams(SEED).fresh("population", "individual")
    assert np.array_equal(a.random(100), b.random(100))


def test_different_paths_diverge():
    reg = streams(SEED)
    a = reg.fresh("population", "individual")
    b = reg.fresh("population", "legal_entity")
    assert not np.array_equal(a.random(100), b.random(100))


def test_different_seeds_diverge():
    a = streams(SEED).fresh("population", "individual")
    b = streams(SEED + 1).fresh("population", "individual")
    assert not np.array_equal(a.random(100), b.random(100))


def test_get_is_cached_and_keeps_advancing():
    """Repeated get() returns one advancing stream, not a restarted one."""
    reg = streams(SEED)
    first = reg.get("behavior", "retail").random(10)
    second = reg.get("behavior", "retail").random(10)
    assert not np.array_equal(first, second)
    assert reg.get("behavior", "retail") is reg.get("behavior", "retail")


def test_fresh_restarts_the_same_sequence():
    """fresh() is what makes per-ring streams safe inside a loop."""
    reg = streams(SEED)
    assert np.array_equal(
        reg.fresh("typology", "structuring", "ring-0007").random(10),
        reg.fresh("typology", "structuring", "ring-0007").random(10),
    )


def test_stream_identity_is_independent_of_creation_order():
    """The property the whole design exists for.

    Consuming an unrelated stream first - as adding a new typology would - must
    not shift the population stream by a single draw.
    """
    baseline = streams(SEED)
    expected = baseline.get("population", "individual").random(50)

    perturbed = streams(SEED)
    # Simulate a later milestone adding stages ahead of the population stage.
    perturbed.get("typology", "cnp_fraud").random(9_999)
    perturbed.get("hard_negatives", "treasury_hub").random(1_234)
    actual = perturbed.get("population", "individual").random(50)

    assert np.array_equal(expected, actual)


def test_path_is_not_flattened():
    """('a','bc') and ('ab','c') must be different streams.

    Hashing a joined string rather than per-segment would collide these, which
    would silently correlate two unrelated generators.
    """
    reg = streams(SEED)
    assert not np.array_equal(
        reg.fresh("a", "bc").random(50),
        reg.fresh("ab", "c").random(50),
    )


def test_child_seed_is_deterministic_and_in_range():
    reg = streams(SEED)
    seed = reg.child_seed("validate", "lightgbm")
    assert seed == streams(SEED).child_seed("validate", "lightgbm")
    assert 0 <= seed < 2**64
    assert seed != reg.child_seed("validate", "sklearn")


@pytest.mark.parametrize("bad", [(), ("",), ("population", "")])
def test_invalid_paths_are_rejected(bad):
    with pytest.raises(ValueError):
        streams(SEED).get(*bad)


def test_negative_seed_is_rejected():
    with pytest.raises(ValueError):
        streams(-1)
