"""Context Session store for the unified CM API.

Manages the lifecycle of ContextSession objects, each of which holds an
append-only chain of ContextSnapshot objects produced by search_context /
recall_experience.

Design:
- One ContextSession per user question lifecycle (created by search_context).
- Each state-changing operation (recall_experience) appends a
  new ContextSnapshot; read-only ops share the current snapshot.
- SessionStore: in-process LRU + TTL dict, guarded by threading.Lock.
  A background daemon thread sweeps expired sessions every 60 s.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..runtime.anchors import AnchorSet
from ..runtime.decision_llm import DecisionOutput
from ..runtime.traversal import TraversalSubgraph

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 1800   # 30 minutes
DEFAULT_CAPACITY = 512
_SWEEP_INTERVAL = 60         # background sweep period (seconds)


# --------------------------------------------------------------------------- #
# Leaf data structures
# --------------------------------------------------------------------------- #

@dataclass
class OutcomeRecord:
    """One outcome record appended to the session."""
    sql: str
    exec_status: str
    feedback_signal: Optional[str]
    writeback_kind: str                    # which writeback_* branch was dispatched
    snapshot_id: str                       # snapshot active at record time
    trace_id: str                          # uuid for log correlation
    queued_at: float


# --------------------------------------------------------------------------- #
# ContextSnapshot
# --------------------------------------------------------------------------- #

@dataclass
class ContextSnapshot:
    """Complete state snapshot from one search_context / recall_experience call."""

    snapshot_id: str
    parent_id: Optional[str]               # None for the first snapshot
    created_at: float
    trigger: str                           # "search_context" | "recall_experience"

    # Retrieval side
    query: str                             # retrieval query used in this snapshot
    facets: list[str]                      # semantic_split output (may be empty)
    anchors: AnchorSet
    subgraph: Optional[TraversalSubgraph]
    decision: Optional[DecisionOutput]

    # Experience side
    cards_visible: list[dict[str, Any]]    # gate-visible cards
    cards_blocked: list[dict[str, Any]]    # gate-blocked cards (debug)
    top_card_gate: dict[str, Any]          # auto_accept / top_card / avoid_cards

    # Semantic index for zoom / compare (built lazily by ctx_assemble)
    entity_index: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Writeback anchors
    task_key: str = ""
    plan_key: str = ""

    # Raw expand_subgraph results for top anchors, keyed by metric_key
    expanded_subgraphs: dict[str, dict[str, Any]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# ContextSession
# --------------------------------------------------------------------------- #

@dataclass
class ContextSession:
    """Full lifecycle of a user question (search_context → … → record_outcome)."""

    session_ref: str                       # opaque handle returned to caller
    created_at: float
    expires_at: float
    scope: dict[str, Any]                  # {domain?, as_of_date?, channel?}
    original_query: str
    datasource_id: str = ""                # 数据源标识（如 oltp_primary / warehouse_odps），用于 SQL 路由 + 图谱隔离

    # Append-only snapshot chain
    snapshots: list[ContextSnapshot] = field(default_factory=list)
    # Outcome records
    outcomes: list[OutcomeRecord] = field(default_factory=list)

    @property
    def current(self) -> ContextSnapshot:
        if not self.snapshots:
            raise IndexError("ContextSession has no snapshots")
        return self.snapshots[-1]

    @property
    def snapshot_index(self) -> int:
        return len(self.snapshots) - 1

    def append_snapshot(self, snap: ContextSnapshot) -> None:
        self.snapshots.append(snap)

    def ensure_current(self, store: Optional["SessionStore"] = None) -> ContextSnapshot:
        """Return current snapshot, bootstrapping a minimal one if the session is empty."""
        if self.snapshots:
            return self.current
        snap = bootstrap_snapshot(self, store=store)
        self.append_snapshot(snap)
        if store is not None:
            store.put(self)
        return snap

    def append_outcome(self, rec: OutcomeRecord) -> None:
        self.outcomes.append(rec)

    def all_visible_card_keys(self) -> set[str]:
        """Union of cards_visible keys across all snapshots (for dedup in recall_experience)."""
        out: set[str] = set()
        for s in self.snapshots:
            for c in s.cards_visible:
                k = (c or {}).get("key")
                if k:
                    out.add(str(k))
            for c in s.cards_blocked:
                k = (c or {}).get("key")
                if k:
                    out.add(str(k))
        return out


# --------------------------------------------------------------------------- #
# SessionStore
# --------------------------------------------------------------------------- #

class SessionStore:
    """In-process LRU + TTL store for ContextSession objects.

    Thread-safe via a single RLock. Background daemon thread sweeps expired
    entries every _SWEEP_INTERVAL seconds.

    Optional SQLite persistence: when ``sqlite_path`` is provided, every
    ``put()`` writes through to a SQLite file (WAL mode) and ``_sweep_once()``
    also deletes expired rows.  On startup, non-expired sessions are loaded
    back into memory.  SQLite errors are logged but never raised — the
    in-memory dict is always authoritative.
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        sqlite_path: Optional[str] = None,
    ) -> None:
        self._capacity = max(1, capacity)
        self._default_ttl = float(ttl_seconds)
        self._store: dict[str, ContextSession] = {}
        self._lock = threading.RLock()

        # SQLite persistence (optional)
        self._sqlite_path = sqlite_path
        self._sqlite_lock = threading.Lock()
        if self._sqlite_path:
            self._init_sqlite()
            self._load_from_sqlite()

        # Start background sweep thread
        self._sweeper = threading.Thread(
            target=self._sweep_loop, daemon=True, name="ctx_session_sweeper"
        )
        self._sweeper.start()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def new_session_ref(self) -> str:
        return uuid.uuid4().hex

    def new_snapshot_id(self) -> str:
        return uuid.uuid4().hex

    def put(self, session: ContextSession) -> None:
        """Insert or replace a session, evicting LRU if at capacity."""
        with self._lock:
            if session.session_ref in self._store:
                del self._store[session.session_ref]
            # LRU eviction: remove oldest (insertion-order) if at capacity
            while len(self._store) >= self._capacity:
                oldest_key = next(iter(self._store))
                del self._store[oldest_key]
            self._store[session.session_ref] = session

        # Persist to SQLite (outside the in-memory lock)
        self._persist_to_sqlite(session)

    def get(self, session_ref: str, *, renew: bool = True) -> Optional[ContextSession]:
        """Retrieve a session by ref, optionally renewing TTL. Returns None if expired/missing."""
        with self._lock:
            session = self._store.get(session_ref)
            if session is None:
                return None
            if time.time() > session.expires_at:
                del self._store[session_ref]
                return None
            if renew:
                session.expires_at = time.time() + self._default_ttl
                # Move to end (LRU most-recently-used)
                del self._store[session_ref]
                self._store[session_ref] = session
            return session

    def renew(self, session_ref: str, ttl_seconds: Optional[float] = None) -> bool:
        """Renew TTL for a session. Returns False if not found / expired."""
        with self._lock:
            session = self._store.get(session_ref)
            if session is None or time.time() > session.expires_at:
                return False
            session.expires_at = time.time() + (ttl_seconds or self._default_ttl)
            return True

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    # ------------------------------------------------------------------ #
    # Internal — sweep
    # ------------------------------------------------------------------ #

    def _sweep_loop(self) -> None:
        while True:
            time.sleep(_SWEEP_INTERVAL)
            try:
                self._sweep_once()
            except Exception:
                pass

    def _sweep_once(self) -> None:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._store.items() if now > v.expires_at]
            for k in expired:
                del self._store[k]

        # Also clean up expired rows from SQLite
        if self._sqlite_path and expired:
            try:
                with self._sqlite_lock:
                    conn = self._sqlite_connect()
                    try:
                        conn.execute(
                            "DELETE FROM sessions WHERE expires_at <= ?", (now,)
                        )
                    finally:
                        conn.close()
            except Exception:
                log.debug("sessions.db: sweep DELETE failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Internal — SQLite
    # ------------------------------------------------------------------ #

    def _init_sqlite(self) -> None:
        """Create the SQLite file and schema if needed."""
        path = Path(self._sqlite_path)  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._sqlite_lock:
            conn = self._sqlite_connect()
            try:
                conn.executescript(_SESSIONS_SCHEMA)
            finally:
                conn.close()

    def _sqlite_connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(
            self._sqlite_path,  # type: ignore[arg-type]
            isolation_level=None,
            check_same_thread=False,
        )
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    def _load_from_sqlite(self) -> None:
        """Load non-expired sessions from SQLite into the in-memory dict.

        Called once at startup. Rows that fail deserialization are skipped.
        """
        now = time.time()
        try:
            with self._sqlite_lock:
                conn = self._sqlite_connect()
                try:
                    rows = conn.execute(
                        "SELECT payload FROM sessions WHERE expires_at > ?",
                        (now,),
                    ).fetchall()
                finally:
                    conn.close()
        except Exception:
            log.warning("sessions.db: failed to load on startup", exc_info=True)
            return

        loaded = 0
        for row in rows:
            try:
                d = json.loads(row["payload"])
                session = session_from_dict(d)
                with self._lock:
                    if session.session_ref not in self._store:
                        self._store[session.session_ref] = session
                        loaded += 1
            except Exception:
                log.debug("sessions.db: skipping corrupt row", exc_info=True)

        if loaded:
            log.info("sessions.db: loaded %d surviving sessions", loaded)

    def _persist_to_sqlite(self, session: ContextSession) -> None:
        """Write one session to SQLite. Failures are logged, not raised."""
        if not self._sqlite_path:
            return
        try:
            payload = json.dumps(session_to_dict(session), ensure_ascii=False)
            with self._sqlite_lock:
                conn = self._sqlite_connect()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO sessions "
                        "(session_ref, created_at, expires_at, datasource_id, "
                        " scope, original_query, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            session.session_ref,
                            session.created_at,
                            session.expires_at,
                            session.datasource_id or "",
                            json.dumps(session.scope, ensure_ascii=False),
                            session.original_query or "",
                            payload,
                        ),
                    )
                finally:
                    conn.close()
        except Exception:
            log.warning(
                "sessions.db: failed to persist session %s",
                session.session_ref,
                exc_info=True,
            )


