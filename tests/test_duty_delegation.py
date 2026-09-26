from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError, ValidationError
from app.database import get_connection
from app.temple.duty import DutyAuthorizationService
from app.temple.rules import DEFAULT_RULES

T0 = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
SHIFT_START = "2026-09-26T22:00:00Z"
SHIFT_END = "2026-09-27T06:00:00Z"
STEWARD_A = "steward-aaaaaaaaaaaa"
STEWARD_B = "steward-bbbbbbbbbbbb"
STEWARD_C = "steward-cccccccccccc"


def temple_payload(**overrides):
    payload = {
        "code": "lingyun-temple",
        "name": "凌云古寺",
        "temple_type": "heritage",
        "timezone": "Asia/Shanghai",
        "max_concurrent_mitigation_sessions": 10,
        "ventilation_capacity": 3000,
    }
    payload.update(overrides)
    return payload


def prepare_site(client):
    temple = client.post("/api/temple/temples", json=temple_payload())
    assert temple.status_code == 201, temple.text
    for code, order in (("main-hall", 1), ("side-hall", 2)):
        hall = client.post(
            "/api/temple/temples/lingyun-temple/halls",
            json={"code": code, "name": code, "visit_order": order, "expected_visit_seconds": 900, "ventilation_capacity": 1200},
        )
        assert hall.status_code == 201, hall.text


