from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import close_connection, get_connection
from app.temple.duty import (
    REASON_ACTION_MISMATCH,
    REASON_GRANT_EXPIRED,
    REASON_GRANT_REVOKED,
    REASON_HALL_OUT_OF_SCOPE,
    REASON_NOT_YET_VALID,
    REASON_NO_GRANTS,
    REASON_OK,
    REASON_SHIFT_CANCELLED,
    REASON_WINDOW_EXPIRED,
    DutyRosterService,
)
from app.temple.rules import DEFAULT_RULES
from app.temple.service import TempleSafetyService

TEMPLE = "lingyun-temple"


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def prepare_site(client) -> None:
    client.post(
        "/api/temple/temples",
        json={
            "code": TEMPLE,
            "name": "凌云古寺",
            "temple_type": "heritage",
            "timezone": "Asia/Shanghai",
            "max_concurrent_mitigation_sessions": 10,
            "ventilation_capacity": 3000,
        },
    )
    for order, (code, name) in enumerate((("east", "东配殿"), ("west", "西配殿")), start=1):
        client.post(
            f"/api/temple/temples/{TEMPLE}/halls",
            json={"code": code, "name": name, "visit_order": order, "expected_visit_seconds": 900, "ventilation_capacity": 1200},
        )


def make_shift(client, code="mid-autumn-eve", start="2026-10-01T16:00:00Z", end="2026-10-02T00:00:00Z", **overrides):
    payload = {
        "temple_code": TEMPLE,
        "code": code,
        "name": "中秋跨夜值守",
        "starts_at": start,
        "ends_at": end,
        "actor": "abbot",
    }
    payload.update(overrides)
    return client.post("/api/temple/duty/shifts", json=payload)


def make_grant(client, *, code="grant-root", subject="volunteer-aaaa", shift="mid-autumn-eve",
               hall="east", action="mitigation.start", start="2026-10-01T16:00:00Z",
               end="2026-10-02T00:00:00Z", approval="approval-root-1", depth=0, actor="abbot"):
    payload = {
        "grant_code": code,
        "subject_hash": subject,
        "temple_code": TEMPLE,
        "hall_code": hall,
        "action": action,
        "shift_code": shift,
        "valid_from": start,
        "valid_until": end,
        "source_approval_id": approval,
        "delegation_depth": depth,
        "actor": actor,
    }
    return client.post("/api/temple/duty/grants", json=payload)


