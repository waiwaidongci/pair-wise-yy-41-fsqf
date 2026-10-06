from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import calculate_hash, make_entry, utc_now
from .domain import (CONTEXT_KINDS, LEVELS, STATES, ConflictError, NotFoundError,
                     QuotaConflict, ValidationError)

BRIDGE_STATUSES = ['normal', 'load_limit', 'lane_close', 'full_close']


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # 故障注入：tag -> 剩余触发次数，用于失败恢复测试
        self.fault_points: Dict[str, int] = {}
        self._create_schema()

    def inject_fault(self, tag: str, times: int = 1) -> None:
        self.fault_points[tag] = self.fault_points.get(tag, 0) + times

    def _maybe_fail(self, tag: str) -> None:
        remaining = self.fault_points.get(tag, 0)
        if remaining > 0:
            self.fault_points[tag] = remaining - 1
            raise RuntimeError(f"injected write failure: {tag}")

    def _create_schema(self) -> None:
        states = ",".join("'" + s + "'" for s in STATES)
        bridge_states = ",".join("'" + s + "'" for s in BRIDGE_STATUSES)
        levels = ",".join("'" + l + "'" for l in LEVELS)
        kinds = ",".join("'" + k + "'" for k in CONTEXT_KINDS)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS bridges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    capacity REAL NOT NULL,
                    daily_vehicles REAL NOT NULL DEFAULT 0,
                    daily_buses REAL NOT NULL DEFAULT 0,
                    neighbor_id INTEGER REFERENCES bridges(id),
                    status TEXT NOT NULL DEFAULT 'normal' CHECK(status IN ({bridge_states})),
                    provisional INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES bridges(id),
                    neighbor_id INTEGER REFERENCES bridges(id),
                    level TEXT NOT NULL CHECK(level IN ({levels})),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({states})),
                    version INTEGER NOT NULL DEFAULT 1,
                    request_id TEXT NOT NULL UNIQUE,
                    network_budget REAL NOT NULL,
                    ambulance_required REAL NOT NULL DEFAULT 0,
                    assessment TEXT NOT NULL,
                    snapshot TEXT,
                    created_by TEXT NOT NULL,
                    released_by TEXT,
                    released_at TEXT,
                    emergency_evidence TEXT,
                    emergency_reviewer TEXT,
                    review_result TEXT,
                    review_detail TEXT,
                    invalidated_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bridge_id INTEGER NOT NULL REFERENCES bridges(id),
                    kind TEXT NOT NULL CHECK(kind IN ({kinds})),
                    detail TEXT NOT NULL,
                    ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diversion_loads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id INTEGER NOT NULL UNIQUE REFERENCES notices(id),
                    neighbor_id INTEGER REFERENCES bridges(id),
                    vehicles REAL NOT NULL,
                    buses REAL NOT NULL,
                    remaining_vehicles REAL NOT NULL,
                    remaining_buses REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','drained','cleared')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publish_quota (
                    bridge_id INTEGER PRIMARY KEY REFERENCES bridges(id),
                    operation_id TEXT NOT NULL,
                    holder_actor TEXT NOT NULL,
                    holder_notice_id INTEGER REFERENCES notices(id),
                    occupied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_steps (
                    operation_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done',
                    entity_id INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(operation_id, step)
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
            """)

    # ---------- 步骤表（失败后从剩余步骤恢复） ----------
    def step_done(self, operation_id: str, step: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM operation_steps WHERE operation_id=? AND step=?",
            (operation_id, step)).fetchone()

    def is_step_done(self, operation_id: str, step: str) -> bool:
        with self._lock:
            return self.step_done(operation_id, step) is not None

    def operation_notice(self, operation_id: str) -> Optional[int]:
        """恢复入口：若该操作已存在通告，返回通告id。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT holder_notice_id FROM publish_quota WHERE operation_id=?",
                (operation_id,)).fetchone()
            if row is not None and row["holder_notice_id"] is not None:
                return int(row["holder_notice_id"])
            row = self.conn.execute(
                "SELECT entity_id FROM operation_steps WHERE operation_id=? "
                "AND step='insert_notice'", (operation_id,)).fetchone()
        return int(row["entity_id"]) if row is not None else None

    def _mark_step(self, conn, operation_id: str, step: str,
                   entity_id: Optional[int]) -> None:
        conn.execute(
            """INSERT INTO operation_steps(operation_id, step, status, entity_id, updated_at)
               VALUES(?,?, 'done', ?,?)
               ON CONFLICT(operation_id, step) DO UPDATE SET
                 status='done', entity_id=excluded.entity_id, updated_at=excluded.updated_at""",
            (operation_id, step, entity_id, utc_now()))

    # ---------- 桥梁 ----------
    def create_bridge(self, name: str, capacity: float, daily_vehicles: float,
                      daily_buses: float, neighbor_id: Optional[int],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO bridges(name, capacity, daily_vehicles, daily_buses,
                       neighbor_id, status, provisional, created_by, created_at)
                       VALUES(?,?,?,?,?, 'normal', 0, ?,?)""",
                    (name, capacity, daily_vehicles, daily_buses, neighbor_id, actor, now))
                bridge_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("桥梁名称已存在") from exc
        return self.get_bridge(bridge_id)
    def get_bridge(self, bridge_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM bridges WHERE id=?", (bridge_id,)).fetchone()
        if row is None:
            raise NotFoundError("桥梁不存在")
        return dict(row)

    def list_bridges(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM bridges ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---------- 额度 ----------
    def get_quota(self, bridge_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM publish_quota WHERE bridge_id=?", (bridge_id,)).fetchone()
        return dict(row) if row else None

    def _quota_conflict(self, conn, bridge_id: int):
        row = conn.execute(
            """SELECT q.*, n.status AS notice_status FROM publish_quota q
               JOIN notices n ON n.id = q.holder_notice_id
               WHERE q.bridge_id=?""", (bridge_id,)).fetchone()
        if row is None:
            return QuotaConflict("发布额度已被占用")
        return QuotaConflict(
            f"发布额度已被{row['holder_actor']}的通告#{row['holder_notice_id']}占用",
            holder=row['holder_actor'], notice_id=row['holder_notice_id'])

    def acquire_quota(self, operation_id: str, bridge_id: int,
                      holder: str) -> Dict[str, Any]:
        """
        步骤1（原子）：先通过者占用发布额度，占用记录归属 operation_id。
        崩溃后续跑：同一操作的额度已存在直接复用（只记一次），
        被其他审批占用则抛出 QuotaConflict（后到者看到被谁占用）。
        """
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute(
                "SELECT * FROM publish_quota WHERE bridge_id=?", (bridge_id,)).fetchone()
            if row is not None:
                if row["operation_id"] == operation_id:
                    return dict(row)
                raise self._quota_conflict(conn, bridge_id)
            self._maybe_fail("acquire_quota")
            now = utc_now()
            try:
                conn.execute(
                    """INSERT INTO publish_quota(bridge_id, operation_id, holder_actor,
                       holder_notice_id, occupied_at) VALUES(?,?,?,NULL,?)""",
                    (bridge_id, operation_id, holder, now))
            except sqlite3.IntegrityError as exc:
                raise self._quota_conflict(conn, bridge_id) from exc
            row = conn.execute("SELECT * FROM publish_quota WHERE bridge_id=?",
                               (bridge_id,)).fetchone()
        return dict(row)

    def insert_notice(self, operation_id: str, step: str, bridge_id: int,
                      holder: str, level: str, reason: str, request_id: str,
                      network_budget: float, ambulance_required: float,
                      neighbor_id: Optional[int],
                      assessment: dict) -> Dict[str, Any]:
        """步骤2（原子）：创建未发布通告并回填额度。request_id 幂等。"""
        with self._lock, self.conn:
            conn = self.conn
            done = conn.execute(
                "SELECT entity_id FROM operation_steps WHERE operation_id=? AND step=?",
                (operation_id, step)).fetchone()
            if done is not None:
                return self.get_notice(done["entity_id"])
            existing = conn.execute(
                "SELECT id FROM notices WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                self._mark_step(conn, operation_id, step, int(existing["id"]))
                conn.execute(
                    "UPDATE publish_quota SET holder_notice_id=? WHERE operation_id=?",
                    (int(existing["id"]), operation_id))
                return self.get_notice(int(existing["id"]))
            self._maybe_fail("insert_notice")
            now = utc_now()
            cur = conn.execute(
                """INSERT INTO notices(bridge_id, neighbor_id, level, reason, status, version,
                   request_id, network_budget, ambulance_required, assessment, snapshot,
                   created_by, created_at, updated_at)
                   VALUES(?,?,?,?,'pending_engineer',1,?,?,?,?,NULL,?,?,?)""",
                (bridge_id, neighbor_id, level, reason, request_id, network_budget,
                 ambulance_required, json.dumps(assessment, ensure_ascii=False),
                 holder, now, now))
            notice_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE publish_quota SET holder_notice_id=? WHERE operation_id=?",
                (notice_id, operation_id))
            self._mark_step(conn, operation_id, step, notice_id)
        return self.get_notice(notice_id)

    # ---------- 通告 ----------
    def get_notice(self, notice_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT n.*, b.name AS bridge_name FROM notices n
                   JOIN bridges b ON b.id=n.bridge_id WHERE n.id=?""",
                (notice_id,)).fetchone()
        if row is None:
            raise NotFoundError("通告不存在")
        item = dict(row)
        item["assessment"] = json.loads(item["assessment"])
        item["snapshot"] = json.loads(item["snapshot"]) if item["snapshot"] else None
        return item

    def list_notices(self, bridge_id: Optional[int] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = """SELECT n.*, b.name AS bridge_name FROM notices n
                 JOIN bridges b ON b.id=n.bridge_id"""
        where, params = [], []
        if bridge_id is not None:
            where.append("n.bridge_id=?")
            params.append(bridge_id)
        if status:
            where.append("n.status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n.id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["assessment"] = json.loads(item["assessment"])
            item["snapshot"] = json.loads(item["snapshot"]) if item["snapshot"] else None
            result.append(item)
        return result

    def publish_notice(self, operation_id: str, step: str, notice_id: int,
                       expected_version: int, target: str, actor: str,
                       bridge_status: str, provisional: bool,
                       snapshot: Optional[dict], diversion: Optional[dict],
                       emergency: Optional[dict] = None) -> Dict[str, Any]:
        """
        步骤（原子）：放行迁移 + 乐观版本 + 施加/不施加绕行 + 源桥状态 + 已发布快照。
        紧急/常规发布共用；diversion 为 None 表示尚未对外发布（工程师一级放行）。
        """
        with self._lock, self.conn:
            done = self.step_done(operation_id, step)
            if done is not None:
                return self.get_notice(done["entity_id"])
            conn = self.conn
            row = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            if row is None:
                raise NotFoundError("通告不存在")
            # 恢复语义：发布步骤已完成（如审计写入失败后重跑），直接返回
            resumed = conn.execute(
                "SELECT 1 FROM operation_steps WHERE operation_id=? AND step=?",
                (operation_id, step)).fetchone()
            if resumed is not None or row["status"] == target:
                self._mark_step(conn, operation_id, step, notice_id)
                return self.get_notice(notice_id)
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            self._maybe_fail("publish")
            now = utc_now()
            emergency_cols = ""
            emergency_params: tuple = ()
            if emergency:
                emergency_cols = ", emergency_evidence=?, emergency_reviewer=?"
                emergency_params = (emergency["evidence"], emergency["reviewer"])
            cur = conn.execute(
                f"""UPDATE notices SET status=?, version=version+1, updated_at=?,
                    released_by=?, released_at=?, snapshot=?{emergency_cols}
                    WHERE id=?""",
                (target, now, actor, now,
                 json.dumps(snapshot, ensure_ascii=False) if snapshot else None)
                + emergency_params + (notice_id,))
            if cur.rowcount == 0:
                raise ConflictError("发布失败")
            if diversion is not None:
                conn.execute(
                    """INSERT INTO diversion_loads(notice_id, neighbor_id, vehicles, buses,
                       remaining_vehicles, remaining_buses, status, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,'active',?,?)
                       ON CONFLICT(notice_id) DO UPDATE SET
                         remaining_vehicles=excluded.remaining_vehicles,
                         remaining_buses=excluded.remaining_buses,
                         status='active', updated_at=excluded.updated_at""",
                    (notice_id, diversion.get("neighbor_id"), diversion["vehicles"],
                     diversion["buses"], diversion["vehicles"], diversion["buses"],
                     now, now))
            conn.execute(
                "UPDATE bridges SET status=?, provisional=? WHERE id=?",
                (bridge_status, 1 if provisional else 0, row["bridge_id"]))
            self._mark_step(conn, operation_id, step, notice_id)
        return self.get_notice(notice_id)

    def review_emergency(self, notice_id: int, expected_version: int,
                         approved: bool, reviewer: str, detail: str) -> Dict[str, Any]:
        """紧急放行复核：通过则转正式发布；不通过则撤销发布并释放额度。"""
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            if row is None:
                raise NotFoundError("通告不存在")
            if row["status"] != "emergency_pending_review":
                raise ConflictError("该通告不在紧急待复核状态")
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            now = utc_now()
            if approved:
                conn.execute(
                    """UPDATE notices SET status='restricted', version=version+1, updated_at=?,
                       review_result='approved', review_detail=?, emergency_reviewer=? WHERE id=?""",
                    (now, detail, reviewer, notice_id))
                conn.execute("UPDATE bridges SET provisional=0 WHERE id=?",
                             (row["bridge_id"],))
                load = conn.execute(
                    "SELECT * FROM diversion_loads WHERE notice_id=?", (notice_id,)).fetchone()
                if load is not None:
                    conn.execute(
                        "UPDATE diversion_loads SET status='active', updated_at=? WHERE id=?",
                        (now, load["id"]))
            else:
                conn.execute(
                    """UPDATE notices SET status='revoked', version=version+1, updated_at=?,
                       review_result='rejected', review_detail=?, emergency_reviewer=? WHERE id=?""",
                    (now, detail, reviewer, notice_id))
                conn.execute(
                    "UPDATE bridges SET status='normal', provisional=0 WHERE id=?",
                    (row["bridge_id"],))
                conn.execute(
                    "UPDATE diversion_loads SET status='cleared', remaining_vehicles=0, "
                    "remaining_buses=0, updated_at=? WHERE notice_id=?", (now, notice_id))
                conn.execute("DELETE FROM publish_quota WHERE bridge_id=?",
                             (row["bridge_id"],))
        return self.get_notice(notice_id)

    def restore_notice(self, operation_id: str, step: str, notice_id: int,
                       expected_version: int, actor: str) -> Dict[str, Any]:
        """步骤（原子）：恢复放行，须下游绕行流量已卸载；完成后释放额度、桥回正常。"""
        with self._lock, self.conn:
            done = self.step_done(operation_id, step)
            if done is not None:
                return self.get_notice(done["entity_id"])
            conn = self.conn
            row = conn.execute("SELECT * FROM notices WHERE id=?", (notice_id,)).fetchone()
            if row is None:
                raise NotFoundError("通告不存在")
            if row["status"] != "restricted":
                raise ConflictError("只有已发布限行通告可以申请恢复")
            if row["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            load = conn.execute(
                "SELECT * FROM diversion_loads WHERE notice_id=? AND status='active'",
                (notice_id,)).fetchone()
            if load is not None and (load["remaining_vehicles"] > 0
                                     or load["remaining_buses"] > 0):
                raise ConflictError(
                    f"相邻桥仍有未卸载绕行流量{load['remaining_vehicles']:.1f}/"
                    f"{load['remaining_buses']:.1f}，请等待下游卸载完成")
            now = utc_now()
            conn.execute(
                """UPDATE notices SET status='restored', version=version+1, updated_at=?
                   WHERE id=?""", (now, notice_id))
            conn.execute(
                "UPDATE diversion_loads SET status='cleared', updated_at=? WHERE notice_id=?",
                (now, notice_id))
            conn.execute("UPDATE bridges SET status='normal', provisional=0 WHERE id=?",
                         (row["bridge_id"],))
            conn.execute("DELETE FROM publish_quota WHERE bridge_id=?",
                         (row["bridge_id"],))
            self._mark_step(conn, operation_id, step, notice_id)
        return self.get_notice(notice_id)

    # ---------- 绕行卸载 ----------
    def get_diversion(self, notice_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM diversion_loads WHERE notice_id=?", (notice_id,)).fetchone()
        return dict(row) if row else None

    def offload(self, notice_id: int, vehicles: float, buses: float) -> Dict[str, Any]:
        with self._lock, self.conn:
            conn = self.conn
            row = conn.execute(
                "SELECT * FROM diversion_loads WHERE notice_id=?", (notice_id,)).fetchone()
            if row is None:
                raise NotFoundError("该通告没有绕行承载记录")
            if row["status"] != "active":
                raise ConflictError("绕行承载已结束，无需重复卸载")
            rem_v = max(0.0, row["remaining_vehicles"] - vehicles)
            rem_b = max(0.0, row["remaining_buses"] - buses)
            status = "drained" if rem_v == 0 and rem_b == 0 else "active"
            conn.execute(
                """UPDATE diversion_loads SET remaining_vehicles=?, remaining_buses=?,
                   status=?, updated_at=? WHERE id=?""",
                (rem_v, rem_b, status, utc_now(), row["id"]))
        return self.get_diversion(notice_id)

    # ---------- 气象/交通通告/巡检记录 + 失效重算 ----------
    def add_context_and_invalidate(self, bridge_id: int, kind: str, detail: str,
                                   ref: Optional[str], actor: str,
                                   reason: str) -> Dict[str, Any]:
        """
        原子操作：登记气象/交通通告/巡检记录；受影响桥梁上所有未发布通告失效，
        并释放其占用的发布额度。已发布通告保留原始快照，不在此列。
        受影响范围：该桥本身的通告，以及把该桥作为相邻承接桥的通告。
        """
        now = utc_now()
        with self._lock, self.conn:
            conn = self.conn
            cur = conn.execute(
                """INSERT INTO context_records(bridge_id, kind, detail, ref, created_by, created_at)
                   VALUES(?,?,?,?,?,?)""", (bridge_id, kind, detail, ref, actor, now))
            record_id = int(cur.lastrowid)
            rows = conn.execute(
                """SELECT id, bridge_id FROM notices
                   WHERE status IN ('pending_engineer','pending_supervisor')
                     AND (bridge_id=? OR neighbor_id=?)""",
                (bridge_id, bridge_id)).fetchall()
            invalidated = []
            for r in rows:
                conn.execute(
                    """UPDATE notices SET status='invalidated', version=version+1, updated_at=?,
                       invalidated_reason=? WHERE id=?""", (now, reason, r["id"]))
                conn.execute("DELETE FROM publish_quota WHERE bridge_id=?",
                             (r["bridge_id"],))
                invalidated.append(int(r["id"]))
            record = dict(conn.execute(
                "SELECT * FROM context_records WHERE id=?", (record_id,)).fetchone())
        return {"record": record, "invalidated_notice_ids": invalidated}

    def list_context(self, bridge_id: int) -> List[Dict[str, Any]]:
        self.get_bridge(bridge_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM context_records WHERE bridge_id=? ORDER BY id",
                (bridge_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 审计（步骤化、幂等） ----------
    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            event = self._insert_audit(self.conn, action, entity_type, entity_id,
                                       actor, detail)
        event["id"] = self.conn.execute(
            "SELECT id FROM audit_events WHERE entry_hash=?", (event["entry_hash"],)
        ).fetchone()["id"]
        return event

    @staticmethod
    def _insert_audit(conn, action, entity_type, entity_id, actor, detail) -> dict:
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]))
        return event

    def append_audit_step(self, operation_id: str, step: str, action: str,
                          entity_type: str, entity_id: int, actor: str,
                          detail: dict) -> Dict[str, Any]:
        """审计作为恢复流程中的一个步骤：崩溃重跑不重复记账。"""
        with self._lock, self.conn:
            conn = self.conn
            done = conn.execute(
                "SELECT entity_id FROM operation_steps WHERE operation_id=? AND step=?",
                (operation_id, step)).fetchone()
            if done is not None:
                return {"id": done["entity_id"], "resumed": True}
            self._maybe_fail("audit")
            event = self._insert_audit(conn, action, entity_type, entity_id,
                                       actor, detail)
            row = conn.execute(
                "SELECT id FROM audit_events WHERE entry_hash=?", (event["entry_hash"],)
            ).fetchone()
            self._mark_step(conn, operation_id, step, int(row["id"]))
        event["id"] = int(row["id"])
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
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM audit_events ORDER BY id").fetchall()
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