def prepare_operations(client):
    prepare_site(client)
    profile = client.post(
        "/api/temple/incense_profiles",
        json={
            "incense_code": "festival-incense",
            "name": "节庆香火",
            "activity_type": "festival",
            "pm25_target": 100,
            "co_target": 0.01,
            "min_supply_airflow": 8,
            "min_exhaust_airflow": 4,
            "default_risk_priority": 70,
        },
    )
    assert profile.status_code == 201, profile.text
    safety_policy = client.post("/api/temple/temples/lingyun-temple/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert safety_policy.status_code == 201, safety_policy.text
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    published = client.post(
        f"/api/temple/policies/{safety_policy.json()['id']}/publish",
        json={"actor": "tests", "effective_from": past},
    )
    assert published.status_code == 200, published.text


def observation_payload(steward_hash: str, key: str, **overrides):
    payload = {
        "observation_key": key,
        "temple_code": "lingyun-temple",
        "hall_code": "main-hall",
        "incense_code": "festival-incense",
        "steward_hash": steward_hash,
        "sensor_class": "ceiling-sensor",
        "visitor_density": 300,
        "pm25_ugm3": 350,
        "co_ppm": 0.08,
        "supply_airflow": 1.5,
        "exhaust_airflow": 0.5,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    return payload


def service_at(moment: str) -> DutyAuthorizationService:
    return DutyAuthorizationService(get_connection(), FrozenClock(datetime.fromisoformat(moment.replace("Z", "+00:00"))))


def shift_payload(**overrides):
    payload = {
        "temple_code": "lingyun-temple",
        "code": "festival-night-1",
        "name": "节庆夜值一班",
        "starts_at": SHIFT_START,
        "ends_at": SHIFT_END,
        "actor": "tests",
    }
    payload.update(overrides)
    return payload


def grant_payload(**overrides):
    payload = {
        "grant_code": "grant-root-0001",
        "shift_code": "festival-night-1",
        "subject_hash": STEWARD_A,
        "hall_code": "main-hall",
        "actions": ["mitigation.start"],
        "valid_from": SHIFT_START,
        "valid_until": SHIFT_END,
        "actor": "tests",
    }
    payload.update(overrides)
    return payload


def delegate_payload(**overrides):
    payload = {
        "grant_code": "grant-deleg-0001",
        "subject_hash": STEWARD_B,
        "hall_code": None,
        "actions": ["mitigation.start"],
        "valid_from": "2026-09-26T23:00:00Z",
        "valid_until": "2026-09-27T02:00:00Z",
        "actor": "tests",
    }
    payload.update(overrides)
    return payload


def decide(service, grant, *, subject=STEWARD_A, hall_id=None, action="mitigation.start", at):
    return service.decide(
        subject_hash=subject,
        temple_id=grant["temple_id"],
        hall_id=grant["hall_id"] if hall_id is None else hall_id,
        action=action,
        at=at,
    )


def test_cross_midnight_shift_authorizes_across_midnight(client):
    prepare_site(client)
    service = service_at("2026-09-26T20:00:00Z")
    shift = service.create_shift(shift_payload())
    assert shift["state"] == "scheduled"
    grant = service.create_grant(grant_payload())
    assert grant["delegation_depth"] == 2
    late = decide(service, grant, at="2026-09-26T23:30:00Z")
    assert late["allowed"] is True
    after_midnight = decide(service, grant, at="2026-09-27T02:00:00Z")
    assert after_midnight["allowed"] is True
    assert after_midnight["sources"][0]["shift_code"] == "festival-night-1"
    before_start = decide(service, grant, at="2026-09-26T21:00:00Z")
    assert before_start["allowed"] is False
    assert before_start["denials"][0]["reason"] == "not_yet_valid"
    morning = decide(service, grant, at="2026-09-27T07:00:00Z")
    assert morning["allowed"] is False
    assert morning["denials"][0]["reason"] == "expired"


def test_grant_window_must_stay_inside_shift(client):
    prepare_site(client)
    service = service_at("2026-09-26T20:00:00Z")
    service.create_shift(shift_payload())
    with pytest.raises(ValidationError):
        service.create_grant(grant_payload(valid_until="2026-09-27T07:00:00Z"))
    with pytest.raises(ValidationError):
        service.create_grant(grant_payload(valid_from="2026-09-26T21:00:00Z"))
    with pytest.raises(ValidationError):
        service.create_grant(grant_payload(valid_from=SHIFT_END, valid_until=SHIFT_START))
    with pytest.raises(ValidationError):
        service.create_grant(grant_payload(actions=["temple.destroy"]))


def test_duplicate_shift_code_conflicts(client):
    prepare_site(client)
    service = service_at("2026-09-26T20:00:00Z")
    service.create_shift(shift_payload())
    with pytest.raises(ConflictError):
        service.create_shift(shift_payload())


def test_delegation_narrows_scope_and_limits_depth(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    root = service.create_grant(grant_payload(hall_code=None, actions=["mitigation.start", "mitigation.finish"]))
    child = service.delegate_grant(root["id"], delegate_payload(hall_code="main-hall"))
    assert child["delegation_depth"] == 1
    assert child["parent_grant_id"] == root["id"]
    grandchild = service.delegate_grant(child["id"], delegate_payload(grant_code="grant-grand-0001", subject_hash=STEWARD_C))
    assert grandchild["delegation_depth"] == 0
    assert grandchild["hall_code"] == "main-hall"
    assert [link["grant_code"] for link in grandchild["chain"]] == ["grant-root-0001", "grant-deleg-0001", "grant-grand-0001"]
    with pytest.raises(ConflictError):
        service.delegate_grant(grandchild["id"], delegate_payload(grant_code="grant-great-0001", subject_hash=STEWARD_A))


def test_delegation_cannot_widen_scope(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    root = service.create_grant(grant_payload())
    with pytest.raises(ValidationError):
        service.delegate_grant(root["id"], delegate_payload(valid_until="2026-09-27T07:00:00Z"))
    with pytest.raises(ValidationError):
        service.delegate_grant(root["id"], delegate_payload(actions=["mitigation.start", "analytics.read"]))
    with pytest.raises(ValidationError):
        service.delegate_grant(root["id"], delegate_payload(hall_code="side-hall"))
    inherited = service.delegate_grant(root["id"], delegate_payload(hall_code=None))
    assert inherited["hall_code"] == "main-hall"


def test_delegation_requires_currently_valid_parent(client):
    prepare_site(client)
    service = service_at("2026-09-26T21:00:00Z")
    service.create_shift(shift_payload())
    root = service.create_grant(grant_payload())
    with pytest.raises(ConflictError):
        service.delegate_grant(root["id"], delegate_payload())
    later = service_at("2026-09-26T23:00:00Z")
    later.revoke_grant(root["id"], "tests")
    with pytest.raises(ConflictError):
        later.delegate_grant(root["id"], delegate_payload())


def test_delegation_depth_is_clamped(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    grant = service.create_grant(grant_payload(delegation_depth=99))
    assert grant["delegation_depth"] == 2
    zero = service.create_grant(grant_payload(grant_code="grant-zero-0001", subject_hash=STEWARD_B, delegation_depth=0))
    with pytest.raises(ConflictError):
        service.delegate_grant(zero["id"], delegate_payload())


def test_revoke_cascades_and_keeps_audit_trail(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    root = service.create_grant(grant_payload(hall_code=None, actions=["mitigation.start", "mitigation.finish"]))
    child = service.delegate_grant(root["id"], delegate_payload(hall_code="main-hall"))
    grandchild = service.delegate_grant(child["id"], delegate_payload(grant_code="grant-grand-0001", subject_hash=STEWARD_C))
    revoked = service.revoke_grant(root["id"], "tests", "节庆提前结束")
    assert revoked["state"] == "revoked"
    assert revoked["revoked_reason"] == "manual"
    for grant_id in (child["id"], grandchild["id"]):
        detail = service.grant_detail(grant_id)
        assert detail["state"] == "revoked"
        assert detail["revoked_reason"] == "parent_revoked"
        assert any(event["event_type"] == "revoked" for event in detail["events"])
    decision = decide(service, child, subject=STEWARD_B, at="2026-09-26T23:30:00Z")
    assert decision["allowed"] is False
    assert decision["denials"][0]["reason"] == "revoked"
    again = service.revoke_grant(root["id"], "tests")
    assert again["state"] == "revoked"
    assert len([event for event in service.grant_detail(root["id"])["events"] if event["event_type"] == "revoked"]) == 1


def test_shift_cancel_revokes_grants_and_offboard_is_targeted(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    shift = service.create_shift(shift_payload())
    grant_a = service.create_grant(grant_payload(grant_code="grant-a", subject_hash=STEWARD_A))
    grant_b = service.create_grant(grant_payload(grant_code="grant-b", subject_hash=STEWARD_B))
    offboarded = service.offboard_shift_member(shift["id"], STEWARD_A, "tests", "提前离岗")
    assert offboarded["revoked_grant_ids"] == [grant_a["id"]]
    assert service.grant_detail(grant_a["id"])["revoked_reason"] == "offboarded"
    assert service.grant_detail(grant_b["id"])["state"] == "active"
    cancelled = service.cancel_shift(shift["id"], "tests", "活动取消")
    assert cancelled["state"] == "cancelled"
    assert service.grant_detail(grant_b["id"])["revoked_reason"] == "shift_cancelled"
    assert service.cancel_shift(shift["id"], "tests")["state"] == "cancelled"
    with pytest.raises(ConflictError):
        service.complete_shift(shift["id"], "tests")
    with pytest.raises(ConflictError):
        service.create_grant(grant_payload(grant_code="grant-c", subject_hash=STEWARD_C))


def test_offboard_cascades_to_delegatees(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    shift = service.create_shift(shift_payload())
    root = service.create_grant(grant_payload(hall_code=None, actions=["mitigation.start", "mitigation.finish"]))
    child = service.delegate_grant(root["id"], delegate_payload(hall_code="main-hall"))
    service.offboard_shift_member(shift["id"], STEWARD_A, "tests")
    assert service.grant_detail(root["id"])["revoked_reason"] == "offboarded"
    assert service.grant_detail(child["id"])["revoked_reason"] == "parent_revoked"


def test_duplicate_grant_and_delegation_are_idempotent(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    first = service.create_grant(grant_payload())
    replay = service.create_grant(grant_payload())
    assert replay["id"] == first["id"]
    assert replay["duplicate"] is True
    with pytest.raises(ConflictError):
        service.create_grant(grant_payload(hall_code="side-hall"))
    child = service.delegate_grant(first["id"], delegate_payload())
    child_replay = service.delegate_grant(first["id"], delegate_payload())
    assert child_replay["id"] == child["id"]
    assert child_replay["duplicate"] is True
    with pytest.raises(ConflictError):
        service.delegate_grant(first["id"], delegate_payload(actions=["mitigation.start"], hall_code="side-hall"))


def test_overlapping_grants_union_sources(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    service.create_shift(shift_payload())
    service.create_shift(shift_payload(code="festival-night-2", starts_at="2026-09-26T20:00:00Z", ends_at="2026-09-27T02:00:00Z"))
    first = service.create_grant(grant_payload(grant_code="grant-overlap-1"))
    second = service.create_grant(
        grant_payload(grant_code="grant-overlap-2", shift_code="festival-night-2", valid_from="2026-09-26T20:00:00Z", valid_until="2026-09-27T02:00:00Z")
    )
    decision = decide(service, first, at="2026-09-26T23:30:00Z")
    assert decision["allowed"] is True
    assert len(decision["sources"]) == 2
    service.revoke_grant(second["id"], "tests")
    decision = decide(service, first, at="2026-09-26T23:30:00Z")
    assert decision["allowed"] is True
    assert len(decision["sources"]) == 1
    assert len(decision["denials"]) == 1
    assert decision["denials"][0]["reason"] == "revoked"


def test_concurrent_revocation_is_serialized(client):
    prepare_site(client)
    service = service_at("2026-09-26T23:00:00Z")
    shift = service.create_shift(shift_payload())
    root = service.create_grant(grant_payload(hall_code=None, actions=["mitigation.start", "mitigation.finish"]))
    child = service.delegate_grant(root["id"], delegate_payload(hall_code="main-hall"))
    errors: list[Exception] = []

    def hammer(action):
        try:
            worker = DutyAuthorizationService(get_connection(), FrozenClock(datetime(2026, 9, 26, 23, 0, tzinfo=UTC)))
            action(worker)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=hammer, args=(lambda worker: worker.revoke_grant(root["id"], "thread-1"),)),
        threading.Thread(target=hammer, args=(lambda worker: worker.revoke_grant(root["id"], "thread-2"),)),
        threading.Thread(target=hammer, args=(lambda worker: worker.cancel_shift(shift["id"], "thread-3"),)),
        threading.Thread(target=hammer, args=(lambda worker: worker.offboard_shift_member(shift["id"], STEWARD_A, "thread-4"),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    final_root = service.grant_detail(root["id"])
    assert final_root["state"] == "revoked"
    assert len([event for event in final_root["events"] if event["event_type"] == "revoked"]) == 1
    final_child = service.grant_detail(child["id"])
    assert final_child["state"] == "revoked"
    assert len([event for event in final_child["events"] if event["event_type"] == "revoked"]) == 1
    assert service.shift_detail(shift["id"])["state"] == "cancelled"


def test_restart_sweep_only_narrows_scope(client):
    prepare_site(client)
    early = service_at("2026-09-26T20:00:00Z")
    shift = early.create_shift(shift_payload())
    grant = early.create_grant(grant_payload())
    restarted = service_at("2026-09-27T08:00:00Z")
    result = restarted.sweep_expired()
    assert grant["id"] in result["expired_grants"]
    assert shift["id"] in result["completed_shifts"]
    # 即使把评估时间拨回授权有效期内，落库的终态也不会让权限复活
    decision = decide(restarted, grant, at="2026-09-26T23:00:00Z")
    assert decision["allowed"] is False
    assert decision["denials"][0]["reason"] == "expired"
    with pytest.raises(ConflictError):
        restarted.delegate_grant(grant["id"], delegate_payload())
    assert restarted.sweep_expired()["expired_grants"] == []


def test_explain_endpoint_reports_source_and_denial(client):
    prepare_site(client)
    now = datetime.now(UTC)
    starts = (now - timedelta(hours=1)).isoformat()
    ends = (now + timedelta(hours=8)).isoformat()
    shift = client.post("/api/temple/duty/shifts", json=shift_payload(starts_at=starts, ends_at=ends))
    assert shift.status_code == 201, shift.text
    grant = client.post("/api/temple/duty/grants", json=grant_payload(valid_from=starts, valid_until=ends))
    assert grant.status_code == 201, grant.text
    actions = client.get("/api/temple/duty/actions")
    assert actions.status_code == 200
    assert "mitigation.start" in actions.json()["items"]
    allowed = client.get(
        "/api/temple/duty/explain",
        params={"subject_hash": STEWARD_A, "temple_code": "lingyun-temple", "hall_code": "main-hall", "action": "mitigation.start"},
    )
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["allowed"] is True
    assert body["sources"][0]["grant_code"] == "grant-root-0001"
    assert body["sources"][0]["chain"][0]["grant_code"] == "grant-root-0001"
    assert "允许" in body["summary"]
    wrong_hall = client.get(
        "/api/temple/duty/explain",
        params={"subject_hash": STEWARD_A, "temple_code": "lingyun-temple", "hall_code": "side-hall", "action": "mitigation.start"},
    ).json()
    assert wrong_hall["allowed"] is False
    assert wrong_hall["denials"][0]["reason"] == "hall_out_of_scope"
    assert "拒绝" in wrong_hall["summary"]
    wrong_action = client.get(
        "/api/temple/duty/explain",
        params={"subject_hash": STEWARD_A, "temple_code": "lingyun-temple", "hall_code": "main-hall", "action": "analytics.read"},
    ).json()
    assert wrong_action["denials"][0]["reason"] == "action_not_granted"
    unknown_action = client.get(
        "/api/temple/duty/explain",
        params={"subject_hash": STEWARD_A, "temple_code": "lingyun-temple", "action": "temple.destroy"},
    )
    assert unknown_action.status_code == 422


def test_explain_includes_legacy_authorization_source(client):
    prepare_operations(client)
    now = datetime.now(UTC)
    authorization = client.post(
        "/api/temple/authorizations",
        json={
            "steward_hash": STEWARD_A,
            "temple_code": "lingyun-temple",
            "authorization_code": "festival-duty",
            "valid_from": (now - timedelta(hours=1)).isoformat(),
            "valid_until": (now + timedelta(hours=8)).isoformat(),
            "source_approval_id": "order-explain-0001",
        },
    )
    assert authorization.status_code == 201, authorization.text
    body = client.get(
        "/api/temple/duty/explain",
        params={"subject_hash": STEWARD_A, "temple_code": "lingyun-temple", "action": "mitigation.start"},
    ).json()
    assert body["allowed"] is True
    assert body["sources"][0]["kind"] == "steward_authorization"


def test_mitigation_uses_duty_grant_and_keeps_completed_operations_auditable(client):
    prepare_operations(client)
    now = datetime.now(UTC)
    starts = (now - timedelta(hours=1)).isoformat()
    ends = (now + timedelta(hours=8)).isoformat()
    shift = client.post("/api/temple/duty/shifts", json=shift_payload(starts_at=starts, ends_at=ends))
    assert shift.status_code == 201, shift.text
    observation = client.post("/api/temple/observations", json=observation_payload(STEWARD_A, "observation-duty-0001"))
    assert observation.status_code == 202, observation.text
    incident = observation.json()["safety_incident_id"]
    denied = client.post(f"/api/temple/safety_incidents/{incident}/mitigate", json={"actor": "tests"})
    assert denied.status_code == 409
    assert denied.json()["error"]["context"]["authorization"]["allowed"] is False
    grant = client.post("/api/temple/duty/grants", json=grant_payload(valid_from=starts, valid_until=ends))
    assert grant.status_code == 201, grant.text
    started = client.post(f"/api/temple/safety_incidents/{incident}/mitigate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    provenance = started.json()["events"][0]["detail"]["authorization"]
    assert provenance["kind"] == "duty_grant"
    assert provenance["grant_code"] == "grant-root-0001"
    finished = client.post(
        f"/api/temple/mitigation_sessions/{started.json()['id']}/finish",
        json={"actor": "tests", "reason": "处置完成", "result": "completed"},
    )
    assert finished.status_code == 200
    revoked = client.post(f"/api/temple/duty/grants/{grant.json()['id']}/revoke", json={"actor": "tests", "reason": "班次调整"})
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    second = client.post("/api/temple/observations", json=observation_payload(STEWARD_A, "observation-duty-0002"))
    denied_after_revoke = client.post(f"/api/temple/safety_incidents/{second.json()['safety_incident_id']}/mitigate", json={"actor": "tests"})
    assert denied_after_revoke.status_code == 409
    detail = client.get(f"/api/temple/mitigation_sessions/{started.json()['id']}")
    assert detail.status_code == 200
    assert [event["event_type"] for event in detail.json()["events"]] == ["started", "completed"]


def test_shift_cancel_blocks_new_mitigation_and_grants(client):
    prepare_operations(client)
    now = datetime.now(UTC)
    starts = (now - timedelta(hours=1)).isoformat()
    ends = (now + timedelta(hours=8)).isoformat()
    shift = client.post("/api/temple/duty/shifts", json=shift_payload(starts_at=starts, ends_at=ends)).json()
    grant = client.post("/api/temple/duty/grants", json=grant_payload(valid_from=starts, valid_until=ends))
    assert grant.status_code == 201, grant.text
    cancelled = client.post(f"/api/temple/duty/shifts/{shift['id']}/cancel", json={"actor": "tests", "reason": "天气原因"})
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["grants"][0]["state"] == "revoked"
    assert cancelled.json()["grants"][0]["revoked_reason"] == "shift_cancelled"
    observation = client.post("/api/temple/observations", json=observation_payload(STEWARD_A, "observation-duty-0003"))
    denied = client.post(f"/api/temple/safety_incidents/{observation.json()['safety_incident_id']}/mitigate", json={"actor": "tests"})
    assert denied.status_code == 409
    late_grant = client.post("/api/temple/duty/grants", json=grant_payload(grant_code="grant-late-0001", valid_from=starts, valid_until=ends))
    assert late_grant.status_code == 409