# --------------------------------------------------------------------------- #
# Factory helpers
# --------------------------------------------------------------------------- #

def bootstrap_snapshot(
    session: ContextSession,
    *,
    store: Optional[SessionStore] = None,
) -> ContextSnapshot:
    """Minimal snapshot for sessions created without search_context."""
    from ..runtime.anchors import AnchorSet
    from ..graph.datasource_registry import try_resolve

    db_id = ""
    if session.datasource_id:
        ds = try_resolve(session.datasource_id)
        if ds:
            db_id = ds.primary_db_id

    return make_snapshot(
        trigger="bootstrap",
        query=session.original_query or "",
        anchors=AnchorSet(
            question=session.original_query or "",
            db_id=db_id,
        ),
        subgraph=None,
        decision=None,
        cards_visible=[],
        cards_blocked=[],
        top_card_gate={},
        store=store,
    )


def make_snapshot(
    *,
    trigger: str,
    query: str,
    anchors: AnchorSet,
    subgraph: Optional[TraversalSubgraph],
    decision: Optional[DecisionOutput],
    cards_visible: list[dict[str, Any]],
    cards_blocked: list[dict[str, Any]],
    top_card_gate: dict[str, Any],
    facets: Optional[list[str]] = None,
    parent_id: Optional[str] = None,
    task_key: str = "",
    plan_key: str = "",
    expanded_subgraphs: Optional[dict[str, dict[str, Any]]] = None,
    store: Optional[SessionStore] = None,
) -> ContextSnapshot:
    sid = store.new_snapshot_id() if store else uuid.uuid4().hex
    return ContextSnapshot(
        snapshot_id=sid,
        parent_id=parent_id,
        created_at=time.time(),
        trigger=trigger,
        query=query,
        facets=list(facets or []),
        anchors=anchors,
        subgraph=subgraph,
        decision=decision,
        cards_visible=list(cards_visible),
        cards_blocked=list(cards_blocked),
        top_card_gate=dict(top_card_gate),
        task_key=task_key,
        plan_key=plan_key,
        expanded_subgraphs=dict(expanded_subgraphs or {}),
    )


