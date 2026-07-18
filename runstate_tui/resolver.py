from __future__ import annotations

from collections.abc import Callable

RunRef = tuple[str, str, str]          # (run_id, root, backend) — open_channel's three inputs
Resolver = Callable[[float], list[RunRef]]  # Time -> IndexSet (re-resolved each frame)


def const_resolver(ref: RunRef) -> Resolver:
    """The singleton resolver: always exactly `[ref]`. The single-run view is the
    table taken over this (spec §1: single-run = table at |I|=1)."""
    return lambda now: [ref]
