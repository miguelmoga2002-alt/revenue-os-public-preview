"""Tenant-scoped security event triage, read-only and deliberately bounded.

This is NOT a SIEM, pager or infrastructure alert pipeline. It surfaces
persisted audit patterns to a tenant admin without leaking other tenants.
"""
from collections import Counter
from datetime import timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import admin, utcnow, aware
from .db import get_db
from .models import Audit, User

router=APIRouter(prefix='/api/v1/security',tags=['Security operations'])
MAX_AUDIT_EVENTS=5000
WINDOW=timedelta(hours=24)

@router.get('/alerts')
def alerts(db:Session=Depends(get_db),user:User=Depends(admin)):
 now=utcnow()
 rows=db.scalars(select(Audit).where(
  Audit.tenant_id==user.tenant_id,
  Audit.created_at>=now-WINDOW,
  Audit.created_at<=now,
  Audit.action.in_(('login.failure','crm.oauth.connect','crm.oauth.disconnect',
                    'pipedrive.sync','crm.oauth.sync','signal.correct')))
  .order_by(Audit.created_at.desc(),Audit.id.desc()).limit(MAX_AUDIT_EVENTS+1)).all()
 truncated=len(rows)>MAX_AUDIT_EVENTS
 rows=rows[:MAX_AUDIT_EVENTS]
 failures=Counter(row.user_id for row in rows if row.action=='login.failure')
 sensitive=Counter(row.action for row in rows if row.action!='login.failure')
 findings=[]
 for actor,count in failures.items():
  if count>=3:
   findings.append({'code':'repeated_login_failures','severity':'warning',
                    'actor_id':actor,'count':count,
                    'explanation':'Tres o más errores de acceso en las últimas 24 horas'})
 for event,count in sensitive.items():
  if count>0:
   findings.append({'code':'sensitive_operation','severity':'info',
                    'event_type':event,'count':count,
                    'explanation':'Operaciones sensibles registradas para revisión del administrador'})
 return {'window_hours':24,'as_of':now.isoformat(),'truncated':truncated,
         'events_examined':len(rows),'findings':findings,
         'delivery':'admin_pull_only',
         'note':'Monitorización por consulta, sin notificación automática ni SIEM externo'}
