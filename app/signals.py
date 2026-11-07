"""Tenant-isolated commercial signals with append-only corrections.

Work results are server-observed; manual reports are attestations, not verified
email/calendar integrations. No external message content is captured.
"""
from datetime import datetime, timedelta
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import principal, record_audit, uid, utcnow, aware
from .access import readable_record
from .db import get_db
from .models import CommercialSignal, User
from .relationships import lock_tenant_crm

router=APIRouter(prefix='/api/v1/signals',tags=['commercial-signals'])
KINDS=('proposal_sent','meeting_held','no_answer','replied','contacted')
class Strict(BaseModel):
 model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class Report(Strict):
 record_id:str
 kind:Literal['proposal_sent','meeting_held','no_answer','replied','contacted']
 occurred_at:datetime
 detail:str=Field(default='',max_length=300)
 idem:str=Field(min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
class Correction(Strict):
 kind:Literal['proposal_sent','meeting_held','no_answer','replied','contacted']
 occurred_at:datetime
 detail:str=Field(default='',max_length=300)
 idem:str=Field(min_length=8,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')

def valid_time(dt):
 when=aware(dt)
 if when>utcnow()+timedelta(minutes=5):
  raise HTTPException(422,'La fecha del evento no puede estar en el futuro')
 if when<utcnow()-timedelta(days=365*5):
  raise HTTPException(422,'Evento fuera del periodo permitido')
 return when

def present(row):
 return {'id':row.id,'record_id':row.record_id,'kind':row.kind,
         'source_type':row.source_type,'source_ref':row.source_ref,
         'occurred_at':aware(row.occurred_at).isoformat(),
         'recorded_at':aware(row.recorded_at).isoformat(),
         'verification':row.verification,'status':row.status,
         'supersedes_id':row.supersedes_id,'detail':row.detail}

def log_work_result(db, user, task, result):
 """Only a persisted user outcome is an internally observed source."""
 if not task.record_id or result.outcome not in ('contacted','no_answer','meeting_set','replied'):
  return
 kind={'meeting_set':'meeting_scheduled'}.get(result.outcome,result.outcome)
 # meeting_set means an appointment was SET, not HELD; never assert meeting_held.
 row=CommercialSignal(id=uid(),tenant_id=user.tenant_id,
  record_id=task.record_id,actor_id=user.id,kind=kind,source_type='work_result',
  source_ref=result.id,occurred_at=utcnow(),verification='recorded_action',
  detail='Resultado indicado en Focus: '+result.outcome,status='current')
 db.add(row)

def get_visible(db,user,record_id):
 if not readable_record(db,user,record_id):
  raise HTTPException(404,'Registro no accesible')

@router.get('/records/{record_id}')
def list_signals(record_id:str,db:Session=Depends(get_db),user:User=Depends(principal)):
 get_visible(db,user,record_id)
 rows=db.scalars(select(CommercialSignal).where(
  CommercialSignal.tenant_id==user.tenant_id,
  CommercialSignal.record_id==record_id).order_by(
  CommercialSignal.recorded_at.desc(),CommercialSignal.id.desc()).limit(100)).all()
 return {'items':[present(row) for row in rows],'truncated':len(rows)==100}

def create_report(db,user,record_id,kind,when,detail,idem,supersedes=None):
 lock_tenant_crm(db,user.tenant_id)
 source='manual_report'
 prior=db.scalar(select(CommercialSignal).where(
  CommercialSignal.tenant_id==user.tenant_id,CommercialSignal.source_type==source,
  CommercialSignal.source_ref==idem))
 if prior:
  if (prior.actor_id!=user.id or prior.record_id!=record_id or prior.kind!=kind
      or aware(prior.occurred_at)!=when or prior.detail!=detail or
      prior.supersedes_id!=supersedes):
   raise HTTPException(409,'Clave de idempotencia usada para otra señal')
  return {'signal':present(prior),'already_recorded':True}
 old=None
 if supersedes:
  old=db.scalar(select(CommercialSignal).where(
   CommercialSignal.tenant_id==user.tenant_id,CommercialSignal.id==supersedes).with_for_update())
  if not old or old.record_id!=record_id:raise HTTPException(404,'Señal inexistente')
  if old.status!='current':raise HTTPException(409,'Señal corregida previamente')
  # Preserve the original WorkResult and signal as immutable evidence; this user
  # attestation supersedes it for NBA rules without rewriting historical activity.
  if user.role!='admin' and old.actor_id!=user.id:
   raise HTTPException(403,'Solo el autor o un administrador puede corregirla')
  old.status='superseded'
 row=CommercialSignal(id=uid(),tenant_id=user.tenant_id,record_id=record_id,
   actor_id=user.id,kind=kind,source_type=source,source_ref=idem,
   occurred_at=when,verification='user_attested',status='current',
   detail=detail,supersedes_id=supersedes)
 db.add(row)
 record_audit(db,user,'signal.correct' if supersedes else 'signal.report',
              row.id,{'record_id':record_id,'kind':kind,'supersedes':supersedes})
 db.commit()
 return {'signal':present(row),'already_recorded':False}

@router.post('/reports')
def report(data:Report,db:Session=Depends(get_db),user:User=Depends(principal)):
 get_visible(db,user,data.record_id)
 return create_report(db,user,data.record_id,data.kind,valid_time(data.occurred_at),
                      data.detail,data.idem)

@router.post('/{signal_id}/correct')
def correct(signal_id:str,data:Correction,db:Session=Depends(get_db),
            user:User=Depends(principal)):
 # Fail closed before any write; never accept a client-selected tenant.
 prior=db.scalar(select(CommercialSignal).where(
   CommercialSignal.id==signal_id,CommercialSignal.tenant_id==user.tenant_id))
 if not prior:raise HTTPException(404,'Señal inexistente')
 get_visible(db,user,prior.record_id)
 return create_report(db,user,prior.record_id,data.kind,valid_time(data.occurred_at),
                      data.detail,data.idem,supersedes=signal_id)
