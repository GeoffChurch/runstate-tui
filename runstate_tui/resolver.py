from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

RunRef = tuple[str, str, str]  # (run_id, root, backend) — attach_channel/create_channel inputs
Resolver = Callable[[float], list[RunRef]]  # Time -> IndexSet (re-resolved each frame)


def const_resolver(ref: RunRef) -> Resolver:
    """The singleton resolver: always exactly `[ref]`. The single-run view is the
    table taken over this (spec §1: single-run = table at |I|=1)."""
    return lambda now: [ref]


def ref_from_path(path: str) -> RunRef:
    """A sqlite run log lives at ``<root>/<run_id>.db``; split a path into its RunRef."""
    p = Path(path)
    return (p.stem, str(p.parent), "sqlite")


def explicit_resolver(refs: list[RunRef]) -> Resolver:
    """A fixed IndexSet — the safe multi-run resolver: the refs it yields are opened
    via `attach_channel`, which never creates, so resolving a stale/foreign pointer
    can't fabricate or mutate a run. Exact duplicate refs are dropped (order preserved)
    so each run is one pooled channel and one DataTable row."""
    snapshot = list(dict.fromkeys(refs))

    def resolve(_now: float) -> list[RunRef]:
        return list(snapshot)

    return resolve


def ref_key(ref: RunRef) -> str:
    """A stable, collision-proof string key for a RunRef (run_id alone collides:
    a/run1.db and b/run1.db both have run_id 'run1'). NUL can't appear in a path,
    so it is a safe join separator."""
    return "\x00".join(ref)
