"""Fail-closed API record access. A tenant filter alone is not a rep ACL."""
from sqlalchemy import false, select
from sqlalchemy.orm import Session
from .models import CRMRecord, RecordGrant, User


def visible_record_condition(user: User):
    if user.role == "admin":
        return CRMRecord.tenant_id == user.tenant_id
    if user.role == "rep":
        granted = select(RecordGrant.record_id).where(
            RecordGrant.tenant_id == user.tenant_id,
            RecordGrant.user_id == user.id,
        )
        return (CRMRecord.tenant_id == user.tenant_id) & CRMRecord.id.in_(granted)
    return false()


def readable_record(db: Session, user: User, record_id: str):
    return db.scalar(select(CRMRecord).where(
        CRMRecord.id == record_id, visible_record_condition(user)
    ))
