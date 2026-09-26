from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

DUTY_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS duty_shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','completed','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duty_shifts_temple ON duty_shifts(temple_id,state,starts_at);
CREATE TABLE IF NOT EXISTS duty_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_code TEXT NOT NULL UNIQUE,
    shift_id INTEGER NOT NULL REFERENCES duty_shifts(id),
    parent_grant_id INTEGER REFERENCES duty_grants(id),
    subject_hash TEXT NOT NULL,
    temple_id INTEGER NOT NULL REFERENCES temple_sites(id),
    hall_id INTEGER REFERENCES worship_halls(id),
    actions_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    delegation_depth INTEGER NOT NULL DEFAULT 0 CHECK(delegation_depth >= 0),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked','expired')),
    revoked_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duty_grants_lookup ON duty_grants(subject_hash,temple_id,state,valid_from,valid_until);
CREATE INDEX IF NOT EXISTS idx_duty_grants_parent ON duty_grants(parent_grant_id);
CREATE INDEX IF NOT EXISTS idx_duty_grants_shift ON duty_grants(shift_id,subject_hash,state);
CREATE TABLE IF NOT EXISTS duty_grant_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id INTEGER NOT NULL REFERENCES duty_grants(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duty_grant_events ON duty_grant_events(grant_id,id);
'''

DUTY_ACTIONS = (
    "observation.ingest",
    "mitigation.start",
    "mitigation.finish",
    "restoration.manage",
    "analytics.read",
)

MAX_DELEGATION_DEPTH = int(os.getenv("TEMPLE_DUTY_MAX_DELEGATION_DEPTH", "2"))

REASON_LABELS = {
    "matched": "授权匹配",
    "no_grant": "没有任何范围授权记录",
    "revoked": "授权已收回",
    "expired": "授权已过期",
    "shift_cancelled": "班次已取消",
    "shift_completed": "班次已结束",
    "parent_revoked": "上级授权已收回",
    "not_yet_valid": "授权尚未生效",
    "hall_out_of_scope": "殿堂不在授权范围内",
    "action_not_granted": "业务动作不在授权范围内",
    "no_active_authorization": "长期授权不存在或已失效",
}


def ensure_duty_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(DUTY_SCHEMA)


class DutyAuthorizationService:
    """与值守班次绑定的范围授权。

    授权限定寺院、殿堂、业务动作与生失效时间；临时委派必须引用委派人当前
    有效的授权并收缩范围，转授深度逐级递减。班次取消、人员离岗或上级授权
    收回会级联收回尚未使用的授权，已完成的操作保留在事件与审计记录中。
    所有判定直接读取 SQLite 当前状态，进程内不缓存任何授权结果，重启后
    只可能缩小、不可能扩大任何人的可见范围。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_duty_schema(self.connection)
        self.clock = clock or SystemClock()

    # ---- 班次生命周期 ----

    def create_shift(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple_by_code(payload["temple_code"])
        starts = self._parse_time(payload["starts_at"], "班次开始时间")
        ends = self._parse_time(payload["ends_at"], "班次结束时间")
        if ends <= starts:
            raise ValidationError("班次结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO duty_shifts(temple_id,code,name,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], starts, ends, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("班次编码已存在") from exc
            return self.shift_detail(cursor.lastrowid, connection)

    def list_shifts(self, temple_code: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("n.code=?")
            params.append(temple_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT s.*,n.code AS temple_code FROM duty_shifts s JOIN temple_sites n ON n.id=s.temple_id"
            + where
            + " ORDER BY s.starts_at DESC,s.id DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def shift_detail(self, shift_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT s.*,n.code AS temple_code FROM duty_shifts s JOIN temple_sites n ON n.id=s.temple_id WHERE s.id=?",
            (shift_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("值守班次不存在")
        result = dict(row)
        grants = connection.execute(
            "SELECT g.*,h.code AS hall_code FROM duty_grants g LEFT JOIN worship_halls h ON h.id=g.hall_id WHERE g.shift_id=? ORDER BY g.id",
            (shift_id,),
        ).fetchall()
        result["grants"] = [self._grant_dict(grant) for grant in grants]
        return result

    def cancel_shift(self, shift_id: int, actor: str, reason: str = "") -> dict[str, Any]:
        return self._terminate_shift(
            shift_id,
            actor,
            reason,
            target="cancelled",
            conflict_message="班次已结束，不能取消",
            grant_reason="shift_cancelled",
        )

    def complete_shift(self, shift_id: int, actor: str, reason: str = "") -> dict[str, Any]:
        return self._terminate_shift(
            shift_id,
            actor,
            reason,
            target="completed",
            conflict_message="班次已取消，不能结束",
            grant_reason="shift_completed",
        )

    def offboard_shift_member(self, shift_id: int, subject_hash: str, actor: str, reason: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            shift = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
            if shift is None:
                raise NotFoundError("值守班次不存在")
            rows = connection.execute(
                "SELECT id FROM duty_grants WHERE shift_id=? AND subject_hash=? AND state='active' ORDER BY id",
                (shift_id, subject_hash),
            ).fetchall()
            revoked = self._revoke_many(
                connection,
                [row["id"] for row in rows],
                reason="offboarded",
                actor=actor,
                now=now,
                detail={"shift_id": shift_id, "subject_hash": subject_hash, "note": reason},
            )
            return {"shift_id": shift_id, "subject_hash": subject_hash, "revoked_grant_ids": revoked}

    # ---- 授权与委派 ----

    def create_grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        shift = self._shift_by_code(payload["shift_code"])
        starts = self._parse_time(payload["valid_from"], "授权生效时间")
        ends = self._parse_time(payload["valid_until"], "授权失效时间")
        if ends <= starts:
            raise ValidationError("授权失效时间必须晚于生效时间")
        if starts < shift["starts_at"] or ends > shift["ends_at"]:
            raise ValidationError("授权有效期必须落在班次时间范围内")
        actions = self._parse_actions(payload["actions"])
        hall_id = self._hall_id(shift["temple_id"], payload.get("hall_code"))
        requested_depth = payload.get("delegation_depth")
        depth = MAX_DELEGATION_DEPTH if requested_depth is None else min(max(int(requested_depth), 0), MAX_DELEGATION_DEPTH)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM duty_grants WHERE grant_code=?", (payload["grant_code"],)).fetchone()
            if existing is not None:
                self._assert_same_grant(
                    existing,
                    shift_id=shift["id"],
                    parent_grant_id=None,
                    subject_hash=payload["subject_hash"],
                    temple_id=shift["temple_id"],
                    hall_id=hall_id,
                    actions=actions,
                    valid_from=starts,
                    valid_until=ends,
                    delegation_depth=depth,
                )
                return self.grant_detail(existing["id"], connection) | {"duplicate": True}
            current = connection.execute("SELECT state FROM duty_shifts WHERE id=?", (shift["id"],)).fetchone()
            if current["state"] != "scheduled":
                raise ConflictError("班次已取消或已结束，不能新增授权")
            cursor = connection.execute(
                "INSERT INTO duty_grants(grant_code,shift_id,parent_grant_id,subject_hash,temple_id,hall_id,actions_json,valid_from,valid_until,delegation_depth,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["grant_code"],
                    shift["id"],
                    None,
                    payload["subject_hash"],
                    shift["temple_id"],
                    hall_id,
                    json.dumps(actions, ensure_ascii=False),
                    starts,
                    ends,
                    depth,
                    payload["actor"],
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                cursor.lastrowid,
                "created",
                payload["actor"],
                {"shift_code": shift["code"], "actions": actions, "delegation_depth": depth},
                now,
            )
            return self.grant_detail(cursor.lastrowid, connection)

    def delegate_grant(self, parent_grant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            parent = connection.execute("SELECT * FROM duty_grants WHERE id=?", (parent_grant_id,)).fetchone()
            if parent is None:
                raise NotFoundError("上级授权不存在")
            starts = self._parse_time(payload["valid_from"], "授权生效时间")
            ends = self._parse_time(payload["valid_until"], "授权失效时间")
            actions = self._parse_actions(payload["actions"])
            existing = connection.execute("SELECT * FROM duty_grants WHERE grant_code=?", (payload["grant_code"],)).fetchone()
            if existing is not None:
                self._assert_same_grant(
                    existing,
                    shift_id=parent["shift_id"],
                    parent_grant_id=parent["id"],
                    subject_hash=payload["subject_hash"],
                    temple_id=parent["temple_id"],
                    hall_id=self._candidate_hall_id(parent, payload.get("hall_code")),
                    actions=actions,
                    valid_from=starts,
                    valid_until=ends,
                    delegation_depth=parent["delegation_depth"] - 1,
                )
                return self.grant_detail(existing["id"], connection) | {"duplicate": True}
            if parent["state"] != "active" or parent["valid_from"] > now or parent["valid_until"] <= now:
                raise ConflictError("委派人的授权当前无效，不能继续委派")
            shift = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (parent["shift_id"],)).fetchone()
            if shift["state"] != "scheduled":
                raise ConflictError("班次已取消或已结束，不能继续委派")
            if not self._ancestors_active(connection, parent):
                raise ConflictError("委派链上游授权已失效，不能继续委派")
            if parent["delegation_depth"] < 1:
                raise ConflictError("转授深度已用尽，不能继续委派")
            if ends <= starts:
                raise ValidationError("授权失效时间必须晚于生效时间")
            if starts < parent["valid_from"] or ends > parent["valid_until"]:
                raise ValidationError("委派有效期必须落在委派人授权有效期内")
            parent_actions = set(json.loads(parent["actions_json"]))
            if not set(actions) <= parent_actions:
                raise ValidationError("委派动作必须包含在委派人授权动作内")
            hall_id = self._delegated_hall_id(parent, payload.get("hall_code"))
            depth = parent["delegation_depth"] - 1
            cursor = connection.execute(
                "INSERT INTO duty_grants(grant_code,shift_id,parent_grant_id,subject_hash,temple_id,hall_id,actions_json,valid_from,valid_until,delegation_depth,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["grant_code"],
                    parent["shift_id"],
                    parent["id"],
                    payload["subject_hash"],
                    parent["temple_id"],
                    hall_id,
                    json.dumps(actions, ensure_ascii=False),
                    starts,
                    ends,
                    depth,
                    payload["actor"],
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                cursor.lastrowid,
                "delegated",
                payload["actor"],
                {"parent_grant_id": parent["id"], "parent_grant_code": parent["grant_code"], "actions": actions, "delegation_depth": depth},
                now,
            )
            return self.grant_detail(cursor.lastrowid, connection)

    def revoke_grant(self, grant_id: int, actor: str, reason: str = "") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM duty_grants WHERE id=?", (grant_id,)).fetchone()
            if row is None:
                raise NotFoundError("授权不存在")
            if row["state"] != "active":
                return self.grant_detail(grant_id, connection)
            self._revoke_many(connection, [grant_id], reason="manual", actor=actor, now=now, detail={"note": reason})
            return self.grant_detail(grant_id, connection)

    def grant_detail(self, grant_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT g.*,h.code AS hall_code,s.code AS shift_code,n.code AS temple_code "
            "FROM duty_grants g JOIN duty_shifts s ON s.id=g.shift_id JOIN temple_sites n ON n.id=g.temple_id "
            "LEFT JOIN worship_halls h ON h.id=g.hall_id WHERE g.id=?",
            (grant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("授权不存在")
        result = self._grant_dict(row)
        result["chain"] = self._chain(connection, row)
        events = connection.execute("SELECT * FROM duty_grant_events WHERE grant_id=? ORDER BY id", (grant_id,)).fetchall()
        result["events"] = [self._event_dict(event) for event in events]
        return result

    # ---- 权限判定与解释 ----

    def decide(self, *, subject_hash: str, temple_id: int, hall_id: int | None, action: str, at: str) -> dict[str, Any]:
        if action not in DUTY_ACTIONS:
            raise ValidationError(f"未知的业务动作：{action}")
        moment = self._parse_time(at, "评估时间")
        rows = self.connection.execute(
            "SELECT g.*,h.code AS hall_code,s.code AS shift_code,s.state AS shift_state,n.code AS temple_code "
            "FROM duty_grants g JOIN duty_shifts s ON s.id=g.shift_id JOIN temple_sites n ON n.id=g.temple_id "
            "LEFT JOIN worship_halls h ON h.id=g.hall_id "
            "WHERE g.subject_hash=? AND g.temple_id=? ORDER BY g.id",
            (subject_hash, temple_id),
        ).fetchall()
        sources: list[dict[str, Any]] = []
        denials: list[dict[str, Any]] = []
        for row in rows:
            evaluation = self._evaluate(self.connection, row, hall_id=hall_id, action=action, at=moment)
            (sources if evaluation["result"] == "allowed" else denials).append(evaluation)
        decision = {"allowed": bool(sources), "action": action, "at": moment, "sources": sources, "denials": denials}
        decision["summary"] = self._summary(decision)
        return decision

    def explain(
        self,
        *,
        subject_hash: str,
        temple_code: str,
        action: str,
        hall_code: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        temple = self._temple_by_code(temple_code)
        hall_id = self._hall_id(temple["id"], hall_code)
        moment = self._parse_time(at, "评估时间") if at else to_storage(self.clock.now())
        decision = self.decide(subject_hash=subject_hash, temple_id=temple["id"], hall_id=hall_id, action=action, at=moment)
        if action == "mitigation.start":
            self._include_legacy_authorization(decision, subject_hash, temple["id"], moment)
        decision.update({"subject_hash": subject_hash, "temple_code": temple["code"], "hall_code": hall_code})
        decision["summary"] = self._summary(decision)
        return decision

    # ---- 到期收敛（重启后只缩不扩） ----

    def sweep_expired(self, actor: str = "duty-scope-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            rows = connection.execute("SELECT id FROM duty_grants WHERE state='active' AND valid_until<=? ORDER BY id", (now,)).fetchall()
            expired: list[int] = []
            for row in rows:
                connection.execute("UPDATE duty_grants SET state='expired',updated_at=? WHERE id=? AND state='active'", (now, row["id"]))
                self._event(connection, row["id"], "expired", actor, {}, now)
                expired.append(row["id"])
            shifts = connection.execute("SELECT id FROM duty_shifts WHERE state='scheduled' AND ends_at<=? ORDER BY id", (now,)).fetchall()
            completed: list[int] = []
            for shift in shifts:
                connection.execute("UPDATE duty_shifts SET state='completed',updated_at=? WHERE id=? AND state='scheduled'", (now, shift["id"]))
                completed.append(shift["id"])
            return {"expired_grants": expired, "completed_shifts": completed}

    # ---- 内部实现 ----

    def _terminate_shift(self, shift_id: int, actor: str, reason: str, *, target: str, conflict_message: str, grant_reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            shift = connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
            if shift is None:
                raise NotFoundError("值守班次不存在")
            if shift["state"] == target:
                return self.shift_detail(shift_id, connection)
            if shift["state"] != "scheduled":
                raise ConflictError(conflict_message)
            connection.execute("UPDATE duty_shifts SET state=?,updated_at=? WHERE id=?", (target, now, shift_id))
            rows = connection.execute("SELECT id FROM duty_grants WHERE shift_id=? AND state='active' ORDER BY id", (shift_id,)).fetchall()
            self._revoke_many(
                connection,
                [row["id"] for row in rows],
                reason=grant_reason,
                actor=actor,
                now=now,
                detail={"shift_id": shift_id, "shift_code": shift["code"], "note": reason},
            )
            return self.shift_detail(shift_id, connection)

    def _revoke_many(
        self,
        connection: sqlite3.Connection,
        root_ids: list[int],
        *,
        reason: str,
        actor: str,
        now: str,
        detail: dict[str, Any] | None = None,
    ) -> list[int]:
        if not root_ids:
            return []
        related = self._descendants(connection, root_ids)
        revoked: list[int] = []
        for grant_id, depth in sorted(related.items(), key=lambda item: (item[1], item[0])):
            row = connection.execute("SELECT state FROM duty_grants WHERE id=?", (grant_id,)).fetchone()
            if row is None or row["state"] != "active":
                continue
            grant_reason = reason if depth == 0 else "parent_revoked"
            connection.execute(
                "UPDATE duty_grants SET state='revoked',revoked_reason=?,updated_at=? WHERE id=? AND state='active'",
                (grant_reason, now, grant_id),
            )
            self._event(connection, grant_id, "revoked", actor, {"reason": grant_reason, **(detail or {})}, now)
            revoked.append(grant_id)
        return revoked

    @staticmethod
    def _descendants(connection: sqlite3.Connection, root_ids: list[int]) -> dict[int, int]:
        related: dict[int, int] = {}
        frontier = [(grant_id, 0) for grant_id in root_ids]
        while frontier:
            grant_id, depth = frontier.pop()
            if grant_id in related:
                continue
            related[grant_id] = depth
            rows = connection.execute("SELECT id FROM duty_grants WHERE parent_grant_id=?", (grant_id,)).fetchall()
            frontier.extend((row["id"], depth + 1) for row in rows)
        return related

    def _evaluate(self, connection: sqlite3.Connection, row: sqlite3.Row, *, hall_id: int | None, action: str, at: str) -> dict[str, Any]:
        evaluation = self._grant_dict(row)
        evaluation.update({"kind": "duty_grant", "grant_id": row["id"], "shift_state": row["shift_state"]})
        reason: str | None = None
        if row["state"] == "revoked":
            reason = "revoked"
        elif row["state"] == "expired":
            reason = "expired"
        elif row["shift_state"] == "cancelled":
            reason = "shift_cancelled"
        elif row["shift_state"] == "completed":
            reason = "shift_completed"
        elif not self._ancestors_active(connection, row):
            reason = "parent_revoked"
        elif row["valid_from"] > at:
            reason = "not_yet_valid"
        elif row["valid_until"] <= at:
            reason = "expired"
        elif row["hall_id"] is not None and row["hall_id"] != hall_id:
            reason = "hall_out_of_scope"
        elif action not in json.loads(row["actions_json"]):
            reason = "action_not_granted"
        evaluation["result"] = "allowed" if reason is None else "denied"
        evaluation["reason"] = reason or "matched"
        evaluation["reason_label"] = REASON_LABELS[evaluation["reason"]]
        evaluation["chain"] = self._chain(connection, row)
        return evaluation

    def _include_legacy_authorization(self, decision: dict[str, Any], subject_hash: str, temple_id: int, at: str) -> None:
        rows = self.connection.execute(
            "SELECT * FROM steward_authorizations WHERE steward_hash=? AND temple_id=? ORDER BY id",
            (subject_hash, temple_id),
        ).fetchall()
        active = [row for row in rows if row["state"] == "active" and row["valid_from"] <= at and row["valid_until"] > at]
        for row in active:
            decision["sources"].append(
                {
                    "kind": "steward_authorization",
                    "authorization_id": row["id"],
                    "authorization_code": row["authorization_code"],
                    "valid_from": row["valid_from"],
                    "valid_until": row["valid_until"],
                    "result": "allowed",
                    "reason": "matched",
                    "reason_label": REASON_LABELS["matched"],
                }
            )
        if rows and not active:
            decision["denials"].append(
                {
                    "kind": "steward_authorization",
                    "result": "denied",
                    "reason": "no_active_authorization",
                    "reason_label": REASON_LABELS["no_active_authorization"],
                }
            )
        decision["allowed"] = bool(decision["sources"])

    def _chain(self, connection: sqlite3.Connection, grant: sqlite3.Row) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        current: sqlite3.Row | None = grant
        seen: set[int] = set()
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(
                {
                    "grant_id": current["id"],
                    "grant_code": current["grant_code"],
                    "subject_hash": current["subject_hash"],
                    "delegation_depth": current["delegation_depth"],
                    "state": current["state"],
                }
            )
            parent_id = current["parent_grant_id"]
            current = connection.execute("SELECT * FROM duty_grants WHERE id=?", (parent_id,)).fetchone() if parent_id is not None else None
        chain.reverse()
        return chain

    @staticmethod
    def _ancestors_active(connection: sqlite3.Connection, grant: sqlite3.Row) -> bool:
        parent_id = grant["parent_grant_id"]
        seen: set[int] = set()
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            row = connection.execute("SELECT state,parent_grant_id FROM duty_grants WHERE id=?", (parent_id,)).fetchone()
            if row is None or row["state"] != "active":
                return False
            parent_id = row["parent_grant_id"]
        return True

    @staticmethod
    def _summary(decision: dict[str, Any]) -> str:
        if decision["allowed"]:
            first = decision["sources"][0]
            if first["kind"] == "duty_grant":
                origin = f"授权 {first['grant_code']}（班次 {first['shift_code']}）"
            else:
                origin = f"长期授权 {first['authorization_code']}"
            return f"允许：{decision['action']} 由{origin}提供，共 {len(decision['sources'])} 条有效来源"
        reasons = "；".join(dict.fromkeys(denial["reason_label"] for denial in decision["denials"]))
        return f"拒绝：{reasons or REASON_LABELS['no_grant']}"

    def _candidate_hall_id(self, parent: sqlite3.Row, hall_code: str | None) -> int | None:
        # 幂等重放比对用：按创建时的继承规则还原目标殿堂，不做扩权校验。
        if hall_code is None and parent["hall_id"] is not None:
            return parent["hall_id"]
        return self._hall_id(parent["temple_id"], hall_code)

    def _delegated_hall_id(self, parent: sqlite3.Row, hall_code: str | None) -> int | None:
        if parent["hall_id"] is not None:
            if hall_code is None:
                return parent["hall_id"]
            hall = self._hall_row(parent["temple_id"], hall_code)
            if hall["id"] != parent["hall_id"]:
                raise ValidationError("委派殿堂不能超出委派人授权殿堂")
            return parent["hall_id"]
        return self._hall_id(parent["temple_id"], hall_code)

    def _hall_id(self, temple_id: int, hall_code: str | None) -> int | None:
        if hall_code is None:
            return None
        return self._hall_row(temple_id, hall_code)["id"]

    def _hall_row(self, temple_id: int, hall_code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM worship_halls WHERE temple_id=? AND code=?", (temple_id, hall_code)).fetchone()
        if row is None:
            raise NotFoundError("寺院殿堂不存在")
        return row

    def _temple_by_code(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM temple_sites WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _shift_by_code(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM duty_shifts WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("值守班次不存在")
        return row

    @staticmethod
    def _assert_same_grant(
        existing: sqlite3.Row,
        *,
        shift_id: int,
        parent_grant_id: int | None,
        subject_hash: str,
        temple_id: int,
        hall_id: int | None,
        actions: list[str],
        valid_from: str,
        valid_until: str,
        delegation_depth: int,
    ) -> None:
        same = (
            existing["shift_id"] == shift_id
            and existing["parent_grant_id"] == parent_grant_id
            and existing["subject_hash"] == subject_hash
            and existing["temple_id"] == temple_id
            and existing["hall_id"] == hall_id
            and json.loads(existing["actions_json"]) == actions
            and existing["valid_from"] == valid_from
            and existing["valid_until"] == valid_until
            and existing["delegation_depth"] == delegation_depth
        )
        if not same:
            raise ConflictError("授权编码已存在且内容不一致")

    @staticmethod
    def _parse_time(value: str, label: str) -> str:
        try:
            return to_storage(from_storage(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    @staticmethod
    def _parse_actions(actions: list[str]) -> list[str]:
        if not actions:
            raise ValidationError("业务动作不能为空")
        unknown = sorted(set(actions) - set(DUTY_ACTIONS))
        if unknown:
            raise ValidationError("未知的业务动作：" + ",".join(unknown))
        return sorted(set(actions))

    @staticmethod
    def _grant_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["actions"] = json.loads(result.pop("actions_json"))
        return result

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    @staticmethod
    def _event(connection: sqlite3.Connection, grant_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO duty_grant_events(grant_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (grant_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
