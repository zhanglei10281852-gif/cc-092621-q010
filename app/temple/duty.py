from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.temple.repository import TempleRepository
from app.temple.schema import ensure_temple_schema

# 授权判定结果的原因码，explain 接口与内部判定共用，保证解释口径稳定。
REASON_OK = "ok"
REASON_NO_GRANTS = "no_grants"
REASON_NO_EFFECTIVE_GRANT = "no_effective_grant"
REASON_GRANT_REVOKED = "grant_revoked"
REASON_GRANT_CANCELLED = "grant_cancelled"
REASON_GRANT_EXPIRED = "grant_expired"
REASON_SHIFT_CANCELLED = "shift_cancelled"
REASON_NOT_YET_VALID = "not_yet_valid"
REASON_WINDOW_EXPIRED = "window_expired"
REASON_HALL_OUT_OF_SCOPE = "hall_out_of_scope"
REASON_ACTION_MISMATCH = "action_mismatch"
REASON_ANCESTOR_INACTIVE = "ancestor_inactive"

_STATE_REASONS = {
    "revoked": REASON_GRANT_REVOKED,
    "cancelled": REASON_GRANT_CANCELLED,
    "expired": REASON_GRANT_EXPIRED,
}


class DutyRosterService:
    """值守班次与范围授权。

    授权始终落库，判定同时校验状态列与时间窗（惰性过期），
    不依赖任何内存状态，重启后不会扩大任何人的可见范围。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_temple_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = TempleRepository(self.connection)

    # ------------------------------------------------------------------ 班次

    def create_shift(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        starts_at = self._parse_time(payload["starts_at"], "班次开始时间")
        ends_at = self._parse_time(payload["ends_at"], "班次结束时间")
        if ends_at <= starts_at:
            raise ValidationError("班次结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO duty_shifts(temple_id,code,name,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (temple["id"], payload["code"], payload["name"], starts_at, ends_at, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("班次编码已存在") from exc
            return self.shift_detail(cursor.lastrowid, connection)

    def list_shifts(self, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if temple_code:
            clauses.append("t.code=?")
            params.append(temple_code)
        if state:
            clauses.append("s.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT s.*,t.code AS temple_code FROM duty_shifts s JOIN temple_sites t ON t.id=s.temple_id" + where + " ORDER BY s.starts_at,s.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def shift_detail(self, shift_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT s.*,t.code AS temple_code FROM duty_shifts s JOIN temple_sites t ON t.id=s.temple_id WHERE s.id=?",
            (shift_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("值守班次不存在")
        result = dict(row)
        result["grant_count"] = int(
            connection.execute("SELECT COUNT(*) FROM scoped_grants WHERE shift_id=?", (shift_id,)).fetchone()[0]
        )
        return result

    def cancel_shift(self, shift_id: int, actor: str, reason: str) -> dict[str, Any]:
        shift = self._shift(shift_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE duty_shifts SET state='cancelled',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=? AND state='scheduled'",
                (now, reason, now, shift_id),
            )
            if cursor.rowcount == 0:
                # 并发或重复取消：班次已是终态，直接返回当前状态，保持幂等。
                return self.shift_detail(shift_id, connection)
            revoked = self._cascade_shift_cancel(connection, shift, actor, now)
            detail = self.shift_detail(shift_id, connection)
            detail["cancelled_grant_ids"] = revoked
            return detail

    # ------------------------------------------------------------------ 授权

    def create_grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        temple = self._temple(payload["temple_code"])
        shift = self._shift_by_code(payload["shift_code"])
        if shift["temple_id"] != temple["id"]:
            raise ValidationError("班次不属于目标寺院")
        hall_id = self._hall_id(temple["id"], payload.get("hall_code"))
        valid_from = self._parse_time(payload["valid_from"], "授权生效时间")
        valid_until = self._parse_time(payload["valid_until"], "授权失效时间")
        self._check_window(valid_from, valid_until, shift["starts_at"], shift["ends_at"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            # 先按审批号查重：幂等重试即使发生在班次取消后也返回原记录。
            existing = connection.execute(
                "SELECT * FROM scoped_grants WHERE source_approval_id=?", (payload["source_approval_id"],)
            ).fetchone()
            if existing is not None:
                self._check_duplicate(existing, payload, temple["id"], hall_id, shift["id"], valid_from, valid_until)
                return self.grant_detail(existing["id"], connection)
            # 新授权要求班次仍可排班；在写事务内复查，与班次取消串行化。
            shift_state = connection.execute("SELECT state FROM duty_shifts WHERE id=?", (shift["id"],)).fetchone()
            if shift_state is None or shift_state["state"] != "scheduled":
                raise ConflictError("班次已取消，不能登记新的授权")
            try:
                cursor = connection.execute(
                    "INSERT INTO scoped_grants(grant_code,subject_hash,temple_id,hall_id,action,shift_id,delegation_depth,valid_from,valid_until,source_approval_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload["grant_code"], payload["subject_hash"], temple["id"], hall_id, payload["action"],
                        shift["id"], payload.get("delegation_depth", 0), valid_from, valid_until,
                        payload["source_approval_id"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("授权编码已存在") from exc
            self._event(connection, cursor.lastrowid, "created", payload["actor"], {"shift_code": shift["code"], "source": "direct"}, now)
            return self.grant_detail(cursor.lastrowid, connection)

    def delegate_grant(self, parent_grant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        parent = self._grant(parent_grant_id)
        if payload["delegator_hash"] != parent["subject_hash"]:
            raise PermissionDeniedError("委派人不持有被引用的授权")
        if parent["delegation_depth"] < 1:
            raise ConflictError("该授权不允许继续转授")
        requested_depth = payload.get("delegation_depth", 0)
        if requested_depth > parent["delegation_depth"] - 1:
            raise ValidationError("转授深度超过委派人剩余可转授层数")
        hall_id = self._delegated_hall(parent, payload.get("hall_code"))
        action = payload.get("action") or parent["action"]
        if parent["action"] != "*" and action != parent["action"]:
            raise ValidationError("委派动作不能超出原授权范围")
        valid_from = self._parse_time(payload["valid_from"], "委派生效时间") if payload.get("valid_from") else parent["valid_from"]
        valid_until = self._parse_time(payload["valid_until"], "委派失效时间") if payload.get("valid_until") else parent["valid_until"]
        self._check_window(valid_from, valid_until, parent["valid_from"], parent["valid_until"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            # 先按审批号查重：幂等重试即使发生在原授权收回后也返回原记录。
            existing = connection.execute(
                "SELECT * FROM scoped_grants WHERE source_approval_id=?", (payload["source_approval_id"],)
            ).fetchone()
            if existing is not None:
                self._check_duplicate(
                    existing, {**payload, "action": action}, parent["temple_id"], hall_id, parent["shift_id"],
                    valid_from, valid_until, action=action, parent_grant_id=parent_grant_id,
                    delegation_depth=requested_depth,
                )
                return self.grant_detail(existing["id"], connection)
            # 新委派要求原授权与班次仍可委派；在写事务内复查，与并发撤回、班次取消串行化。
            parent_state = connection.execute("SELECT state,valid_until FROM scoped_grants WHERE id=?", (parent_grant_id,)).fetchone()
            if parent_state is None or parent_state["state"] != "active":
                raise ConflictError("原授权已失效，不能继续委派")
            # 允许提前排班委派（原授权可能尚未生效），但已过失效时间的授权不能再委派。
            if parent_state["valid_until"] <= now:
                raise ConflictError("原授权已过失效时间，不能继续委派")
            shift_state = connection.execute("SELECT state FROM duty_shifts WHERE id=?", (parent["shift_id"],)).fetchone()
            if shift_state is None or shift_state["state"] != "scheduled":
                raise ConflictError("原授权所属班次已取消，不能委派")
            try:
                cursor = connection.execute(
                    "INSERT INTO scoped_grants(grant_code,subject_hash,temple_id,hall_id,action,shift_id,parent_grant_id,delegator_hash,delegation_depth,valid_from,valid_until,source_approval_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload["grant_code"], payload["subject_hash"], parent["temple_id"], hall_id, action,
                        parent["shift_id"], parent_grant_id, payload["delegator_hash"], requested_depth,
                        valid_from, valid_until, payload["source_approval_id"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("授权编码已存在") from exc
            self._event(
                connection, cursor.lastrowid, "delegated", payload["actor"],
                {"parent_grant_id": parent_grant_id, "parent_grant_code": parent["grant_code"], "delegator_hash": payload["delegator_hash"]},
                now,
            )
            return self.grant_detail(cursor.lastrowid, connection)

    def revoke_grant(self, grant_id: int, actor: str, reason: str) -> dict[str, Any]:
        self._grant(grant_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            affected = self._revoke_tree(connection, grant_id, actor, reason, now)
            detail = self.grant_detail(grant_id, connection)
            detail["cascade_revoked_grant_ids"] = affected[1:]
            return detail

    def offboard_subject(self, payload: dict[str, Any]) -> dict[str, Any]:
        subject_hash = payload["subject_hash"]
        temple_id = self._temple(payload["temple_code"])["id"] if payload.get("temple_code") else None
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            sql = "SELECT id FROM scoped_grants WHERE subject_hash=? AND state='active'"
            params: list[Any] = [subject_hash]
            if temple_id is not None:
                sql += " AND temple_id=?"
                params.append(temple_id)
            root_ids = [int(row[0]) for row in connection.execute(sql + " ORDER BY id", params).fetchall()]
            revoked: list[int] = []
            for grant_id in root_ids:
                revoked.extend(
                    self._revoke_tree(
                        connection, grant_id, payload["actor"], "subject_offboarded", now,
                        cascade_reason="subject_offboarded", detail={"note": payload["reason"]},
                    )
                )
            return {"subject_hash": subject_hash, "revoked_grant_ids": revoked, "revoked": len(revoked)}

    def list_grants(self, subject_hash: str | None = None, temple_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject_hash:
            clauses.append("g.subject_hash=?")
            params.append(subject_hash)
        if temple_code:
            clauses.append("t.code=?")
            params.append(temple_code)
        if state:
            clauses.append("g.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT g.*,t.code AS temple_code,h.code AS hall_code,s.code AS shift_code FROM scoped_grants g "
            "JOIN temple_sites t ON t.id=g.temple_id LEFT JOIN worship_halls h ON h.id=g.hall_id JOIN duty_shifts s ON s.id=g.shift_id"
            + where + " ORDER BY g.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def grant_detail(self, grant_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT g.*,t.code AS temple_code,h.code AS hall_code,s.code AS shift_code FROM scoped_grants g "
            "JOIN temple_sites t ON t.id=g.temple_id LEFT JOIN worship_halls h ON h.id=g.hall_id JOIN duty_shifts s ON s.id=g.shift_id WHERE g.id=?",
            (grant_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("范围授权不存在")
        result = dict(row)
        result["events"] = self._events(connection, grant_id)
        result["chain"] = self._chain(connection, result)
        return result

    def expire_due_grants(self, actor: str = "scoped-grant-reaper") -> dict[str, Any]:
        """把已过失效时间的授权翻为 expired；判定本身不依赖此动作，仅用于状态整洁与审计。"""
        now = to_storage(self.clock.now())
        rows = self.connection.execute("SELECT id FROM scoped_grants WHERE state='active' AND valid_until<=? ORDER BY id", (now,)).fetchall()
        expired: list[int] = []
        for row in rows:
            with transaction(immediate=True) as connection:
                cursor = connection.execute(
                    "UPDATE scoped_grants SET state='expired',revoked_at=?,revoke_reason='expired',updated_at=? WHERE id=? AND state='active'",
                    (now, now, row["id"]),
                )
                if cursor.rowcount == 0:
                    continue
                self._event(connection, row["id"], "expired", actor, {}, now)
                expired.append(int(row["id"]))
        return {"expired": expired}

    # ------------------------------------------------------------------ 判定

    def find_allowing_grant(
        self,
        subject_hash: str,
        temple_id: int,
        hall_id: int | None,
        action: str,
        at: str,
    ) -> sqlite3.Row | None:
        """返回本次放行所依据的授权；与 explain 的选择口径完全一致。"""
        grants = self._candidate_grants(subject_hash, temple_id)
        if not grants:
            return None
        shifts = self._shifts_by_id({grant["shift_id"] for grant in grants})
        allowing = [
            grant for grant in grants
            if self._evaluate(self.connection, grant, hall_id=hall_id, action=action, at=at, shifts=shifts)[0]
        ]
        if not allowing:
            return None
        allowing.sort(key=lambda grant: (grant["valid_until"], grant["id"]), reverse=True)
        return allowing[0]

    def is_allowed(self, subject_hash: str, temple_id: int, hall_id: int | None, action: str, at: str) -> bool:
        """供业务操作（如通风处置）使用的布尔判定，语义与 explain 完全一致。"""
        return self.find_allowing_grant(subject_hash, temple_id, hall_id, action, at) is not None

    def explain(self, payload: dict[str, Any]) -> dict[str, Any]:
        """解释某次允许或拒绝的权限来源：每个候选授权给出判定、原因与委派链。"""
        temple = self._temple(payload["temple_code"])
        hall_id = self._hall_id(temple["id"], payload.get("hall_code"))
        at = self._parse_time(payload["at"], "判定时间") if payload.get("at") else to_storage(self.clock.now())
        grants = self._candidate_grants(payload["subject_hash"], temple["id"])
        shifts = self._shifts_by_id({grant["shift_id"] for grant in grants})
        candidates: list[dict[str, Any]] = []
        for grant in grants:
            ok, reason = self._evaluate(self.connection, grant, hall_id=hall_id, action=payload["action"], at=at, shifts=shifts)
            candidates.append({
                "grant_id": grant["id"],
                "grant_code": grant["grant_code"],
                "state": grant["state"],
                "decision": "allow" if ok else "deny",
                "reason": reason,
                "chain": self._chain(self.connection, grant),
            })
        selected = self.find_allowing_grant(payload["subject_hash"], temple["id"], hall_id, payload["action"], at)
        if not grants:
            reason = REASON_NO_GRANTS
        elif selected is not None:
            reason = REASON_OK
        else:
            reason = REASON_NO_EFFECTIVE_GRANT
        return {
            "allowed": selected is not None,
            "reason": reason,
            "subject_hash": payload["subject_hash"],
            "temple_code": temple["code"],
            "hall_code": payload.get("hall_code"),
            "action": payload["action"],
            "at": at,
            "selected_grant_id": selected["id"] if selected else None,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------ 内部

    def _evaluate(
        self,
        connection: sqlite3.Connection,
        grant: sqlite3.Row,
        *,
        hall_id: int | None,
        action: str,
        at: str,
        shifts: dict[int, sqlite3.Row],
    ) -> tuple[bool, str]:
        # 班次取消先于授权自身状态判定：能准确解释“班次取消导致授权失效”。
        shift = shifts.get(grant["shift_id"])
        if shift is not None and shift["state"] == "cancelled":
            return False, REASON_SHIFT_CANCELLED
        state_reason = _STATE_REASONS.get(grant["state"])
        if state_reason is not None:
            return False, state_reason
        if grant["valid_from"] > at:
            return False, REASON_NOT_YET_VALID
        if grant["valid_until"] <= at:
            return False, REASON_WINDOW_EXPIRED
        if grant["hall_id"] is not None and grant["hall_id"] != hall_id:
            return False, REASON_HALL_OUT_OF_SCOPE
        if grant["action"] != "*" and grant["action"] != action:
            return False, REASON_ACTION_MISMATCH
        # 防御性校验委派链：即使级联失效漏写，祖先失效时后代也不能放行。
        ancestor = grant
        seen = {grant["id"]}
        while ancestor["parent_grant_id"] is not None and ancestor["parent_grant_id"] not in seen:
            parent = connection.execute("SELECT * FROM scoped_grants WHERE id=?", (ancestor["parent_grant_id"],)).fetchone()
            if parent is None:
                return False, REASON_ANCESTOR_INACTIVE
            seen.add(parent["id"])
            if parent["state"] != "active" or not (parent["valid_from"] <= at < parent["valid_until"]):
                return False, REASON_ANCESTOR_INACTIVE
            ancestor = parent
        return True, REASON_OK

    def _candidate_grants(self, subject_hash: str, temple_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM scoped_grants WHERE subject_hash=? AND temple_id=? ORDER BY id",
            (subject_hash, temple_id),
        ).fetchall()

    def _shifts_by_id(self, shift_ids: set[int]) -> dict[int, sqlite3.Row]:
        if not shift_ids:
            return {}
        placeholders = ",".join("?" for _ in shift_ids)
        rows = self.connection.execute(f"SELECT * FROM duty_shifts WHERE id IN ({placeholders})", tuple(sorted(shift_ids))).fetchall()
        return {int(row["id"]): row for row in rows}

    def _chain(self, connection: sqlite3.Connection, grant: dict[str, Any] | sqlite3.Row) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        current: sqlite3.Row | None = connection.execute("SELECT * FROM scoped_grants WHERE id=?", (grant["id"],)).fetchone()
        seen: set[int] = set()
        while current is not None and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(
                {
                    "grant_id": current["id"],
                    "grant_code": current["grant_code"],
                    "subject_hash": current["subject_hash"],
                    "delegator_hash": current["delegator_hash"],
                    "action": current["action"],
                    "delegation_depth": current["delegation_depth"],
                    "state": current["state"],
                    "source_approval_id": current["source_approval_id"],
                    "source": "delegation" if current["parent_grant_id"] is not None else "direct",
                }
            )
            if current["parent_grant_id"] is None:
                break
            current = connection.execute("SELECT * FROM scoped_grants WHERE id=?", (current["parent_grant_id"],)).fetchone()
        chain.reverse()
        return chain

    def _revoke_tree(
        self,
        connection: sqlite3.Connection,
        grant_id: int,
        actor: str,
        reason: str,
        now: str,
        *,
        cascade_reason: str = "parent_revoked",
        detail: dict[str, Any] | None = None,
    ) -> list[int]:
        """条件更新实现并发安全的幂等撤回，并级联撤回全部后代授权。"""
        cursor = connection.execute(
            "UPDATE scoped_grants SET state='revoked',revoked_at=?,revoke_reason=?,updated_at=? WHERE id=? AND state='active'",
            (now, reason, now, grant_id),
        )
        if cursor.rowcount == 0:
            return []
        self._event(connection, grant_id, "revoked", actor, {"reason": reason, **(detail or {})}, now)
        descendants = connection.execute(
            "WITH RECURSIVE descendants(id) AS ("
            "SELECT id FROM scoped_grants WHERE parent_grant_id=? "
            "UNION "
            "SELECT g.id FROM scoped_grants g JOIN descendants d ON g.parent_grant_id=d.id"
            ") SELECT id FROM descendants",
            (grant_id,),
        ).fetchall()
        affected = [grant_id]
        for row in descendants:
            descendant_id = int(row["id"])
            cursor = connection.execute(
                "UPDATE scoped_grants SET state='revoked',revoked_at=?,revoke_reason=?,updated_at=? WHERE id=? AND state='active'",
                (now, cascade_reason, now, descendant_id),
            )
            if cursor.rowcount == 0:
                continue
            self._event(connection, descendant_id, "cascade_revoked", actor, {"reason": cascade_reason, "root_grant_id": grant_id}, now)
            affected.append(descendant_id)
        return affected

    @staticmethod
    def _cascade_shift_cancel(connection: sqlite3.Connection, shift: sqlite3.Row, actor: str, now: str) -> list[int]:
        rows = connection.execute("SELECT id FROM scoped_grants WHERE shift_id=? AND state='active' ORDER BY id", (shift["id"],)).fetchall()
        cancelled: list[int] = []
        for row in rows:
            cursor = connection.execute(
                "UPDATE scoped_grants SET state='cancelled',revoked_at=?,revoke_reason='shift_cancelled',updated_at=? WHERE id=? AND state='active'",
                (now, now, row["id"]),
            )
            if cursor.rowcount == 0:
                continue
            DutyRosterService._event(
                connection, row["id"], "cancelled", actor,
                {"reason": "shift_cancelled", "shift_code": shift["code"]}, now,
            )
            cancelled.append(int(row["id"]))
        return cancelled

    def _delegated_hall(self, parent: sqlite3.Row, hall_code: str | None) -> int | None:
        if parent["hall_id"] is not None:
            if hall_code is None:
                return int(parent["hall_id"])
            hall = self.repository.hall_by_code(parent["temple_id"], hall_code)
            if hall is None:
                raise NotFoundError("寺院殿堂不存在")
            if int(hall["id"]) != int(parent["hall_id"]):
                raise ValidationError("委派殿堂不能超出原授权范围")
            return int(parent["hall_id"])
        return self._hall_id(parent["temple_id"], hall_code)

    @staticmethod
    def _check_window(valid_from: str, valid_until: str, bound_from: str, bound_until: str) -> None:
        if valid_until <= valid_from:
            raise ValidationError("授权失效时间必须晚于生效时间")
        if valid_from < bound_from or valid_until > bound_until:
            raise ValidationError("授权时间窗不能超出所属班次或原授权的时间范围")

    @staticmethod
    def _check_duplicate(
        existing: sqlite3.Row,
        payload: dict[str, Any],
        temple_id: int,
        hall_id: int | None,
        shift_id: int,
        valid_from: str,
        valid_until: str,
        *,
        action: str | None = None,
        parent_grant_id: int | None = None,
        delegation_depth: int | None = None,
    ) -> None:
        same = (
            existing["grant_code"] == payload["grant_code"]
            and existing["subject_hash"] == payload["subject_hash"]
            and int(existing["temple_id"]) == temple_id
            and existing["hall_id"] == hall_id
            and existing["action"] == (action if action is not None else payload["action"])
            and int(existing["shift_id"]) == shift_id
            and existing["valid_from"] == valid_from
            and existing["valid_until"] == valid_until
            and (parent_grant_id is None or existing["parent_grant_id"] == parent_grant_id)
            and (delegation_depth is None or int(existing["delegation_depth"]) == delegation_depth)
        )
        if not same:
            raise ConflictError("相同 source_approval_id 对应了不同授权内容")

    def _temple(self, code: str) -> sqlite3.Row:
        row = self.repository.temple_by_code(code)
        if row is None:
            raise NotFoundError("寺院不存在")
        return row

    def _shift(self, shift_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM duty_shifts WHERE id=?", (shift_id,)).fetchone()
        if row is None:
            raise NotFoundError("值守班次不存在")
        return row

    def _shift_by_code(self, code: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM duty_shifts WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("值守班次不存在")
        return row

    def _grant(self, grant_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM scoped_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise NotFoundError("范围授权不存在")
        return row

    def _hall_id(self, temple_id: int, hall_code: str | None) -> int | None:
        if hall_code is None:
            return None
        hall = self.repository.hall_by_code(temple_id, hall_code)
        if hall is None:
            raise NotFoundError("寺院殿堂不存在")
        return int(hall["id"])

    @staticmethod
    def _parse_time(value: str, label: str) -> str:
        try:
            return to_storage(from_storage(value))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    @staticmethod
    def _event(connection: sqlite3.Connection, grant_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO scoped_grant_events(grant_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (grant_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, grant_id: int) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM scoped_grant_events WHERE grant_id=? ORDER BY id", (grant_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