def explain(client, *, subject="volunteer-aaaa", hall="east", action="mitigation.start", at="2026-10-01T20:00:00Z"):
    payload = {"subject_hash": subject, "temple_code": TEMPLE, "hall_code": hall, "action": action, "at": at}
    response = client.post("/api/temple/duty/grants/explain", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _root_with_delegation(client, depth=1):
    prepare_site(client)
    assert make_shift(client).status_code == 201
    created = make_grant(client, depth=depth)
    assert created.status_code == 201, created.text
    return created.json()


# --------------------------------------------------------------------- 班次

def test_shift_window_and_lifecycle(client):
    prepare_site(client)
    created = make_shift(client)
    assert created.status_code == 201, created.text
    assert created.json()["state"] == "scheduled"
    bad_window = make_shift(client, code="bad-window", start="2026-10-02T00:00:00Z", end="2026-10-01T16:00:00Z")
    assert bad_window.status_code == 422
    duplicate = make_shift(client)
    assert duplicate.status_code == 409
    shift_id = created.json()["id"]
    cancelled = client.post(f"/api/temple/duty/shifts/{shift_id}/cancel", json={"actor": "abbot", "reason": "节庆活动取消"})
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    again = client.post(f"/api/temple/duty/shifts/{shift_id}/cancel", json={"actor": "abbot", "reason": "节庆活动取消"})
    assert again.status_code == 200
    assert again.json()["state"] == "cancelled"


def test_grant_window_must_fit_shift_and_shift_must_belong_to_temple(client):
    prepare_site(client)
    make_shift(client)
    early = make_grant(client, code="grant-early", start="2026-10-01T15:59:00Z", approval="approval-out-1")
    assert early.status_code == 422
    late = make_grant(client, code="grant-late", end="2026-10-02T00:01:00Z", approval="approval-out-2")
    assert late.status_code == 422
    inverted = make_grant(client, code="grant-inverted", start="2026-10-01T20:00:00Z", end="2026-10-01T19:00:00Z", approval="approval-out-3")
    assert inverted.status_code == 422
    client.post(
        "/api/temple/temples",
        json={"code": "other-temple", "name": "其他寺院", "temple_type": "urban", "max_concurrent_mitigation_sessions": 5, "ventilation_capacity": 500},
    )
    foreign = client.post(
        "/api/temple/duty/grants",
        json={
            "grant_code": "grant-foreign", "subject_hash": "volunteer-bbbb", "temple_code": "other-temple",
            "hall_code": None, "action": "mitigation.start", "shift_code": "mid-autumn-eve",
            "valid_from": "2026-10-01T16:00:00Z", "valid_until": "2026-10-02T00:00:00Z",
            "source_approval_id": "approval-foreign-1", "actor": "abbot",
        },
    )
    assert foreign.status_code == 422


# ------------------------------------------------------------- 跨午夜与范围

def test_cross_midnight_shift_allows_in_local_small_hours(client):
    prepare_site(client)
    make_shift(client)
    assert make_grant(client).status_code == 201
    # 20:00Z 对应本地次日 04:00，跨午夜后仍在班次窗口内
    decision = explain(client, at="2026-10-01T20:00:00Z")
    assert decision["allowed"] is True
    assert decision["reason"] == REASON_OK
    assert decision["selected_grant_id"] is not None
    before = explain(client, at="2026-10-01T15:59:00Z")
    assert before["allowed"] is False
    assert {item["reason"] for item in before["candidates"]} == {REASON_NOT_YET_VALID}
    # valid_until 为排他边界
    ended = explain(client, at="2026-10-02T00:00:00Z")
    assert ended["allowed"] is False
    assert {item["reason"] for item in ended["candidates"]} == {REASON_WINDOW_EXPIRED}


def test_scope_hall_and_action_are_enforced(client):
    prepare_site(client)
    make_shift(client)
    assert make_grant(client).status_code == 201
    other_hall = explain(client, hall="west")
    assert other_hall["allowed"] is False
    assert {item["reason"] for item in other_hall["candidates"]} == {REASON_HALL_OUT_OF_SCOPE}
    other_action = explain(client, action="observation.ingest")
    assert other_action["allowed"] is False
    assert {item["reason"] for item in other_action["candidates"]} == {REASON_ACTION_MISMATCH}
    # 寺院级（不限定殿堂）+ 通配动作的授权覆盖任意殿堂与动作
    wide = make_grant(client, code="grant-wide", subject="volunteer-wide", hall=None, action="*", approval="approval-wide-1")
    assert wide.status_code == 201
    decision = explain(client, subject="volunteer-wide", hall="west", action="observation.ingest", at="2026-10-01T18:00:00Z")
    assert decision["allowed"] is True


def test_overlapping_grants_pick_latest_valid_until(client):
    prepare_site(client)
    make_shift(client)
    assert make_grant(client, code="grant-short", end="2026-10-01T18:00:00Z", approval="approval-short-1").status_code == 201
    assert make_grant(client, code="grant-long", end="2026-10-01T22:00:00Z", approval="approval-long-1").status_code == 201
    decision = explain(client, at="2026-10-01T19:00:00Z")
    assert decision["allowed"] is True
    selected = next(item for item in decision["candidates"] if item["grant_id"] == decision["selected_grant_id"])
    assert selected["grant_code"] == "grant-long"
    # 多条候选都会给出各自判定与原因，便于解释重叠授权
    assert len(decision["candidates"]) == 2


def test_explain_reports_chain_and_no_grants(client):
    prepare_site(client)
    denied = explain(client)
    assert denied["allowed"] is False
    assert denied["reason"] == REASON_NO_GRANTS
    assert denied["candidates"] == []
    make_shift(client)
    assert make_grant(client).status_code == 201
    decision = explain(client)
    chain = decision["candidates"][0]["chain"]
    assert chain[0]["source"] == "direct"
    assert chain[0]["grant_code"] == "grant-root"


# ----------------------------------------------------------------- 委派链

def test_delegation_requires_holder_permission_and_narrows_scope(client):
    root = _root_with_delegation(client)
    delegate_payload = {
        "grant_code": "grant-child",
        "subject_hash": "external-expert-1",
        "delegator_hash": "volunteer-aaaa",
        "hall_code": "east",
        "valid_from": "2026-10-01T17:00:00Z",
        "valid_until": "2026-10-01T19:00:00Z",
        "source_approval_id": "approval-child-1",
        "delegation_depth": 0,
        "actor": "volunteer-aaaa",
    }
    created = client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json=delegate_payload)
    assert created.status_code == 201, created.text
    assert created.json()["parent_grant_id"] == root["id"]
    assert created.json()["delegator_hash"] == "volunteer-aaaa"
    assert [item["source"] for item in created.json()["chain"]] == ["direct", "delegation"]

    def attempt(**overrides):
        payload = {**delegate_payload, **overrides}
        return client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json=payload)

    fraud = attempt(grant_code="grant-fraud", source_approval_id="approval-fraud-1", delegator_hash="someone-else")
    assert fraud.status_code == 403
    too_deep = attempt(grant_code="grant-deep", source_approval_id="approval-deep-1", delegation_depth=1)
    assert too_deep.status_code == 422
    wider = attempt(grant_code="grant-wider", source_approval_id="approval-wider-1", valid_from="2026-10-01T15:00:00Z")
    assert wider.status_code == 422
    other_action = attempt(grant_code="grant-other-action", source_approval_id="approval-action-1", action="observation.ingest")
    assert other_action.status_code == 422
    other_hall = attempt(grant_code="grant-west", source_approval_id="approval-west-1", hall_code="west")
    assert other_hall.status_code == 422
    # 深度为 0 的授权不能继续转授
    child_id = created.json()["id"]
    no_more = client.post(
        f"/api/temple/duty/grants/{child_id}/delegate",
        json={
            "grant_code": "grant-grand", "subject_hash": "temp-volunteer", "delegator_hash": "external-expert-1",
            "source_approval_id": "approval-grand-1", "delegation_depth": 0, "actor": "external-expert-1",
        },
    )
    assert no_more.status_code == 409


