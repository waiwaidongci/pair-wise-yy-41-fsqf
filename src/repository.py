from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import DETOUR_KINDS, NOTICE_STATES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        notice_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in NOTICE_STATES)
        detour_kinds = ",".join("'" + k.replace("'", "''") + "'" for k in DETOUR_KINDS)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS restriction_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ({notice_statuses})),
                    severity TEXT NOT NULL,
                    snapshot TEXT,
                    emergency INTEGER NOT NULL DEFAULT 0,
                    reviewer TEXT,
                    evidence TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id INTEGER NOT NULL
                        REFERENCES restriction_notices(id) ON DELETE CASCADE,
                    level TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'approved',
                    comment TEXT,
                    evidence TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publish_quota_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    notice_id INTEGER NOT NULL
                        REFERENCES restriction_notices(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'occupied'
                        CHECK(status IN ('occupied','released')),
                    occupied_at TEXT NOT NULL,
                    released_at TEXT,
                    UNIQUE(bridge_id, notice_id)
                );
                CREATE TABLE IF NOT EXISTS detour_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ({detour_kinds})),
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL DEFAULT 0,
                    current_load REAL NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS detour_impacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    notice_id INTEGER NOT NULL
                        REFERENCES restriction_notices(id) ON DELETE CASCADE,
                    adjacent_bridge TEXT NOT NULL,
                    extra_load REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(notice_id, adjacent_bridge)
                );
            """)

    # ------------------------------------------------------------------
    # items
    # ------------------------------------------------------------------

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # ------------------------------------------------------------------
    # records
    # ------------------------------------------------------------------

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # restriction notices
    # ------------------------------------------------------------------

    def create_notice(self, bridge_id: int, severity: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(bridge_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO restriction_notices(bridge_id, status, severity, version,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
                (bridge_id, 'pending_approval', severity, 1, actor, now, now),
            )
            notice_id = int(cur.lastrowid)
        return self.get_notice(notice_id)

    def get_notice(self, notice_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM restriction_notices WHERE id=?", (notice_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("限行通告不存在")
        return self._notice(row)

    def list_notices(self, bridge_id: Optional[int] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM restriction_notices WHERE 1=1"
        params: list = []
        if bridge_id is not None:
            sql += " AND bridge_id=?"
            params.append(bridge_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._notice(row) for row in rows]

    def update_notice_status(self, notice_id: int, target: str,
                             expected_version: int, actor: str,
                             **extra) -> Dict[str, Any]:
        """更新通告状态，带乐观锁。extra 可包含 snapshot/published_at/reviewer 等。"""
        now = utc_now()
        sets = ["status=?", "version=version+1", "updated_at=?"]
        params: list = [target, now]
        for key in ('snapshot', 'published_at', 'reviewer', 'evidence', 'emergency'):
            if key in extra and extra[key] is not None:
                sets.append(f"{key}=?")
                params.append(extra[key])
        params.extend([notice_id, expected_version])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE restriction_notices SET {', '.join(sets)} WHERE id=? AND version=?",
                params,
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM restriction_notices WHERE id=?", (notice_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("限行通告不存在")
                raise ConflictError("通告版本冲突，请刷新后重试")
        return self.get_notice(notice_id)

    @staticmethod
    def _notice(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------

    def add_approval(self, notice_id: int, level: str, actor: str,
                     decision: str = 'approved', comment: Optional[str] = None,
                     evidence: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO approvals(notice_id, level, actor, decision, comment,
                   evidence, created_at) VALUES(?,?,?,?,?,?,?)""",
                (notice_id, level, actor, decision, comment, evidence, now),
            )
            approval_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        return dict(row)

    def list_approvals(self, notice_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE notice_id=? ORDER BY id", (notice_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # publish quota ledger
    # ------------------------------------------------------------------

    def occupy_quota(self, bridge_id: int, notice_id: int, actor: str) -> Dict[str, Any]:
        """占用发布额度（幂等）。若该桥额度已被占用，抛出 ConflictError 并携带占用者信息。"""
        now = utc_now()
        with self._lock, self.conn:
            # 幂等：若该通告已占用，直接返回
            existing = self.conn.execute(
                "SELECT * FROM publish_quota_ledger WHERE bridge_id=? AND notice_id=?",
                (bridge_id, notice_id),
            ).fetchone()
            if existing is not None:
                if existing['status'] == 'occupied':
                    return dict(existing)
                # 已释放则重新占用
                self.conn.execute(
                    "UPDATE publish_quota_ledger SET status='occupied', actor=?, "
                    "occupied_at=?, released_at=NULL WHERE id=?",
                    (actor, now, existing['id']),
                )
                return dict(self.conn.execute(
                    "SELECT * FROM publish_quota_ledger WHERE id=?", (existing['id'],)
                ).fetchone())
            # 检查该桥当前占用额度
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM publish_quota_ledger "
                "WHERE bridge_id=? AND status='occupied'",
                (bridge_id,),
            ).fetchone()
            if int(row['n']) >= 1:
                holder = self.conn.execute(
                    "SELECT * FROM publish_quota_ledger "
                    "WHERE bridge_id=? AND status='occupied' ORDER BY id LIMIT 1",
                    (bridge_id,),
                ).fetchone()
                raise ConflictError(
                    f"发布额度已被占用，占用者：{holder['actor']}（通告#{holder['notice_id']}）"
                )
            cur = self.conn.execute(
                """INSERT INTO publish_quota_ledger(bridge_id, notice_id, actor, status,
                   occupied_at) VALUES(?,?,?,?,?)""",
                (bridge_id, notice_id, actor, 'occupied', now),
            )
            quota_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM publish_quota_ledger WHERE id=?", (quota_id,)
            ).fetchone()
        return dict(row)

    def release_quota(self, notice_id: int) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE publish_quota_ledger SET status='released', released_at=? "
                "WHERE notice_id=? AND status='occupied'",
                (now, notice_id),
            )

    def get_quota_holder(self, bridge_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM publish_quota_ledger "
                "WHERE bridge_id=? AND status='occupied' ORDER BY id LIMIT 1",
                (bridge_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # detour routes & impacts
    # ------------------------------------------------------------------

    def add_detour_route(self, bridge_id: int, kind: str, name: str,
                         capacity: float, current_load: float,
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(bridge_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO detour_routes(bridge_id, kind, name, capacity,
                   current_load, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (bridge_id, kind, name, capacity, current_load, actor, now),
            )
            route_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM detour_routes WHERE id=?", (route_id,)
            ).fetchone()
        return dict(row)

    def list_detour_routes(self, bridge_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM detour_routes WHERE bridge_id=? ORDER BY id", (bridge_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_detour_impact(self, bridge_id: int, notice_id: int,
                          adjacent_bridge: str, extra_load: float) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO detour_impacts(bridge_id, notice_id, adjacent_bridge,
                   extra_load, created_at) VALUES(?,?,?,?,?)""",
                (bridge_id, notice_id, adjacent_bridge, extra_load, now),
            )
            impact_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM detour_impacts WHERE id=?", (impact_id,)
            ).fetchone()
        return dict(row)

    def list_detour_impacts(self, bridge_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM detour_impacts WHERE bridge_id=? ORDER BY id", (bridge_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["detail"] = json.loads(item["detail"])
                result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
            previous = "GENESIS"
            for row in rows:
                if row["previous_hash"] != previous:
                    return False
                payload = {
                    "action": row["action"], "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"], "actor": row["actor"],
                    "detail": json.loads(row["detail"]), "created_at": row["created_at"],
                }
                if calculate_hash(previous, payload) != row["entry_hash"]:
                    return False
                previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