def make_session(
    *,
    session_ref: str,
    scope: dict[str, Any],
    original_query: str,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    datasource_id: str = "",
) -> ContextSession:
    now = time.time()
    return ContextSession(
        session_ref=session_ref,
        created_at=now,
        expires_at=now + ttl_seconds,
        scope=dict(scope),
        original_query=original_query,
        datasource_id=datasource_id,
    )


# --------------------------------------------------------------------------- #
# Serialization helpers (for SQLite persistence)
# --------------------------------------------------------------------------- #

def session_to_dict(session: ContextSession) -> dict:
    """Serialize a ContextSession to a JSON-compatible dict.

    Uses dataclasses.asdict() which recursively converts all nested
    dataclasses into plain dicts.
    """
    return dataclasses.asdict(session)


def session_from_dict(d: dict) -> ContextSession:
    """Reconstruct a ContextSession from a dict produced by session_to_dict().

    Manually rebuilds nested dataclass instances since asdict() strips type info.
    Uses .get() with defaults throughout for forward/backward compatibility.
    """
    snapshots = []
    for snap_d in d.get("snapshots", []):
        anchors = _rebuild_anchor_set(snap_d.get("anchors", {}))

        subgraph = None
        sg_d = snap_d.get("subgraph")
        if sg_d:
            subgraph = TraversalSubgraph(
                node_keys=sg_d.get("node_keys", []),
                nodes_by_label=sg_d.get("nodes_by_label", {}),
                edges=sg_d.get("edges", []),
                method=sg_d.get("method", "typed"),
            )

        decision = None
        dec_d = snap_d.get("decision")
        if dec_d:
            decision = DecisionOutput(
                task_type=dec_d.get("task_type", "multistep"),
                reuse_key=dec_d.get("reuse_key"),
                card_confidence=float(dec_d.get("card_confidence", 0.0)),
                card_reason=dec_d.get("card_reason", ""),
                negative_hints=dec_d.get("negative_hints", []),
                llm_calls=int(dec_d.get("llm_calls", 1)),
            )

        snapshots.append(ContextSnapshot(
            snapshot_id=snap_d.get("snapshot_id", ""),
            parent_id=snap_d.get("parent_id"),
            created_at=float(snap_d.get("created_at", 0.0)),
            trigger=snap_d.get("trigger", ""),
            query=snap_d.get("query", ""),
            facets=snap_d.get("facets", []),
            anchors=anchors,
            subgraph=subgraph,
            decision=decision,
            cards_visible=snap_d.get("cards_visible", []),
            cards_blocked=snap_d.get("cards_blocked", []),
            top_card_gate=snap_d.get("top_card_gate", {}),
            entity_index=snap_d.get("entity_index", {}),
            task_key=snap_d.get("task_key", ""),
            plan_key=snap_d.get("plan_key", ""),
            expanded_subgraphs=snap_d.get("expanded_subgraphs", {}),
        ))

    outcomes = [
        OutcomeRecord(
            sql=o.get("sql", ""),
            exec_status=o.get("exec_status", ""),
            feedback_signal=o.get("feedback_signal"),
            writeback_kind=o.get("writeback_kind", ""),
            snapshot_id=o.get("snapshot_id", ""),
            trace_id=o.get("trace_id", ""),
            queued_at=float(o.get("queued_at", 0.0)),
        )
        for o in d.get("outcomes", [])
    ]

    return ContextSession(
        session_ref=d.get("session_ref", ""),
        created_at=float(d.get("created_at", 0.0)),
        expires_at=float(d.get("expires_at", 0.0)),
        scope=d.get("scope", {}),
        original_query=d.get("original_query", ""),
        datasource_id=d.get("datasource_id", ""),
        snapshots=snapshots,
        outcomes=outcomes,
    )