def test_delegated_grant_allows_within_narrowed_window(client):
    root = _root_with_delegation(client)
    created = client.post(
        f"/api/temple/duty/grants/{root['id']}/delegate",
        json={
            "grant_code": "grant-child", "subject_hash": "external-expert-1",
            "delegator_hash": "volunteer-aaaa", "hall_code": "east",
            "valid_from": "2026-10-01T17:00:00Z", "valid_until": "2026-10-01T19:00:00Z",
            "source_approval_id": "approval-child-1", "delegation_depth": 0, "actor": "volunteer-aaaa",
        },
    )
    assert created.status_code == 201
    assert explain(client, subject="external-expert-1", at="2026-10-01T18:00:00Z")["allowed"] is True
    # 超出收窄后的窗口即拒绝，即使原授权仍有效
    outside = explain(client, subject="external-expert-1", at="2026-10-01T19:30:00Z")
    assert outside["allowed"] is False
    assert {item["reason"] for item in outside["candidates"]} == {REASON_WINDOW_EXPIRED}


def test_duplicate_delegation_is_idempotent_but_content_change_conflicts(client):
    root = _root_with_delegation(client)
    payload = {
        "grant_code": "grant-child", "subject_hash": "external-expert-1",
        "delegator_hash": "volunteer-aaaa", "hall_code": "east",
        "source_approval_id": "approval-child-1", "delegation_depth": 0, "actor": "volunteer-aaaa",
    }
    first = client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json=payload)
    second = client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    conflict = client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json={**payload, "subject_hash": "external-expert-2"})
    assert conflict.status_code == 409
    # 原授权收回后，相同审批号的幂等重试仍返回原记录，而不是报错
    revoked = client.post(f"/api/temple/duty/grants/{root['id']}/revoke", json={"actor": "abbot", "reason": "原授权收回"})
    assert revoked.status_code == 200
    replay = client.post(f"/api/temple/duty/grants/{root['id']}/delegate", json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["state"] == "revoked"


# --------------------------------------------------------- 收回、取消与离岗

def test_revoking_parent_grant_cascades_to_descendants(client):
    root = _root_with_delegation(client, depth=2)
    child = client.post(
        f"/api/temple/duty/grants/{root['id']}/delegate",
        json={
            "grant_code": "grant-child", "subject_hash": "external-expert-1",
            "delegator_hash": "volunteer-aaaa", "hall_code": "east",
            "source_approval_id": "approval-child-1", "delegation_depth": 1, "actor": "volunteer-aaaa",
        },
    ).json()
    grand = client.post(
        f"/api/temple/duty/grants/{child['id']}/delegate",
        json={
            "grant_code": "grant-grand", "subject_hash": "temp-volunteer",
            "delegator_hash": "external-expert-1", "hall_code": "east",
            "source_approval_id": "approval-grand-1", "delegation_depth": 0, "actor": "external-expert-1",
        },
    ).json()
    revoked = client.post(f"/api/temple/duty/grants/{root['id']}/revoke", json={"actor": "abbot", "reason": "原授权收回"})
    assert revoked.status_code == 200
    assert set(revoked.json()["cascade_revoked_grant_ids"]) == {child["id"], grand["id"]}
    child_decision = explain(client, subject="external-expert-1", at="2026-10-01T18:00:00Z")
    assert child_decision["allowed"] is False
    assert {item["reason"] for item in child_decision["candidates"]} == {REASON_GRANT_REVOKED}
    grand_decision = explain(client, subject="temp-volunteer", at="2026-10-01T18:00:00Z")
    assert grand_decision["allowed"] is False
    # 已失效授权不能继续委派
    retry = client.post(
        f"/api/temple/duty/grants/{child['id']}/delegate",
        json={
            "grant_code": "grant-again", "subject_hash": "temp-volunteer-2",
            "delegator_hash": "external-expert-1", "source_approval_id": "approval-again-1",
            "delegation_depth": 0, "actor": "external-expert-1",
        },
    )
    assert retry.status_code == 409


def test_shift_cancel_invalidates_grants_but_keeps_audit(client):
    root = _root_with_delegation(client)
    shift_id = root["shift_id"]
    cancelled = client.post(f"/api/temple/duty/shifts/{shift_id}/cancel", json={"actor": "abbot", "reason": "活动取消"})
    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled_grant_ids"] == [root["id"]]
    decision = explain(client, at="2026-10-01T18:00:00Z")
    assert decision["allowed"] is False
    assert {item["reason"] for item in decision["candidates"]} == {REASON_SHIFT_CANCELLED}
    # 已取消班次上不能再登记授权
    rejected = make_grant(client, code="grant-after-cancel", approval="approval-after-1")
    assert rejected.status_code == 409
    # 授权生命周期事件仍然可审计
    detail = client.get(f"/api/temple/duty/grants/{root['id']}").json()
    assert [event["event_type"] for event in detail["events"]] == ["created", "cancelled"]
    assert detail["state"] == "cancelled"


def test_offboard_revokes_every_active_grant_subject_holds(client):
    prepare_site(client)
    make_shift(client)
    assert make_grant(client, code="grant-a1", subject="leaving-staff", approval="approval-a1").status_code == 201
    assert make_grant(client, code="grant-a2", subject="leaving-staff", hall=None, approval="approval-a2").status_code == 201
    assert make_grant(client, code="grant-other", subject="staying-staff", approval="approval-other").status_code == 201
    result = client.post(
        "/api/temple/duty/subjects/offboard",
        json={"subject_hash": "leaving-staff", "actor": "abbot", "reason": "值守结束离岗"},
    )
    assert result.status_code == 200
    assert result.json()["revoked"] == 2
    assert explain(client, subject="leaving-staff")["allowed"] is False
    assert explain(client, subject="staying-staff")["allowed"] is True


def test_reaper_expires_due_grants(client):
    prepare_site(client)
    make_shift(client)
    created = make_grant(client)
    assert created.status_code == 201
    grant_id = created.json()["id"]
    reaped = DutyRosterService(get_connection(), FrozenClock(datetime(2026, 10, 2, 0, 1, tzinfo=UTC))).expire_due_grants("reaper")
    assert reaped["expired"] == [grant_id]
    decision = explain(client, at="2026-10-02T00:01:00Z")
    assert decision["allowed"] is False
    assert {item["reason"] for item in decision["candidates"]} == {REASON_GRANT_EXPIRED}
    again = DutyRosterService(get_connection(), FrozenClock(datetime(2026, 10, 2, 0, 2, tzinfo=UTC))).expire_due_grants("reaper")
    assert again["expired"] == []


# --------------------------------------------------------------- 并发与重启

def test_concurrent_revoke_is_serialized_and_idempotent(client):
    root = _root_with_delegation(client, depth=2)
    client.post(
        f"/api/temple/duty/grants/{root['id']}/delegate",
        json={
            "grant_code": "grant-child", "subject_hash": "external-expert-1",
            "delegator_hash": "volunteer-aaaa", "hall_code": "east",
            "source_approval_id": "approval-child-1", "delegation_depth": 1, "actor": "volunteer-aaaa",
        },
    )
    errors: list[Exception] = []

    def revoke() -> None:
        try:
            # 每个线程使用独立连接，模拟并发请求
            close_connection()
            DutyRosterService().revoke_grant(root["id"], "abbot", "并发撤回")
        except Exception as exc:  # noqa: BLE001 - 测试需要收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=revoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    connection = get_connection()
    states = {row["id"]: row["state"] for row in connection.execute("SELECT id,state FROM scoped_grants ORDER BY id")}
    assert states == {root["id"]: "revoked", root["id"] + 1: "revoked"}
    revoke_events = connection.execute(
        "SELECT COUNT(*) FROM scoped_grant_events WHERE grant_id=? AND event_type='revoked'", (root["id"],)
    ).fetchone()[0]
    assert revoke_events == 1


def test_restart_with_new_service_instance_does_not_expand_scope(client):
    prepare_site(client)
    make_shift(client)
    created = make_grant(client, start="2026-10-01T18:00:00Z", end="2026-10-01T20:00:00Z")
    assert created.status_code == 201
    # 用全新服务实例模拟重启：判定完全依赖落库状态与时间窗，不缓存任何放行结论
    before = DutyRosterService(get_connection(), FrozenClock(datetime(2026, 10, 1, 17, 0, tzinfo=UTC)))
    assert before.is_allowed("volunteer-aaaa", 1, 1, "mitigation.start", "2026-10-01T17:30:00+00:00") is False
    inside = DutyRosterService(get_connection(), FrozenClock(datetime(2026, 10, 1, 19, 0, tzinfo=UTC)))
    assert inside.is_allowed("volunteer-aaaa", 1, 1, "mitigation.start", "2026-10-01T19:00:00+00:00") is True
    after = DutyRosterService(get_connection(), FrozenClock(datetime(2026, 10, 2, 2, 0, tzinfo=UTC)))
    assert after.is_allowed("volunteer-aaaa", 1, 1, "mitigation.start", "2026-10-01T21:00:00+00:00") is False


# ----------------------------------------------------- 与通风处置业务的衔接

def _prepare_policy_and_incident(client) -> int:
    client.post(
        "/api/temple/incense_profiles",
        json={
            "incense_code": "festival-incense", "name": "节庆香火", "activity_type": "festival",
            "pm25_target": 100, "co_target": 0.01, "min_supply_airflow": 8, "min_exhaust_airflow": 4, "default_risk_priority": 70,
        },
    )
    policy = client.post(f"/api/temple/temples/{TEMPLE}/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/temple/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"})
    observation = client.post(
        "/api/temple/observations",
        json={
            "observation_key": "observation-duty-001", "temple_code": TEMPLE, "hall_code": "east",
            "incense_code": "festival-incense", "steward_hash": "volunteer-aaaa-0001", "sensor_class": "ceiling-sensor",
            "visitor_density": 300, "pm25_ugm3": 350, "co_ppm": 0.08, "supply_airflow": 1.5, "exhaust_airflow": 0.5,
            "observed_at": "2026-09-26T09:00:00Z",
        },
    ).json()
    return observation["safety_incident_id"]


