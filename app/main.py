"""Revenue OS v1.1 release candidate API. Run Alembic upgrade head before serving."""
from __future__ import annotations
import hashlib,json,os,re,uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,Form,Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel,Field,ConfigDict
from .db import get_db
from .tenant_context import set_tenant_context
from .relationships import reconcile,lock_tenant_crm
from .crm_workspace import router as crm_router
from .deal_workspace import router as deal_router
from .access import visible_record_condition, readable_record
from .models import User,Session as AuthSession,CRMRecord,CRMLink,RecordGrant,ImportBatch,BatchMutation
from .auth import uid,utcnow,verify,principal,admin,record_audit,login_attempt_for,deny_login,DUMMY_HASH
from .parser import read_rows
from .security_auth import require_admin_mfa,decrypt_secret,verified_step
from .http_hardening import policy as browser_csp, allowed_browser_origin
from .runtime_security import require_secure_runtime
from .security_monitor import router as monitoring_router
from .work import router as work_router
from .signals import router as signal_router
from .pipedrive_sync import router as pipedrive_router
from .crm_oauth import router as oauth_router
from .crm_multi_sync import router as multi_sync_router
from .auth import hash_password,aware
from .models import WorkPolicy,WorkItem,AdminTOTP,SessionProof,CommercialSignal,DealNote

APP_VERSION = '1.1.0-rc.1'
app=FastAPI(title='Revenue OS — Deal Workspace (release candidate)',version=APP_VERSION)

@app.on_event('startup')
def check_production_runtime():
 require_secure_runtime()

