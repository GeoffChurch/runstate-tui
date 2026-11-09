from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from runstate import Logs, PostgresLogs, SqliteLogs
from runstate.formats import LegacyAddress, NewerFormatRoot, VersionedAddress, sqlite_address

RunRef = tuple[str, str, str]  # (run_id, root, backend): a run, and where its log is (`place_of`)
Attrs = Mapping[str, str]  # per-row relational metadata — grouping + labeling ONLY (no fold input)
Resolver = Callable[
    [float], list[tuple[RunRef, Attrs]]
]  # Time -> [(ref, attrs)] (re-resolved each frame)


def place_of(ref: RunRef) -> Logs:
    """The place a ref's log is in, built from the ref's strings: the one point where the
    cockpit reads a backend as a string (a ref comes from a path or a manifest, which hold
    strings). Opening is the place's: `place_of(ref).reader(run_id)` to observe, which never
    creates and cannot write, and `.attach(run_id)` to send a stop. A backend the cockpit
    cannot open raises `ValueError`, which a table frame reports as a loud `error` row."""
    _run_id, root, backend = ref
    if backend == "sqlite":
        return SqliteLogs(root)
    if backend == "postgres":
        return PostgresLogs(root)
    raise ValueError(f"unknown backend {backend!r} (expected 'sqlite' or 'postgres')")


def const_resolver(ref: RunRef) -> Resolver:
    """The singleton resolver: always exactly `[(ref, {})]`. The single-run view is the
    table taken over this (spec §1: single-run = table at |I|=1)."""
    return lambda now: [(ref, {})]


def ref_from_path(path: str) -> RunRef | None:
    """The RunRef for a sqlite log's path, by runstate's own
    inverse of its address scheme (`runstate.formats.sqlite_address`, log-formats.md §3),
    or None when the path is the address of no run (a sidecar, an unregistered older
    ``vX.Y.Z/`` directory). Every copy of one run (its current log, sealed older ones, a
    legacy tombstone) names the same (run_id, root), so it is one ref and one row; opening
    the ref decides which answers, else a loud `LogFormatMismatch` / `LogFormatMissing` row.

    A newer format's directory names only its root: that format's layout, and so its run
    ids, cannot be known. Its row is labelled by the file's stem, a label and never a
    claimed run id: every open under that root raises the newer-format mismatch whatever
    the id, so the row reads "upgrade runstate"."""
    p = Path(path)
    match sqlite_address(p):
        case VersionedAddress(root=root, run_id=run_id) | LegacyAddress(root=root, run_id=run_id):
            return (run_id, str(root), "sqlite")
        case NewerFormatRoot(root=root):
            return (p.stem, str(root), "sqlite")
        case None:
            return None


def explicit_resolver(refs: list[RunRef]) -> Resolver:
    """A fixed IndexSet — the safe multi-run resolver: the refs it yields are opened
    through `place_of(ref).reader`, which never creates, so resolving a stale/foreign pointer
    can't fabricate or mutate a run. Exact duplicate refs are dropped (order preserved)
    so each run is one pooled channel and one DataTable row. Attribute-less: every ref is
    paired with empty attrs, so grouping/labeling fall back to the disambiguated stem."""
    snapshot = list(dict.fromkeys(refs))

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        return [(r, {}) for r in snapshot]

    return resolve


def _db_files(root: Path) -> Iterator[Path]:
    """Every ``*.db`` file under `root`, recursively, following symlinked directories and
    entering each real directory once (keyed by device and inode). A consumer that links
    its cells to run homes (mycooc's ``<cell>/run -> runs/<shard>/<rid>/``) keeps every
    log behind a link; the once-only rule makes a cycle end where it closes, and a home
    reached by several links is one walk, so one row. A directory that vanishes or cannot
    be read mid-walk is skipped."""
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        try:
            st = os.stat(dirpath)
        except OSError:
            dirnames.clear()
            continue
        if (st.st_dev, st.st_ino) in seen:
            dirnames.clear()
            continue
        seen.add((st.st_dev, st.st_ino))
        yield from (Path(dirpath, f) for f in filenames if f.endswith(".db"))


def glob_resolver(root: str) -> Resolver:
    """A LIVE resolver over a directory: each frame, discover every ``*.db`` run log under
    `root` (recursively, through symlinked directories, `_db_files`), in any format, and
    return one (RunRef, {}) pair per run. Matches open through a reader (never
    create), so a stale / foreign / half-written ``.db`` reads ``missing`` /
    ``unreadable`` / a format row and is left byte-identical -- the fold classifies it,
    the resolver does not pre-filter. Order is irrelevant: the table sorts on the
    (disambiguated) run column."""
    root_path = Path(root)

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        # Dedup, order preserved: one run has a log per format it has been migrated
        # through (the current one, sealed older ones, a legacy tombstone), and
        # ref_from_path maps every copy to the same ref -- one run, one row. A path that
        # addresses no run (an unregistered older vX.Y.Z/ directory) is no row.
        # Attribute-less: paired with empty attrs (the label comes from `disambiguate`).
        found = (ref_from_path(str(p)) for p in _db_files(root_path))
        refs = dict.fromkeys(r for r in found if r is not None)
        return [(r, {}) for r in refs]

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
