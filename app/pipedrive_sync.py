"""Pipedrive API v2 read-only connector.

Controlled sandbox integration. No OAuth, webhook, email or customer data enabled
by default. API tokens live only in operator-provided environment variables.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import admin, aware, record_audit, utcnow, uid
from .db import get_db
from .models import CRMRecord, CRMSyncCheckpoint, User
from .relationships import lock_tenant_crm

router=APIRouter(prefix='/api/v1/integrations/pipedrive',tags=['CRM integrations'])
KINDS={'companies':'organizations','contacts':'persons','deals':'deals','activities':'activities'}
PAGE_LIMIT=100
MAX_PAGES=5
MAX_BYTES=2*1024*1024
OVERLAP=timedelta(minutes=2)
class Strict(BaseModel):
 model_config=ConfigDict(extra='forbid')
class SyncRequest(Strict):
 kind:str
 confirm:bool=False

def connection_for(tenant_id:str):
 if os.getenv('REVENUE_OS_ENABLE_PIPEDRIVE_SYNC')!='1':
  raise HTTPException(503,'Sincronización externa deshabilitada')
 try:
  mapping=json.loads(os.getenv('REVENUE_OS_PIPEDRIVE_CONNECTIONS_JSON','{}'))
  entry=mapping[tenant_id]
  domain=entry['subdomain'];variable=entry['token_env']
 except (KeyError,TypeError,ValueError,AttributeError) as exc:
  raise HTTPException(503,'Conexión Pipedrive no aprovisionada para esta empresa') from exc
 if (not isinstance(domain,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',domain)
     or not isinstance(variable,str) or not re.fullmatch(r'REVENUE_OS_PD_TOKEN_[A-Z0-9_]{1,60}',variable)):
  raise HTTPException(503,'Configuración del conector inválida')
 token=os.getenv(variable,'')
 if len(token)<15 or len(token)>512:
  raise HTTPException(503,'Credencial de integración no aprovisionada')
 return domain,token

def scalar(value):
 if value is None or isinstance(value,bool):return None
 if isinstance(value,(str,int,float)):return str(value)[:180]
 if isinstance(value,dict):return scalar(value.get('value') or value.get('id'))
 return None

def first_contact(value):
 if isinstance(value,list) and value:
  return first_contact(value[0])
 return scalar(value)

def normalize(kind,obj):
 if not isinstance(obj,dict):raise HTTPException(502,'Respuesta CRM malformada')
 external=scalar(obj.get('id'))
 if not external or not re.fullmatch(r'[0-9]+',external):
  raise HTTPException(502,'Identificador externo de CRM inválido')
 name=scalar(obj.get('subject' if kind=='activities' else 'title' if kind=='deals' else 'name'))
 if not name:raise HTTPException(502,'Registro CRM sin nombre/título')
 org=scalar(obj.get('org_id'))
 person=scalar(obj.get('person_id'))
 deal=scalar(obj.get('deal_id'))
 status=scalar(obj.get('status'))
 stage_id=scalar(obj.get('stage_id'))
 updated=scalar(obj.get('update_time'))
 # Do not interpret stage_id as a human pipeline-stage label.
 custom={'origin':'pipedrive_api_v2','update_time':updated,
         'deal_status':status if kind=='deals' else None,
         'stage_id':stage_id if kind=='deals' else None,
         'activity_done':obj.get('done') if kind=='activities' and isinstance(obj.get('done'),bool) else None}
 payload={
  'name':name[:300],
  'email':first_contact(obj.get('emails'))[:320] if kind=='contacts' and first_contact(obj.get('emails')) else None,
  'phone':first_contact(obj.get('phones'))[:80] if kind=='contacts' and first_contact(obj.get('phones')) else None,
  'company_external_id':org if kind in ('contacts','deals','activities') else None,
  'contact_external_id':person if kind in ('deals','activities') else None,
  'deal_external_id':deal if kind=='activities' else None,
  'stage':('pipedrive_stage_id:'+stage_id)[:120] if kind=='deals' and stage_id else None,
  'owner':scalar(obj.get('owner_id')),
  'amount':scalar(obj.get('value')) if kind=='deals' else None,
  'due_at':scalar(obj.get('due_date')) if kind=='activities' else None,
  'attributes':json.dumps(custom,sort_keys=True,ensure_ascii=False,separators=(',',':')),
 }
 return external,payload

def pull_pages(domain,token,kind,since,until):
 endpoint=KINDS[kind]
 params={'limit':PAGE_LIMIT,'updated_until':until.isoformat().replace('+00:00','Z')}
 if since:params['updated_since']=since.isoformat().replace('+00:00','Z')
 result=[];seen=set();cursor=None
 base=f'https://{domain}.pipedrive.com/api/v2/{endpoint}'
 # Fixed hostname + path; redirect handling explicitly disabled (no credential forwarding).
 with httpx.Client(timeout=httpx.Timeout(12.0,connect=5.0),follow_redirects=False,trust_env=False) as client:
  for page in range(MAX_PAGES):
   if cursor:params['cursor']=cursor
   try:
    with client.stream('GET',base,params=params,headers={'x-api-token':token,'Accept':'application/json'}) as resp:
     if resp.status_code==429:raise HTTPException(503,'Pipedrive ha limitado temporalmente las consultas')
     if resp.status_code!=200:raise HTTPException(502,'No se ha podido consultar Pipedrive')
     body=bytearray()
     for block in resp.iter_bytes():
      body.extend(block)
      if len(body)>MAX_BYTES:raise HTTPException(502,'Respuesta CRM excede el límite permitido')
    payload=json.loads(body)
   except (httpx.HTTPError,ValueError,UnicodeError) as exc:
    raise HTTPException(502,'La respuesta de Pipedrive no pudo verificarse') from exc
   if not isinstance(payload,dict) or payload.get('success') is not True or not isinstance(payload.get('data'),list):
    raise HTTPException(502,'Contrato inesperado de Pipedrive')
   page_items=payload['data']
   if len(page_items)>PAGE_LIMIT:raise HTTPException(502,'Límite de página inesperado')
   for obj in page_items:
    external,normalized=normalize(kind,obj)
    if external in seen:raise HTTPException(502,'Paginación CRM duplicada o inconsistente')
    seen.add(external);result.append((external,normalized))
   extra=payload.get('additional_data') or {}
   if not isinstance(extra,dict):raise HTTPException(502,'Paginación CRM inválida')
   next_cursor=extra.get('next_cursor')
   if not next_cursor:
    return result
   if not isinstance(next_cursor,str) or len(next_cursor)>2048 or next_cursor==cursor:
    raise HTTPException(502,'Cursor de paginación inválido')
   cursor=next_cursor
 raise HTTPException(413,'Más de 500 registros en la ventana; ampliar con paginación persistente antes de sincronizar')

@router.get('/status')
def status(db:Session=Depends(get_db),user:User=Depends(admin)):
 rows=db.scalars(select(CRMSyncCheckpoint).where(
  CRMSyncCheckpoint.tenant_id==user.tenant_id,CRMSyncCheckpoint.provider=='pipedrive')).all()
 return {'enabled':os.getenv('REVENUE_OS_ENABLE_PIPEDRIVE_SYNC')=='1',
  'connection_configured':configured_for(user.tenant_id),
  'checkpoints':{r.kind:{'last_completed_until':aware(r.last_completed_until).isoformat(),
                         'last_completed_at':aware(r.last_completed_at).isoformat(),
                         'imported_count':r.imported_count} for r in rows}}

def configured_for(tenant_id):
 try:connection_for(tenant_id);return True
 except HTTPException:return False

@router.post('/sync')
def sync(data:SyncRequest,db:Session=Depends(get_db),user:User=Depends(admin)):
 if data.kind not in KINDS:raise HTTPException(422,'Entidad CRM no soportada')
 domain,token=connection_for(user.tenant_id)
 watermark=db.get(CRMSyncCheckpoint,(user.tenant_id,'pipedrive',data.kind))
 previous=aware(watermark.last_completed_until) if watermark else None
 since=previous-OVERLAP if previous else None
 until=utcnow()-timedelta(minutes=1)
 if previous and until<=previous+timedelta(minutes=1):
  raise HTTPException(409,'Sincronización demasiado reciente')
 data_rows=pull_pages(domain,token,data.kind,since,until)
 if not data.confirm:
  return {'preview':True,'kind':data.kind,'count':len(data_rows),
          'window_start':since.isoformat() if since else None,'window_end':until.isoformat(),
          'warning':'Previsualización no vinculante: confirmar vuelve a consultar al proveedor.'}
 # Serialize writes under tenant lock and reject a concurrent or stale sync window.
 lock_tenant_crm(db,user.tenant_id)
 checkpoint=db.get(CRMSyncCheckpoint,(user.tenant_id,'pipedrive',data.kind))
 actual=aware(checkpoint.last_completed_until) if checkpoint else None
 if actual!=previous:raise HTTPException(409,'Otra sincronización actualizó esta entidad; vuelve a previsualizar')
 if checkpoint and actual>=until:raise HTTPException(409,'Ventana anterior o ya sincronizada')
 counts={'created':0,'updated':0,'unchanged':0}
 try:
  for external,values in data_rows:
   record=db.scalar(select(CRMRecord).where(CRMRecord.tenant_id==user.tenant_id,
    CRMRecord.source=='pipedrive',CRMRecord.kind==data.kind,CRMRecord.external_id==external))
   if record:
    try:origin=json.loads(record.attributes or '{}').get('origin')
    except (ValueError,TypeError,AttributeError):origin=None
    if origin!='pipedrive_api_v2':
     raise HTTPException(409,'Registro Pipedrive existente con origen distinto; requiere revisión manual')
    if all(getattr(record,key)==value for key,value in values.items()):
     counts['unchanged']+=1;continue
    for key,value in values.items():setattr(record,key,value)
    counts['updated']+=1
   else:
    db.add(CRMRecord(id=uid(),tenant_id=user.tenant_id,kind=data.kind,
      source='pipedrive',external_id=external,**values))
    counts['created']+=1
  if not checkpoint:
   checkpoint=CRMSyncCheckpoint(tenant_id=user.tenant_id,provider='pipedrive',kind=data.kind,
                                last_completed_until=until,last_completed_at=utcnow(),imported_count=len(data_rows))
   db.add(checkpoint)
  else:
   checkpoint.last_completed_until=until;checkpoint.last_completed_at=utcnow()
   checkpoint.imported_count=len(data_rows)
  record_audit(db,user,'pipedrive.sync',data.kind,{
   'counts':counts,'window_end':until.isoformat(),'kind':data.kind,
   'source':'pipedrive','externally_verified_connection':True})
  db.commit()
 except Exception:
  db.rollback();raise
 return {'preview':False,'kind':data.kind,**counts,'window_end':until.isoformat(),
  'reconciliation_required':data.kind!='companies',
  'warning':'Las referencias externas se concilian aparte; no se crean grants ni señales no comprobadas.'}
