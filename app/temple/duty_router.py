from __future__ import annotations

from fastapi import APIRouter, Query

from app.temple.duty import DutyRosterService
from app.temple.schemas import (
    ScopedGrantCreate,
    ScopedGrantDelegate,
    ScopedGrantExplain,
    ScopedGrantRevoke,
    ShiftCancel,
    ShiftCreate,
    SubjectOffboard,
)

router = APIRouter(prefix="/api/temple/duty", tags=["值守班次与范围授权"])


def service() -> DutyRosterService:
    return DutyRosterService()


@router.post("/shifts", status_code=201)
def create_shift(payload: ShiftCreate):
    return service().create_shift(payload.model_dump())


@router.get("/shifts")
def list_shifts(temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_shifts(temple_code, state)}


@router.get("/shifts/{shift_id}")
def shift_detail(shift_id: int):
    return service().shift_detail(shift_id)


@router.post("/shifts/{shift_id}/cancel")
def cancel_shift(shift_id: int, payload: ShiftCancel):
    return service().cancel_shift(shift_id, payload.actor, payload.reason)


@router.post("/grants", status_code=201)
def create_grant(payload: ScopedGrantCreate):
    return service().create_grant(payload.model_dump())


@router.get("/grants")
def list_grants(subject_hash: str | None = None, temple_code: str | None = None, state: str | None = None):
    return {"items": service().list_grants(subject_hash, temple_code, state)}


# 固定路径必须先于 /grants/{grant_id} 声明，否则会被路径参数截获。
@router.post("/grants/explain")
def explain_grant(payload: ScopedGrantExplain):
    return service().explain(payload.model_dump())


@router.post("/grants/expire")
def expire_grants(actor: str = Query(default="scoped-grant-reaper", min_length=1)):
    return service().expire_due_grants(actor)


@router.get("/grants/{grant_id}")
def grant_detail(grant_id: int):
    return service().grant_detail(grant_id)


@router.post("/grants/{parent_grant_id}/delegate", status_code=201)
def delegate_grant(parent_grant_id: int, payload: ScopedGrantDelegate):
    return service().delegate_grant(parent_grant_id, payload.model_dump())


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(grant_id: int, payload: ScopedGrantRevoke):
    return service().revoke_grant(grant_id, payload.actor, payload.reason)


@router.post("/subjects/offboard")
def offboard_subject(payload: SubjectOffboard):
    return service().offboard_subject(payload.model_dump())
