from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

RunRef = tuple[str, str, str]  # (run_id, root, backend) — attach_channel/create_channel inputs
Attrs = Mapping[str, str]  # per-row relational metadata — grouping + labeling ONLY (no fold input)
Resolver = Callable[
    [float], list[tuple[RunRef, Attrs]]
]  # Time -> [(ref, attrs)] (re-resolved each frame)


def const_resolver(ref: RunRef) -> Resolver:
    """The singleton resolver: always exactly `[(ref, {})]`. The single-run view is the
    table taken over this (spec §1: single-run = table at |I|=1)."""
    return lambda now: [(ref, {})]


def ref_from_path(path: str) -> RunRef:
    """A sqlite run log lives at ``<root>/<run_id>.db``; split a path into its RunRef."""
    p = Path(path)
    return (p.stem, str(p.parent), "sqlite")


def explicit_resolver(refs: list[RunRef]) -> Resolver:
    """A fixed IndexSet — the safe multi-run resolver: the refs it yields are opened
    via `attach_channel`, which never creates, so resolving a stale/foreign pointer
    can't fabricate or mutate a run. Exact duplicate refs are dropped (order preserved)
    so each run is one pooled channel and one DataTable row. Attribute-less: every ref is
    paired with empty attrs, so grouping/labeling fall back to the disambiguated stem."""
    snapshot = list(dict.fromkeys(refs))

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        return [(r, {}) for r in snapshot]

    return resolve


def glob_resolver(root: str) -> Resolver:
    """A LIVE resolver over a directory: each frame, discover every ``*.db`` run under
    `root` (recursively) and return their (RunRef, {}) pairs. Uses ``Path.rglob`` -- which does NOT
    recurse into symlinked directories -- so a cyclic symlink can neither hang nor explode
    the scan (verified 2026-07-21). Matches open via ``attach_channel`` (never create), so
    a stale / foreign / half-written ``.db`` reads ``missing`` / ``unreadable`` and is left
    byte-identical -- the fold classifies it, the resolver does not pre-filter. Order is
    irrelevant: the table sorts on the (disambiguated) run column."""
    root_path = Path(root)

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        # No dedup: rglob yields each path once and ref_from_path is injective over distinct
        # paths (a symlinked file and its target still have distinct paths). Unlike
        # explicit_resolver, whose CLI args can legitimately repeat, glob has no dup source.
        # Attribute-less: paired with empty attrs (the label comes from `disambiguate`).
        return [(ref_from_path(str(p)), {}) for p in root_path.rglob("*.db")]

    return resolve


def manifest_resolver(path: str) -> Resolver:
    """A LIVE resolver over a neutral JSON manifest -- the discovery INDEX (the per-run channels are
    the contents). Each frame it re-reads `path` and returns one (RunRef, attrs) per entry
    (`{"run_id", "root", "backend", "attrs": {...}}`; `attrs` optional -> {}). It deliberately does
    NOT tolerate a bad read: a malformed manifest RAISES, crashing the cockpit via the owner
    thread's catastrophic path -- correct because the atomic-write contract (spec §2) guarantees a
    read never sees a torn file, so a parse failure is a real emitter bug, not a mid-rewrite race
    (spec §6). keep-last-good robustness is deferred until a producer we cannot fix on the spot."""
    manifest_path = Path(path)

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        entries = json.loads(manifest_path.read_text())
        if not isinstance(entries, list):
            raise TypeError(f"manifest must be a JSON array, got {type(entries).__name__}")
        out: list[tuple[RunRef, Attrs]] = []
        for e in entries:
            attrs = dict(e.get("attrs", {}))
            if not all(isinstance(v, str) for v in attrs.values()):
                raise TypeError(f"manifest attrs must be str->str, got {attrs!r}")
            out.append(((e["run_id"], e["root"], e["backend"]), attrs))
        return out

    return resolve


def disambiguate(refs: Sequence[RunRef]) -> dict[str, str]:
    """Map each ref (by ``ref_key``) to the SHORTEST trailing path suffix that is unique
    across `refs`. Start every run at its bare stem; any group that still collides grows
    one more parent level; repeat until no group collides. Ragged-minimal -- a lone
    collision never lengthens the labels of already-unique runs. A NO-OP when every stem
    is unique (each label is the bare stem), so applying it globally never changes a table
    whose stems don't collide. Termination is guaranteed by the depth cap: `grew` flips
    only when some depth strictly increases, and each depth is bounded by its own path
    length, so the loop runs at most sum(len(parts)) rounds. Two refs sharing an identical
    path (same root+run_id, differing only in backend) share a part-tuple and thus a label
    -- harmless (rows stay distinct by ``ref_key``), and unreachable from the all-sqlite
    ``ref_from_path`` wiring."""
    parts: dict[str, tuple[str, ...]] = {ref_key(r): Path(r[1], r[0]).parts for r in refs}
    depth: dict[str, int] = {k: 1 for k in parts}

    def label(k: str) -> str:
        # str(Path(*suffix)) renders the trailing components cleanly -- notably it collapses
        # the absolute-anchor case ('/', 'r', 'trial') to "/r/trial" rather than the
        # "//r/trial" a bare "/".join yields. Grouping and final render share this function,
        # so uniqueness is judged on exactly what is displayed.
        return str(Path(*parts[k][-depth[k] :]))

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