def test_scoped_grant_authorizes_mitigation_and_completion_survives_revocation(client):
    prepare_site(client)
    incident_id = _prepare_policy_and_incident(client)
    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    duty = DutyRosterService(get_connection(), FrozenClock(now))
    duty.create_shift({
        "temple_code": TEMPLE, "code": "festival-day", "name": "当日值守",
        "starts_at": _iso(now - timedelta(hours=1)), "ends_at": _iso(now + timedelta(hours=8)), "actor": "abbot",
    })
    safety = TempleSafetyService(get_connection(), FrozenClock(now))
    # 没有任何授权时启动处置被拒绝
    with pytest.raises(ConflictError):
        safety.start_mitigation(incident_id, "tests")
    # 范围授权放行处置，且事件记录授权来源
    duty.create_grant({
        "grant_code": "grant-mitigate", "subject_hash": "volunteer-aaaa-0001", "temple_code": TEMPLE,
        "hall_code": "east", "action": "mitigation.start", "shift_code": "festival-day",
        "valid_from": _iso(now - timedelta(hours=1)), "valid_until": _iso(now + timedelta(hours=2)),
        "source_approval_id": "approval-mitigate-1", "delegation_depth": 0, "actor": "abbot",
    })
    started = safety.start_mitigation(incident_id, "tests")
    assert started["status"] == "active"
    started_event = started["events"][0]
    assert started_event["detail"]["authorization"]["type"] == "scoped_grant"
    assert started_event["detail"]["authorization"]["grant_code"] == "grant-mitigate"
    # 完成处置后收回授权：已完成的操作仍可审计，新判定立即拒绝
    finished = safety.finish_mitigation_session(started["id"], "tests", "处置完成", "completed")
    assert finished["status"] == "completed"
    duty.revoke_grant(started_event["detail"]["authorization"]["grant_id"], "abbot", "值守结束")
    grants = duty.list_grants(subject_hash="volunteer-aaaa-0001")
    assert grants[0]["state"] == "revoked"
    assert duty.explain({
        "subject_hash": "volunteer-aaaa-0001", "temple_code": TEMPLE, "hall_code": "east", "action": "mitigation.start",
    })["allowed"] is False
    detail = safety.get_mitigation_session(started["id"])
    assert [event["event_type"] for event in detail["events"]] == ["started", "completed"]