# Defense in depth: browser hardening and cache prevention for sensitive APIs.
# HTTPS termination and HSTS are the responsibility of the deployment proxy.
@app.middleware('http')
async def security_headers(request:Request,call_next):
 if request.method in ('POST','PUT','PATCH','DELETE') and not allowed_browser_origin(request):
  return JSONResponse(status_code=403,content={'detail':'Origen de solicitud no permitido'},
   headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
 response=await call_next(request)
 csp=browser_csp(request.url.path)
 if csp:response.headers['Content-Security-Policy']=csp
 if request.url.scheme=='https':
  response.headers['Strict-Transport-Security']='max-age=31536000'
 if request.url.path in ('/','/focus','/crm','/api/v1/oauth/callback') or request.url.path.startswith('/deals/'):
  response.headers['Cache-Control']='no-store'
  response.headers['Pragma']='no-cache'
 response.headers['X-Content-Type-Options']='nosniff'
 response.headers['X-Frame-Options']='DENY'
 response.headers['Referrer-Policy']='no-referrer'
 response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
 response.headers['Cross-Origin-Resource-Policy']='same-origin'
 if request.url.path.startswith('/api/'):
  response.headers['Cache-Control']='no-store'
  response.headers['Pragma']='no-cache'
 return response

app.include_router(work_router)
app.include_router(signal_router)
app.include_router(pipedrive_router)
app.include_router(oauth_router)
app.include_router(multi_sync_router)
app.include_router(monitoring_router)
app.include_router(crm_router)
app.include_router(deal_router)
ROOT=Path(__file__).resolve().parents[1]
KINDS=('companies','contacts','deals','activities')
FIELDS=('external_id','name','email','phone','company_external_id','contact_external_id','deal_external_id','stage','owner','amount','due_at')
ALIASES={
 'external_id':['id','person id','organization id','deal id','activity id','record id','hs_object_id','salesforce id','pipedrive id','contact id','unique id','external_id'],
 'name':['name','title','deal title','organization name','person name','contact name','company','subject'],
 'email':['email','email address','primary email','e-mail'],
 'phone':['phone','phone number','mobile','mobile phone'],
 'company_external_id':['organization id','company id','account id','associated company id','company_external_id'],
 'contact_external_id':['person id','contact id','associated contact id','contact_external_id'],
 'deal_external_id':['deal id','associated deal id','deal_external_id'],
 'stage':['stage','stage name','status','deal stage','pipeline stage'],
 'owner':['owner','owner name','deal owner','assigned user','user'],
 'amount':['value','amount','deal value','monetary value','price'],
 'due_at':['due date','due_at','date','activity date']}
REQUIRED={'companies':('name',),'contacts':('name',),'deals':('name',),'activities':('name',)}
class Strict(BaseModel):model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class Login(Strict):
 username:str=Field(min_length=1,max_length=120)
 password:str=Field(min_length=1,max_length=1024)
 otp:str|None=Field(default=None,max_length=20)
class Mapping(Strict):kind:Literal['companies','contacts','deals','activities'];source:str=Field(min_length=2,max_length=80,pattern='^[a-zA-Z0-9_-]+$');columns:dict[str,str];strategy:Literal['skip','upsert']='skip';digest:str=Field(min_length=64,max_length=64)

def require_mapping(headers,model):
 if not set(model.columns).issubset(set(FIELDS)):raise HTTPException(422,'Campo destino no reconocido')
 if not set(model.columns.values()).issubset(set(headers)):raise HTTPException(422,'La columna no existe en el archivo')
 if len(set(model.columns.values()))!=len(model.columns):raise HTTPException(422,'Una columna no puede mapearse dos veces')
 for k in REQUIRED[model.kind]:
  if k not in model.columns:raise HTTPException(422,f'Falta mapear {k}')

def digest(data):return hashlib.sha256(data).hexdigest()
def propose(headers,kind):
 mapping={};seen=set()
 for target in FIELDS:
  for alias in ALIASES[target]:
   options=[h for h in headers if h.casefold().strip()==alias]
   if options and options[0] not in seen:
    # Ambiguous id columns should only map to external_id when explicitly requested.
    mapping[target]=options[0];seen.add(options[0]);break
 return mapping

def normalized(row,model):
 data={dest:row.get(column,'').strip() for dest,column in model.columns.items()}
 remaining={k:v for k,v in row.items() if k not in model.columns.values() and v!=''}
 return data,remaining

def row_errors(rows,model):
 errors=[];keys=set()
 for index,row in enumerate(rows,start=2):
  d,_=normalized(row,model)
  if not d.get('name'):errors.append({'line':index,'field':'name','message':'Nombre obligatorio'})
  external=d.get('external_id')
  if not external:errors.append({'line':index,'field':'external_id','message':'ID externo obligatorio para migración idempotente'})
  elif external in keys:errors.append({'line':index,'field':'external_id','message':'ID externo duplicado en archivo'})
  else:keys.add(external)
  if d.get('email') and ('@' not in d['email'] or len(d['email'])>320):errors.append({'line':index,'field':'email','message':'Correo inválido'})
  if d.get('amount') and (len(d['amount'])>80 or not re.fullmatch(r'[0-9.,+ -]+',d['amount'])):errors.append({'line':index,'field':'amount','message':'Importe no válido'})
  if len(errors)>=100:break
 return errors

@app.get('/')
def root():return FileResponse(ROOT/'web'/'index.html')
@app.get('/focus')
def focus():return FileResponse(ROOT/'web'/'focus.html')
@app.get('/crm')
def crm_page():return FileResponse(ROOT/'web'/'crm.html')

# Canonical, stable browser URL for a deal. HTML contains no record data or
# credentials; the authenticated /api/v1/crm/{id}/360 endpoint remains the
# only source of truth and enforces tenant/grant ACL after sign-in.
@app.get('/deals/{deal_id}')
def deal_deep_link(deal_id:uuid.UUID):
 return FileResponse(ROOT/'web'/'crm.html')

@app.get('/health')
def health():return {'status':'up','version':APP_VERSION,'warning':'Release candidate de laboratorio. No implica preparación para producción'}
@app.post('/api/v1/auth/login')
def login(data:Login,db:Session=Depends(get_db)):
 user=db.scalar(select(User).where(User.username==data.username))
 if not user or not user.active:
  verify(data.password,DUMMY_HASH)
  raise HTTPException(401,'Credenciales inválidas')
 set_tenant_context(db,user.tenant_id)
 attempt=login_attempt_for(db,user)
 if not verify(data.password,user.password_hash):deny_login(db,attempt,user)
 mfa_ok=False
 if user.role=='admin':
  credential=db.scalar(select(AdminTOTP).where(
   AdminTOTP.tenant_id==user.tenant_id,AdminTOTP.user_id==user.id).with_for_update())
  if require_admin_mfa() or credential:
   if credential is None:raise HTTPException(403,'MFA de administración sin aprovisionar')
   step=verified_step(decrypt_secret(credential.secret_ciphertext),
                      data.otp,credential.last_step)
   if step is None:deny_login(db,attempt,user)
   credential.last_step=step
   mfa_ok=True
 attempt.failures=0
 attempt.locked_until=None
 attempt.window_started_at=utcnow()
 import secrets
 token=secrets.token_hex(32)
 token_hash=hashlib.sha256(token.encode()).hexdigest()
 db.add(AuthSession(token_hash=token_hash,user_id=user.id,
                    expires_at=utcnow()+timedelta(hours=8)))
 db.flush()
 db.add(SessionProof(token_hash=token_hash,tenant_id=user.tenant_id,mfa_verified=mfa_ok))
 record_audit(db,user,'login',user.id,{'mfa':mfa_ok})
 db.commit()
 return {'access_token':token,'token_type':'bearer','role':user.role,'tenant':user.tenant_id}

@app.delete('/api/v1/auth/session')
def revoke(request:Request,db:Session=Depends(get_db),user:User=Depends(principal)):
 token=request.headers['authorization'][7:]
 row=db.get(AuthSession,hashlib.sha256(token.encode()).hexdigest());row.revoked=True
 record_audit(db,user,'logout',user.id,{})
 db.commit();return {'revoked':True}
@app.get('/api/v1/auth/sessions')
def own_sessions(request:Request,db:Session=Depends(get_db),user:User=Depends(principal)):
 current=hashlib.sha256(request.headers['authorization'][7:].encode()).hexdigest()
 rows=db.scalars(select(AuthSession).where(AuthSession.user_id==user.id).order_by(
  AuthSession.expires_at.desc()).limit(100)).all()
 return [{'id':r.token_hash,'expires_at':aware(r.expires_at).isoformat(),
          'revoked':r.revoked,'current':r.token_hash==current} for r in rows]

@app.post('/api/v1/auth/sessions/revoke-others')
def revoke_other_sessions(request:Request,db:Session=Depends(get_db),user:User=Depends(principal)):
 current=hashlib.sha256(request.headers['authorization'][7:].encode()).hexdigest()
 rows=db.scalars(select(AuthSession).where(
  AuthSession.user_id==user.id,AuthSession.token_hash!=current,
  AuthSession.revoked==False)).all()
 for row in rows:row.revoked=True
 record_audit(db,user,'session.revoke_others',user.id,{'count':len(rows)})
 db.commit()
 return {'revoked':len(rows)}

@app.post('/api/v1/auth/sessions/{session_hash}/revoke')
def revoke_own_session(session_hash:str,db:Session=Depends(get_db),user:User=Depends(principal)):
 if not re.fullmatch(r'[0-9a-f]{64}',session_hash):raise HTTPException(404,'Sesión inexistente')
 row=db.scalar(select(AuthSession).where(
  AuthSession.token_hash==session_hash,AuthSession.user_id==user.id))
 if not row:raise HTTPException(404,'Sesión inexistente')
 row.revoked=True
 record_audit(db,user,'session.revoke',user.id,{})
 db.commit()
 return {'revoked':True}

@app.post('/api/v1/admin/reps/{rep_id}/sessions/revoke')
def revoke_rep_sessions(rep_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 rep=db.scalar(select(User).where(User.tenant_id==user.tenant_id,
   User.id==rep_id,User.role=='rep'))
 if not rep:raise HTTPException(404,'Comercial no encontrado')
 rows=db.scalars(select(AuthSession).where(
  AuthSession.user_id==rep.id,AuthSession.revoked==False)).all()
 for row in rows:row.revoked=True
 record_audit(db,user,'session.revoke_rep',rep.id,{'count':len(rows)})
 db.commit()
 return {'revoked':len(rows)}

@app.get('/api/v1/me')
def me(user:User=Depends(principal)):
 return {'username':user.username,'role':user.role,'tenant_id':user.tenant_id}
@app.post('/api/v1/migrations/preview')
async def preview(kind:Literal['companies','contacts','deals','activities']=Form(...),file:UploadFile=File(...),user:User=Depends(admin)):
 data=await file.read(8*1024*1024+1);headers,rows=read_rows(data,file.filename or '')
 suggested=propose(headers,kind)
 return {'digest':digest(data),'filename':file.filename,'kind':kind,'total_rows':len(rows),'columns':headers,'suggested_mapping':suggested,'preview':rows[:8],'max_rows':10000,'recommendation':'Importar organizaciones, contactos, negocios y actividades en ese orden. Confirmar IDs y relaciones.'}
@app.post('/api/v1/migrations/commit')
async def commit(spec:str=Form(...),file:UploadFile=File(...),db:Session=Depends(get_db),user:User=Depends(admin)):
 try:model=Mapping.model_validate_json(spec)
 except Exception as e:raise HTTPException(422,'Configuración de mapeo inválida') from e
 data=await file.read(8*1024*1024+1)
 if digest(data)!=model.digest:raise HTTPException(409,'Archivo diferente al previsualizado')
 headers,rows=read_rows(data,file.filename or '')
 require_mapping(headers,model)
 if 'external_id' not in model.columns:raise HTTPException(422,'Debes mapear un ID externo estable')
 errors=row_errors(rows,model)
 if errors:raise HTTPException(422,{'message':'Se ha cancelado toda la importación; corrige errores','errors':errors})
 lock_tenant_crm(db,user.tenant_id)
 batch=ImportBatch(id=uid(),tenant_id=user.tenant_id,actor_id=user.id,kind=model.kind,source=model.source,digest=model.digest,status='complete')
 db.add(batch)
 db.flush()  # Persist batch FK before creating child mutation rows (PostgreSQL).
 count={'created':0,'updated':0,'skipped':0}
 try:
  # One atomic transaction; any exception rolls back the entire import.
  for row in rows:
   item,remaining=normalized(row,model)
   existing=db.scalar(select(CRMRecord).where(CRMRecord.tenant_id==user.tenant_id,CRMRecord.kind==model.kind,CRMRecord.source==model.source,CRMRecord.external_id==item['external_id']))
   if existing and model.strategy=='skip':count['skipped']+=1;continue
   values={'name':item['name'],'email':item.get('email') or None,'phone':item.get('phone') or None,'company_external_id':item.get('company_external_id') or None,'contact_external_id':item.get('contact_external_id') or None,'deal_external_id':item.get('deal_external_id') or None,'stage':item.get('stage') or None,'owner':item.get('owner') or None,'amount':item.get('amount') or None,'due_at':item.get('due_at') or None,'attributes':json.dumps(remaining,ensure_ascii=False)}
   if existing:
    previous={field:getattr(existing,field) for field in values}
    for field,value in values.items():setattr(existing,field,value)
    count['updated']+=1;operation='update';target=existing
   else:
    target=CRMRecord(id=uid(),tenant_id=user.tenant_id,kind=model.kind,source=model.source,external_id=item['external_id'],**values)
    db.add(target);db.flush()  # Ensure record FK exists before its BatchMutation.
    count['created']+=1;operation='create';previous=None
   db.add(BatchMutation(id=uid(),batch_id=batch.id,tenant_id=user.tenant_id,record_id=target.id,operation=operation,previous=json.dumps(previous,ensure_ascii=False) if previous else None))
  batch.created_count=count['created'];batch.updated_count=count['updated'];batch.skipped_count=count['skipped']
  record_audit(db,user,'migration.commit',batch.id,{'kind':model.kind,'source':model.source,**count})
  db.commit()
 except Exception:
  db.rollback();raise
 return {'batch_id':batch.id,'status':'complete',**count,'total':len(rows)}
@app.get('/api/v1/migrations/batches')
def batches(db:Session=Depends(get_db),user:User=Depends(admin)):
 rows=db.scalars(select(ImportBatch).where(ImportBatch.tenant_id==user.tenant_id).order_by(ImportBatch.created_at.desc()).limit(100)).all()
 return [{'id':r.id,'kind':r.kind,'source':r.source,'created':r.created_count,'updated':r.updated_count,'skipped':r.skipped_count,'status':r.status,'date':r.created_at.isoformat()} for r in rows]
@app.post('/api/v1/migrations/{batch_id}/rollback')
def rollback(batch_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 lock_tenant_crm(db,user.tenant_id)
 batch=db.scalar(select(ImportBatch).where(
  ImportBatch.id==batch_id,ImportBatch.tenant_id==user.tenant_id).with_for_update())
 if not batch:raise HTTPException(404,'Lote no encontrado')
 if batch.status!='complete':raise HTTPException(409,'Lote ya revertido')
 changes=db.scalars(select(BatchMutation).where(
  BatchMutation.batch_id==batch_id,BatchMutation.tenant_id==user.tenant_id)).all()
 # Refuse rollback when a newer completed import also modified these records.
 ids=[c.record_id for c in changes]
 if ids:
  later=db.scalar(select(func.count()).select_from(BatchMutation).join(
   ImportBatch,ImportBatch.id==BatchMutation.batch_id).where(
   BatchMutation.record_id.in_(ids),BatchMutation.batch_id!=batch_id,
   ImportBatch.tenant_id==user.tenant_id,ImportBatch.status=='complete',
   ImportBatch.created_at>=batch.created_at))
  if later:raise HTTPException(409,'Hay importaciones posteriores sobre esos registros; revierte primero los lotes más recientes')
 # Never remove records with real commercial activity attached; FK failures
 # must be explained instead of emitting a generic 500.
 created_ids=[c.record_id for c in changes if c.operation=='create']
 if created_ids:
  task_id=db.scalar(select(WorkItem.id).where(
   WorkItem.tenant_id==user.tenant_id,WorkItem.record_id.in_(created_ids)).limit(1))
  signal_id=db.scalar(select(CommercialSignal.id).where(
   CommercialSignal.tenant_id==user.tenant_id,
   CommercialSignal.record_id.in_(created_ids)).limit(1))
  note_id=db.scalar(select(DealNote.id).where(
   DealNote.tenant_id==user.tenant_id,
   DealNote.deal_id.in_(created_ids)).limit(1))
  if task_id or signal_id or note_id:
   raise HTTPException(409,'No se puede revertir este lote: contiene registros con tareas, notas o señales comerciales. Revísalos manualmente.')
 # FK batch_mutations.record_id -> crm_records.id has NO ON DELETE CASCADE.
 # Preserve the import_batches summary and audit, but delete mutation rows
 # BEFORE their created parent records in the same transaction.
 targets={}
 for change in changes:
  target=db.scalar(select(CRMRecord).where(
   CRMRecord.id==change.record_id,CRMRecord.tenant_id==user.tenant_id))
  if not target:raise HTTPException(409,'Registro modificado o eliminado; revisar manualmente')
  targets[change.id]=target
 db.query(BatchMutation).filter(
  BatchMutation.batch_id==batch_id,BatchMutation.tenant_id==user.tenant_id
 ).delete(synchronize_session=False)
 db.flush()
 for change in reversed(changes):
  target=targets[change.id]
  if change.operation=='create':
   db.query(RecordGrant).filter(
    RecordGrant.tenant_id==user.tenant_id,RecordGrant.record_id==target.id
   ).delete(synchronize_session=False)
   db.query(CRMLink).filter(CRMLink.tenant_id==user.tenant_id,
    ((CRMLink.record_id==target.id)|(CRMLink.related_id==target.id))
   ).delete(synchronize_session=False)
   db.delete(target)
  elif change.operation=='update':
   for field,value in json.loads(change.previous).items():setattr(target,field,value)
  else:
   raise HTTPException(409,'Tipo de mutación desconocido; reversión cancelada')
 batch.status='rolled_back'
 record_audit(db,user,'migration.rollback',batch.id,{'mutations':len(changes),
  'source':batch.source,'kind':batch.kind})
 try:db.commit()
 except IntegrityError as exc:
  db.rollback()
  raise HTTPException(409,'No es seguro revertir: hay referencias a registros de este lote. Revisa tareas, señales o importaciones relacionadas.') from exc
 return {'status':'rolled_back','mutations':len(changes)}
@app.get('/api/v1/records')
def records(kind:Literal['companies','contacts','deals','activities']='contacts',limit:int=50,db:Session=Depends(get_db),user:User=Depends(principal)):
 if limit<1 or limit>200:raise HTTPException(422,'Límite entre 1 y 200')
 rows=db.scalars(select(CRMRecord).where(visible_record_condition(user),CRMRecord.kind==kind).order_by(CRMRecord.created_at.desc()).limit(limit)).all()
 return [{'id':r.id,'external_id':r.external_id,'source':r.source,'name':r.name,'email':r.email,'company_external_id':r.company_external_id,'stage':r.stage,'owner':r.owner,'attributes':json.loads(r.attributes)} for r in rows]
@app.get('/api/v1/migrations/summary')
def summary(db:Session=Depends(get_db),user:User=Depends(admin)):
 return {kind:db.scalar(select(func.count()).select_from(CRMRecord).where(CRMRecord.tenant_id==user.tenant_id,CRMRecord.kind==kind)) for kind in KINDS}

@app.get('/api/v1/records/{record_id}')
def record_detail(record_id:str,db:Session=Depends(get_db),user:User=Depends(principal)):
 r=readable_record(db,user,record_id)
 if not r:raise HTTPException(404,'Registro no encontrado')
 return {'id':r.id,'kind':r.kind,'name':r.name,'email':r.email,'phone':r.phone,
         'source':r.source,'external_id':r.external_id,'stage':r.stage,
         'company_external_id':r.company_external_id,'contact_external_id':r.contact_external_id,
         'deal_external_id':r.deal_external_id,'attributes':json.loads(r.attributes)}

class RecordAccessIn(Strict):
 user_id:str=Field(min_length=1,max_length=36)

@app.put('/api/v1/admin/records/{record_id}/access')
def grant_record_access(record_id:str,data:RecordAccessIn,db:Session=Depends(get_db),user:User=Depends(admin)):
 r=db.scalar(select(CRMRecord.id).where(CRMRecord.tenant_id==user.tenant_id,CRMRecord.id==record_id))
 rep=db.scalar(select(User).where(User.tenant_id==user.tenant_id,User.id==data.user_id,User.active==True,User.role=='rep'))
 if not r or not rep:raise HTTPException(404,'Registro o comercial no encontrado')
 key=(user.tenant_id,rep.id,record_id)
 if db.get(RecordGrant,key):
  return {'status':'already_granted'}
 db.add(RecordGrant(tenant_id=user.tenant_id,user_id=rep.id,record_id=record_id))
 record_audit(db,user,'record.access.grant',record_id,{'rep_id':rep.id})
 db.commit()
 return {'status':'granted'}

@app.delete('/api/v1/admin/records/{record_id}/access/{rep_id}')
def revoke_record_access(record_id:str,rep_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 grant=db.get(RecordGrant,(user.tenant_id,rep_id,record_id))
 if not grant:raise HTTPException(404,'Concesión no encontrada')
 # Revoking while an open task still exposes its title via Focus; fail closed.
 pending=db.scalar(select(func.count()).select_from(WorkItem).where(
  WorkItem.tenant_id==user.tenant_id,WorkItem.owner_id==rep_id,
  WorkItem.record_id==record_id,WorkItem.state.in_(['open','active'])))
 if pending:raise HTTPException(409,'Reasigna o cierra primero las tareas abiertas del comercial')
 db.delete(grant)
 record_audit(db,user,'record.access.revoke',record_id,{'rep_id':rep_id})
 db.commit()
 return {'status':'revoked'}

class NewRep(Strict):username:str=Field(min_length=3,max_length=120);password:str=Field(min_length=14,max_length=200)
@app.post('/api/v1/admin/reps')
def create_rep(data:NewRep,db:Session=Depends(get_db),user:User=Depends(admin)):
 if db.scalar(select(User).where(User.username==data.username)):raise HTTPException(409,'Usuario ya existe')
 rep=User(id=uid(),tenant_id=user.tenant_id,username=data.username,password_hash=hash_password(data.password),role='rep')
 db.add(rep);record_audit(db,user,'rep.create',rep.id,{})
 db.commit();return {'id':rep.id,'username':rep.username}
@app.get('/api/v1/admin/reps')
def list_reps(db:Session=Depends(get_db),user:User=Depends(admin)):
 reps=db.scalars(select(User).where(User.tenant_id==user.tenant_id,User.role=='rep',User.active==True)).all()
 return [{'id':r.id,'username':r.username} for r in reps]
@app.get('/api/v1/migrations/relationships')
def relationships(source:str='pipedrive',db:Session=Depends(get_db),user:User=Depends(admin)):
 if not re.fullmatch(r'[A-Za-z0-9_-]{2,80}',source):raise HTTPException(422,'Proveedor inválido')
 return reconcile(db,user.tenant_id,source,persist=False)

class ReconcileApproval(Strict):
 expected_snapshot:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')

@app.post('/api/v1/migrations/relationships/reconcile')
def persist_relationships(data:ReconcileApproval,source:str='pipedrive',
                          db:Session=Depends(get_db),user:User=Depends(admin)):
 if not re.fullmatch(r'[A-Za-z0-9_-]{2,80}',source):raise HTTPException(422,'Proveedor inválido')
 lock_tenant_crm(db,user.tenant_id)
 report=reconcile(db,user.tenant_id,source,persist=True,expected_snapshot=data.expected_snapshot)
 record_audit(db,user,'migration.reconcile',source,{k:v for k,v in report.items() if k!='unresolved'})
 db.commit()
 return report

@app.get('/api/v1/records/{record_id}/links')
def record_links(record_id:str,db:Session=Depends(get_db),user:User=Depends(principal)):
 record=readable_record(db,user,record_id)
 if not record:raise HTTPException(404,'Registro no encontrado')
 query=select(CRMLink).where(CRMLink.tenant_id==user.tenant_id,CRMLink.record_id==record_id)
 if user.role!='admin':
  query=query.where(CRMLink.related_id.in_(select(CRMRecord.id).where(visible_record_condition(user))))
 rows=db.scalars(query).all()
 return [{'relation':r.relation,'record_id':r.related_id} for r in rows]
