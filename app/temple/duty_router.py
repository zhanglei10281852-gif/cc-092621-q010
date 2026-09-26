from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.duty import DUTY_ACTIONS, DutyAuthorizationService
from app.temple.duty_schemas import DelegationCreate, GrantCreate, OffboardAction, ShiftAction, ShiftCreate

router = APIRouter(prefix="/api/temple/duty", tags=["节庆值守范围授权"])


def service() -> DutyAuthorizationService:
    return DutyAuthorizationService()


@router.post("/shifts", status_code=201)
def create_shift(payload: ShiftCreate):
    return service().create_shift(payload.model_dump())


@router.get("/shifts")
def list_shifts(temple_code: str | None = None):
    return {"items": service().list_shifts(temple_code)}


@router.get("/shifts/{shift_id}")
def shift_detail(shift_id: int):
    return service().shift_detail(shift_id)


@router.post("/shifts/{shift_id}/cancel")
def cancel_shift(shift_id: int, payload: ShiftAction):
    return service().cancel_shift(shift_id, payload.actor, payload.reason)


@router.post("/shifts/{shift_id}/complete")
def complete_shift(shift_id: int, payload: ShiftAction):
    return service().complete_shift(shift_id, payload.actor, payload.reason)


@router.post("/shifts/{shift_id}/offboard")
def offboard_shift_member(shift_id: int, payload: OffboardAction):
    return service().offboard_shift_member(shift_id, payload.subject_hash, payload.actor, payload.reason)


@router.post("/grants", status_code=201)
def create_grant(payload: GrantCreate):
    return service().create_grant(payload.model_dump())


@router.get("/grants/{grant_id}")
def grant_detail(grant_id: int):
    return service().grant_detail(grant_id)


@router.post("/grants/{grant_id}/delegate", status_code=201)
def delegate_grant(grant_id: int, payload: DelegationCreate):
    return service().delegate_grant(grant_id, payload.model_dump())


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: int, payload: ShiftAction):
    return service().revoke_grant(grant_id, payload.actor, payload.reason)


@router.get("/explain")
def explain(subject_hash: str, temple_code: str, action: str, hall_code: str | None = None, at: str | None = None):
    return service().explain(subject_hash=subject_hash, temple_code=temple_code, hall_code=hall_code, action=action, at=at)


@router.get("/actions")
def duty_actions():
    return {"items": list(DUTY_ACTIONS)}


@router.post("/sweep")
def sweep_expired(actor: str = Query(default="duty-scope-reaper", min_length=1)):
    return service().sweep_expired(actor)
