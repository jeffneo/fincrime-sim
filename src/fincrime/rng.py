"""Deterministic random-stream hierarchy.

One master seed reproduces an entire dataset (PHASE1-PLAN.md D8). The property
that matters beyond plain reproducibility is **stream independence**: drawing
from one stream must never shift another. Without it, adding a typology or
changing a ring's size perturbs the background population, every downstream
distribution moves, and no two dataset versions are comparable.

That rules out the obvious approach of a single ``default_rng(seed)`` threaded
through the generators, because consumption order then couples everything to
everything. Instead each stream is addressed by a path::

    rng = streams(seed).get("population", "individual", "income")

The path is hashed into a ``SeedSequence`` spawn key, so a stream's identity
depends only on its own name - not on how many streams were created before it,
or in what order. ``get`` is idempotent: the same path always returns the same
generator instance within a ``StreamRegistry``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

#: Width of each spawn-key word. SeedSequence accepts arbitrary uint32 tuples;
#: 32-bit words are its native granularity.
_WORD_BITS = 32
_WORD_MASK = (1 << _WORD_BITS) - 1


def _path_key(path: tuple[str, ...]) -> tuple[int, ...]:
    """Hash a stream path into a spawn key.

    BLAKE2b rather than ``hash()``: Python's string hash is salted per process
    unless PYTHONHASHSEED is fixed, which would make "deterministic" depend on
    an environment variable. Two 32-bit words per segment gives a 64-bit space
    per segment - collision risk is negligible for the hundreds of streams this
    simulator uses, and a collision would be a silent correlation between two
    streams rather than a crash, so the headroom is deliberate.
    """
    key: list[int] = []
    for segment in path:
        digest = hashlib.blake2b(segment.encode("utf-8"), digest_size=8).digest()
        word = int.from_bytes(digest, "big")
        key.append((word >> _WORD_BITS) & _WORD_MASK)
        key.append(word & _WORD_MASK)
    return tuple(key)


@dataclass(slots=True)
class StreamRegistry:
    """Named, mutually independent random streams derived from one seed."""

    seed: int
    _cache: dict[tuple[str, ...], np.random.Generator] = field(default_factory=dict)

    def get(self, *path: str) -> np.random.Generator:
        """Return the generator for ``path``, creating it on first request.

        Repeated calls return the *same* generator, so a caller that draws in a
        loop keeps advancing one stream rather than restarting it. Use
        :meth:`fresh` when a reset is genuinely wanted.
        """
        if not path:
            raise ValueError("a stream path needs at least one segment")
        if any(not s for s in path):
            raise ValueError(f"empty path segment in {path!r}")
        cached = self._cache.get(path)
        if cached is None:
            cached = self.fresh(*path)
            self._cache[path] = cached
        return cached

    def fresh(self, *path: str) -> np.random.Generator:
        """Return a new generator for ``path``, at its initial state.

        Bypasses the cache, so two calls with the same path produce two
        generators that emit identical sequences. That is what makes per-ring
        streams safe to build inside a loop: ``fresh("typology", "t1", ring_id)``
        depends on the ring id alone, not on how many rings came before it.
        """
        seq = np.random.SeedSequence(entropy=self.seed, spawn_key=_path_key(path))
        return np.random.default_rng(seq)

    def child_seed(self, *path: str) -> int:
        """A 64-bit integer seed for ``path``, for libraries that want an int.

        Anything outside numpy - a Python ``random.Random``, a LightGBM seed -
        goes through here rather than being handed an arbitrary constant, so
        third-party randomness stays inside the same reproducibility guarantee.
        """
        seq = np.random.SeedSequence(entropy=self.seed, spawn_key=_path_key(path))
        return int(seq.generate_state(2, dtype=np.uint32).astype(np.uint64) @ [1, 1 << 32])


def streams(seed: int) -> StreamRegistry:
    """Build the stream registry for a master seed."""
    if not 0 <= seed < 2**63:
        raise ValueError(f"seed must be a non-negative 63-bit integer, got {seed!r}")
    return StreamRegistry(seed=seed)
