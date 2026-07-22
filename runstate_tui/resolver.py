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


def glob_resolver(root: str) -> Resolver:
    """A LIVE resolver over a directory: each frame, discover every ``*.db`` run under
    `root` (recursively) and return their RunRefs. Uses ``Path.rglob`` -- which does NOT
    recurse into symlinked directories -- so a cyclic symlink can neither hang nor explode
    the scan (verified 2026-07-21). Matches open via ``attach_channel`` (never create), so
    a stale / foreign / half-written ``.db`` reads ``missing`` / ``unreadable`` and is left
    byte-identical -- the fold classifies it, the resolver does not pre-filter. Order is
    irrelevant: the table sorts on the (disambiguated) run column."""
    root_path = Path(root)

    def resolve(_now: float) -> list[RunRef]:
        refs = [ref_from_path(str(p)) for p in root_path.rglob("*.db")]
        return list(dict.fromkeys(refs))  # dedup, order preserved

    return resolve


def ref_key(ref: RunRef) -> str:
    """A stable, collision-proof string key for a RunRef (run_id alone collides:
    a/run1.db and b/run1.db both have run_id 'run1'). NUL can't appear in a path,
    so it is a safe join separator."""
    return "\x00".join(ref)
