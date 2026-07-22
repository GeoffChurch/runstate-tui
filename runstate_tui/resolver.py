from __future__ import annotations

from collections.abc import Callable, Sequence
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


def disambiguate(refs: Sequence[RunRef]) -> dict[str, str]:
    """Map each ref (by ``ref_key``) to the SHORTEST trailing path suffix that is unique
    across `refs`. Start every run at its bare stem; any group that still collides grows
    one more parent level; repeat until no group collides. Ragged-minimal -- a lone
    collision never lengthens the labels of already-unique runs. A NO-OP when every stem
    is unique (each label is the bare stem), so applying it globally never changes a table
    whose stems don't collide. Distinct refs have distinct part-tuples and `grew` only
    flips when a depth actually increases, so the loop always terminates (worst case: the
    full path)."""
    parts: dict[str, tuple[str, ...]] = {ref_key(r): Path(r[1], r[0]).parts for r in refs}
    depth: dict[str, int] = {k: 1 for k in parts}

    def label(k: str) -> str:
        return "/".join(parts[k][-depth[k] :])

    while True:
        groups: dict[str, list[str]] = {}
        for k in parts:
            groups.setdefault(label(k), []).append(k)
        grew = False
        for members in groups.values():
            if len(members) > 1:
                for k in members:
                    if depth[k] < len(parts[k]):
                        depth[k] += 1
                        grew = True
        if not grew:
            break
    return {k: label(k) for k in parts}


def ref_key(ref: RunRef) -> str:
    """A stable, collision-proof string key for a RunRef (run_id alone collides:
    a/run1.db and b/run1.db both have run_id 'run1'). NUL can't appear in a path,
    so it is a safe join separator."""
    return "\x00".join(ref)