def _rebuild_anchor_set(d: dict) -> AnchorSet:
    """Rebuild an AnchorSet from a plain dict."""
    from ..runtime.anchors import AnchorNode

    def _node(nd: dict) -> AnchorNode:
        return AnchorNode(
            key=nd.get("key", ""),
            label=nd.get("label", ""),
            name=nd.get("name", ""),
            score=float(nd.get("score", 1.0)),
            vec_score=float(nd.get("vec_score", 0.0)),
            source=nd.get("source", "rule"),
            description=nd.get("description", ""),
            aliases=[
                p.strip()
                for x in (nd.get("aliases") or [])
                for p in (str(x).split("$$$") if "$$$" in str(x) else [str(x)])
                if p.strip()
            ],
            rerank_score=float(nd.get("rerank_score", -1.0)),
            domain=nd.get("domain", ""),
        )

    return AnchorSet(
        anchors=[_node(n) for n in d.get("anchors", [])],
        time_hints=list(d.get("time_hints", [])),
        question=d.get("question", ""),
        db_id=d.get("db_id", ""),
        anchors_metric=[_node(n) for n in d.get("anchors_metric", [])],
        anchors_dimension=[_node(n) for n in d.get("anchors_dimension", [])],
        anchors_column=[_node(n) for n in d.get("anchors_column", [])],
        anchors_knowledge=[_node(n) for n in d.get("anchors_knowledge", [])],
    )


# --------------------------------------------------------------------------- #
# SQLite schema (for SessionStore persistence)
# --------------------------------------------------------------------------- #

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_ref    TEXT PRIMARY KEY,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    datasource_id  TEXT NOT NULL DEFAULT '',
    scope          TEXT NOT NULL DEFAULT '{}',
    original_query TEXT NOT NULL DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""
