"""Database-persisted guided workflow; deterministic priority, no background scheduler yet."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import WorkPolicy,WorkItem,WorkLease,WorkResult,User,CRMRecord,RecordGrant,TaskCreateReceipt,CommercialSignal
from .auth import uid,utcnow,principal,admin,record_audit
from .relationships import lock_tenant_crm
from .signals import log_work_result
router=APIRouter(prefix='/api/v1/work',tags=['guided-execution'])
class Strict(BaseModel):model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class PolicyIn(Strict):
 mode:str=Field(pattern='^(guided|pipeline|hybrid)$')
 timezone:str=Field(max_length=60)
 weights:dict[str,int]
class TaskIn(Strict):
 idem:str=Field(min_length=8,max_length=120,pattern=r'^[A-Za-z0-9_-]+$')
 owner_id:str
 record_id:str|None=None
 title:str=Field(min_length=1,max_length=300)
 channel:str=Field(pattern='^(call|email|whatsapp|meeting|research|other)$')
 due_at:datetime;priority_source:str=Field(default='manual',max_length=80)
class ResultIn(Strict):
 task_id:str;idem:str=Field(min_length=8,max_length=120)
 outcome:str=Field(pattern='^(contacted|no_answer|meeting_set|replied|done|blocked)$')
 note:str=Field(default='',max_length=3000)
 next_title:str|None=Field(default=None,max_length=300)
 next_due_at:datetime|None=None

def active_policy(db,user):
 p=db.get(WorkPolicy,user.tenant_id)
 if not p:
  p=WorkPolicy(tenant_id=user.tenant_id,mode='guided',timezone='Europe/Madrid',weights=json.dumps({'overdue':5,'inbound':15,'proposal':10,'stale':5}))
  db.add(p);db.flush()
 return p

def aware(dt):return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def lock_owner_work(db:Session,tenant_id:str,owner_id:str):
 # PostgreSQL serializes claims, completions and manager releases for a rep.
 # SQLite is a single-process lab fallback, not a concurrency guarantee.
 if db.get_bind().dialect.name == 'postgresql':
  found=db.scalar(select(User.id).where(User.id==owner_id,User.tenant_id==tenant_id).with_for_update())
  if not found:raise HTTPException(404,'Comercial no encontrado')


def detail(task,policy,now,signal=None):
 """Explain a manager-assigned action using persisted facts, not inferred CRM signals.

 This is a deterministic queue priority, not a model prediction of conversion.
 Only work_items and work_policies contribute points; no unverified stage,
 email, call, or intent signal is silently treated as evidence.
 """
 due=aware(task.due_at)
 created=aware(task.created_at)
 overdue_quarters=max(0,int((now-due).total_seconds()/900))
 weights=json.loads(policy.weights)
 overdue_points=min(overdue_quarters,96)*weights.get('overdue',5)
 source_points={'inbound':weights.get('inbound',15),
                'proposal':weights.get('proposal',10)}.get(task.priority_source,0)
 signal_points=12 if signal and signal.kind=='no_answer' and signal.source_type=='work_result' else 0
 score=100+overdue_points+source_points+signal_points
 overdue_minutes=max(0,int((now-due).total_seconds()/60))
 reasons=[f'Acción vencida hace {overdue_minutes} minutos' if overdue_minutes else
          'Compromiso disponible según la fecha asignada']
 evidence=[{
  'rule':'due_window',
  'origin':'work_items',
  'field':'due_at',
  'value':due.isoformat(),
  'recorded_at':created.isoformat(),
  'points':overdue_points,
  'explanation':'La fecha proviene de la tarea registrada; los puntos por retraso se limitan a 96 intervalos de 15 minutos.',
 }]
 if signal_points:
  reasons.append('Último intento registrado sin respuesta')
  evidence.append({
   'rule':'recent_no_answer',
   'origin':'commercial_signals',
   'field':'kind',
   'value':signal.kind,
   'source_ref':signal.source_ref,
   'occurred_at':aware(signal.occurred_at).isoformat(),
   'recorded_at':aware(signal.recorded_at).isoformat(),
   'verification':signal.verification,
   'points':signal_points,
   'explanation':'Resultado no_answer registrado previamente desde Focus para este lead; válido durante siete días.',
  })
 if task.priority_source!='manual':
  reasons.append('Clasificación asignada: '+task.priority_source)
  evidence.append({
   'rule':'declared_priority',
   'origin':'work_items',
   'field':'priority_source',
   'value':task.priority_source,
   'recorded_at':created.isoformat(),
   'points':source_points,
   'explanation':'Valor declarado al crear la tarea. No confirma por sí solo una señal de correo, CRM externo o llamada.',
  })
 return {
  'id':task.id,'record_id':task.record_id,'title':task.title,
  'due_at':due.isoformat(),'channel':task.channel,
  'score':score,'reasons':reasons,
  'recommendation':{'action':task.title,'channel':task.channel,
                    'basis':'assigned_work_item','automatic_execution':False},
  'evidence':evidence,
  'score_breakdown':{'base':100,'overdue':overdue_points,'declared_priority':source_points,'verified_signal':signal_points},
  'policy_revision':policy.revision,
  'evaluated_at':now.isoformat(),
  'evidence_scope':'assigned_task_and_recorded_outcomes' if signal_points else 'assigned_task_only',
 }

def recent_signals(db,user,tasks,now):
 ids={task.record_id for task in tasks if task.record_id}
 if not ids:return {}
 rows=db.scalars(select(CommercialSignal).where(
  CommercialSignal.tenant_id==user.tenant_id,
  CommercialSignal.record_id.in_(ids),
  CommercialSignal.source_type=='work_result',
  CommercialSignal.verification=='recorded_action',
  CommercialSignal.status=='current',
  CommercialSignal.kind=='no_answer',
  CommercialSignal.occurred_at>=now-timedelta(days=7),
  CommercialSignal.occurred_at<=now,
  CommercialSignal.actor_id==user.id).order_by(
  CommercialSignal.occurred_at.desc(),CommercialSignal.id.desc()).limit(2000)).all()
 found={}
 for row in rows:found.setdefault(row.record_id,row)
 return found

def ranked_details(db,user,policy,tasks,now):
 by_record=recent_signals(db,user,tasks,now)
 return [detail(t,policy,now,by_record.get(t.record_id)) for t in tasks]

@router.get('/policy')
def get_policy(db:Session=Depends(get_db),user:User=Depends(principal)):
 p=active_policy(db,user)
 return {'mode':p.mode,'timezone':p.timezone,'weights':json.loads(p.weights),'revision':p.revision}
@router.put('/policy')
def set_policy(item:PolicyIn,db:Session=Depends(get_db),user:User=Depends(admin)):
 try:ZoneInfo(item.timezone)
 except ZoneInfoNotFoundError:raise HTTPException(422,'Zona horaria desconocida')
 keys={'overdue','inbound','proposal','stale'}
 if set(item.weights)!=keys or any(not isinstance(v,int) or isinstance(v,bool) or v<0 or v>100 for v in item.weights.values()):raise HTTPException(422,'Pesos fuera de rango o faltantes')
 p=active_policy(db,user);p.mode=item.mode;p.timezone=item.timezone;p.weights=json.dumps(item.weights);p.revision+=1
 record_audit(db,user,'policy.update',user.tenant_id,{'mode':item.mode,'revision':p.revision});db.commit()
 return {'mode':p.mode,'revision':p.revision}
def task_payload_sha(item: TaskIn) -> str:
 # Canonicalize times to UTC and exclude the operation key itself.
 normalized = item.model_dump(mode='json', exclude={'idem'})
 normalized['due_at'] = aware(item.due_at).isoformat()
 blob = json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
 return hashlib.sha256(blob.encode('utf-8')).hexdigest()

@router.post('/tasks')
def new_task(item:TaskIn,db:Session=Depends(get_db),user:User=Depends(admin)):
 # Tenant lock serializes requests from multiple managers/processes in PostgreSQL.
 # SQLite keeps the receipt unique for sequential lab tests; not an HA database.
 lock_tenant_crm(db,user.tenant_id)
 payload_sha = task_payload_sha(item)
 prior = db.get(TaskCreateReceipt, (user.tenant_id, item.idem))
 if prior:
  if prior.actor_id!=user.id or prior.payload_sha!=payload_sha:
   raise HTTPException(409,'Clave de idempotencia reutilizada con otra operación')
  previous_task=db.scalar(select(WorkItem).where(WorkItem.id==prior.task_id,WorkItem.tenant_id==user.tenant_id))
  if not previous_task:raise HTTPException(409,'Recibo sin tarea; requiere revisión')
  return {'id':previous_task.id,'state':previous_task.state,'already_recorded':True}
 owner=db.scalar(select(User).where(User.id==item.owner_id,User.tenant_id==user.tenant_id,User.active==True))
 if not owner or owner.role!='rep':raise HTTPException(422,'Comercial no válido para esta empresa')
 if item.record_id and not db.scalar(select(CRMRecord).where(CRMRecord.id==item.record_id,CRMRecord.tenant_id==user.tenant_id)):
  raise HTTPException(422,'Lead no válido para esta empresa')
 if item.record_id and not db.get(RecordGrant,(user.tenant_id,owner.id,item.record_id)):
  db.add(RecordGrant(tenant_id=user.tenant_id,user_id=owner.id,record_id=item.record_id))
  record_audit(db,user,'record.access.grant',item.record_id,{'rep_id':owner.id,'via':'task'})
 task=WorkItem(id=uid(),tenant_id=user.tenant_id,owner_id=owner.id,record_id=item.record_id,title=item.title,channel=item.channel,due_at=aware(item.due_at),priority_source=item.priority_source)
 db.add(task);db.flush()
 db.add(TaskCreateReceipt(tenant_id=user.tenant_id,idem=item.idem,actor_id=user.id,
                          task_id=task.id,payload_sha=payload_sha))
 record_audit(db,user,'task.create',task.id,{'owner':owner.id})
 db.commit()
 return {'id':task.id,'state':'open','already_recorded':False}
def scheduled_for_user(db:Session,user:User,now:datetime):
 # Future work must be visible but cannot be claimed before due_at.
 # Query strictly by authenticated tenant + owner, independent of other users.
 rows=db.scalars(select(WorkItem).where(
  WorkItem.tenant_id==user.tenant_id,
  WorkItem.owner_id==user.id,
  WorkItem.state=='open',
  WorkItem.due_at>now
 ).order_by(WorkItem.due_at,WorkItem.id).limit(26)).all()
 return ([{
  'id':row.id,'record_id':row.record_id,'title':row.title,
  'channel':row.channel,'due_at':aware(row.due_at).isoformat(),
  'state':'scheduled','executable':False,
 } for row in rows[:25]],len(rows)>25)

@router.get('/queue')
def queue(db:Session=Depends(get_db),user:User=Depends(principal)):
 p=active_policy(db,user)
 if user.role=='rep' and p.mode=='pipeline':raise HTTPException(403,'Trabajo guiado deshabilitado por la empresa')
 now=utcnow()
 upcoming,upcoming_truncated=scheduled_for_user(db,user,now)
 due=queue_for_user(db,user,p,now)
 due_list=[{
  'id':row['id'],'record_id':row['record_id'],'title':row['title'],
  'channel':row['channel'],'due_at':row['due_at'],
  'state':'awaiting_priority','executable_from_list':False,
  'score':row['score']
 } for row in due[:25]]
 due_truncated=len(due)>25
 lease=db.get(WorkLease,(user.tenant_id,user.id))
 if lease and aware(lease.expires_at)>now:
  t=db.scalar(select(WorkItem).where(WorkItem.id==lease.task_id,WorkItem.tenant_id==user.tenant_id,WorkItem.owner_id==user.id))
  if t:return {'active':ranked_details(db,user,p,[t],now)[0],'ready':[],
               'locked':True,'upcoming':upcoming,'upcoming_truncated':upcoming_truncated,
               'queued':due_list,'queued_truncated':due_truncated}
 return {'active':None,'ready':due[:25],'locked':False,
         'upcoming':upcoming,'upcoming_truncated':upcoming_truncated,
         'queued':due_list,'queued_truncated':due_truncated}
@router.post('/claim/{task_id}')
def claim(task_id:str,db:Session=Depends(get_db),user:User=Depends(principal)):
 if user.role!='rep':raise HTTPException(403,'Solo comercial')
 lock_owner_work(db,user.tenant_id,user.id)
 p=active_policy(db,user)
 if p.mode=='pipeline':raise HTTPException(403,'Trabajo guiado deshabilitado')
 now=utcnow()
 lease=db.get(WorkLease,(user.tenant_id,user.id))
 if lease and aware(lease.expires_at)>now:
  if lease.task_id==task_id:return {'id':task_id,'state':'active','expires_at':aware(lease.expires_at).isoformat()}
  raise HTTPException(409,'Debes resolver la tarea activa')
 task=db.scalar(select(WorkItem).where(WorkItem.id==task_id,WorkItem.tenant_id==user.tenant_id,WorkItem.owner_id==user.id,WorkItem.state=='open'))
 if not task:raise HTTPException(404,'Tarea no disponible')
 if aware(task.due_at)>now:raise HTTPException(409,'La tarea todavía no está disponible')
 eligible=queue_for_user(db,user,p,now)
 if not eligible or eligible[0]['id']!=task_id:raise HTTPException(409,'Debes atender la máxima prioridad')
 if lease:db.delete(lease);db.flush()
 db.add(WorkLease(tenant_id=user.tenant_id,owner_id=user.id,task_id=task.id,expires_at=now+timedelta(minutes=45)))
 task.state='active';record_audit(db,user,'task.claim',task.id,{});db.commit()
 return {'id':task.id,'state':'active','expires_at':(now+timedelta(minutes=45)).isoformat()}
def queue_for_user(db,user,p,now):
 tasks=db.scalars(select(WorkItem).where(WorkItem.tenant_id==user.tenant_id,WorkItem.owner_id==user.id,WorkItem.state=='open',WorkItem.due_at<=now).limit(250)).all()
 return sorted(ranked_details(db,user,p,tasks,now),key=lambda t:(-t['score'],t['due_at'],t['id']))
@router.post('/complete')
def complete(data:ResultIn,db:Session=Depends(get_db),user:User=Depends(principal)):
 if user.role!='rep':raise HTTPException(403,'Solo comercial')
 lock_owner_work(db,user.tenant_id,user.id)
 prior=db.scalar(select(WorkResult).where(WorkResult.tenant_id==user.tenant_id,WorkResult.owner_id==user.id,WorkResult.idem==data.idem))
 if prior:
  if prior.task_id!=data.task_id:raise HTTPException(409,'Clave de idempotencia reutilizada')
  return {'result_id':prior.id,'already_recorded':True}
 lease=db.get(WorkLease,(user.tenant_id,user.id))
 if not lease or lease.task_id!=data.task_id:raise HTTPException(409,'No es la tarea activa')
 if aware(lease.expires_at)<=utcnow():raise HTTPException(409,'Sesión de tarea caducada; contactar con manager')
 task=db.scalar(select(WorkItem).where(WorkItem.id==data.task_id,WorkItem.tenant_id==user.tenant_id,WorkItem.owner_id==user.id))
 if not task:raise HTTPException(404,'Tarea inexistente')
 if data.next_title and not data.next_due_at:raise HTTPException(422,'Falta fecha de seguimiento')
 if not data.next_title and task.record_id and data.outcome not in ('done','blocked'):
  raise HTTPException(422,'Lead activo: programa el siguiente paso o indica que está bloqueado')
 row=WorkResult(id=uid(),tenant_id=user.tenant_id,owner_id=user.id,task_id=task.id,idem=data.idem,outcome=data.outcome,note=data.note)
 db.add(row);log_work_result(db,user,task,row);task.state='done';db.delete(lease)
 if data.next_title:
  db.add(WorkItem(id=uid(),tenant_id=user.tenant_id,owner_id=user.id,record_id=task.record_id,title=data.next_title,channel=task.channel,due_at=aware(data.next_due_at),priority_source=task.priority_source))
 record_audit(db,user,'task.complete',task.id,{'outcome':data.outcome});db.commit()
 return {'result_id':row.id,'already_recorded':False}
@router.post('/release/{owner_id}')
def release(owner_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 lock_owner_work(db,user.tenant_id,owner_id)
 lease=db.get(WorkLease,(user.tenant_id,owner_id))
 if not lease:raise HTTPException(404,'Sin tarea activa')
 task=db.scalar(select(WorkItem).where(WorkItem.id==lease.task_id,WorkItem.tenant_id==user.tenant_id))
 if task and task.state=='active':task.state='open'
 db.delete(lease);record_audit(db,user,'task.release',owner_id,{'reason':'manual manager release'});db.commit()
 return {'released':True}
