from __future__ import annotations

from pydantic import BaseModel, Field


class ShiftCreate(BaseModel):
    temple_code: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    starts_at: str
    ends_at: str
    actor: str = Field(min_length=1, max_length=120)


class ShiftAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=500)


class OffboardAction(ShiftAction):
    subject_hash: str = Field(min_length=16, max_length=128)


class GrantCreate(BaseModel):
    grant_code: str = Field(min_length=4, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    shift_code: str = Field(min_length=2, max_length=80)
    subject_hash: str = Field(min_length=16, max_length=128)
    hall_code: str | None = Field(default=None, max_length=64)
    actions: list[str] = Field(min_length=1, max_length=16)
    valid_from: str
    valid_until: str
    delegation_depth: int | None = Field(default=None, ge=0, le=16)
    actor: str = Field(min_length=1, max_length=120)


class DelegationCreate(BaseModel):
    grant_code: str = Field(min_length=4, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    subject_hash: str = Field(min_length=16, max_length=128)
    hall_code: str | None = Field(default=None, max_length=64)
    actions: list[str] = Field(min_length=1, max_length=16)
    valid_from: str
    valid_until: str
    actor: str = Field(min_length=1, max_length=120)
