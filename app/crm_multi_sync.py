"""Read-only OAuth CRM ingestion: HubSpot 2026-03, Salesforce REST and Pipedrive v2.

No background or customer data flow is enabled by default. Max 500 rows per
request. Provider objects are canonicalized into CRMRecord without inventing
commercial signals from activity status.
"""
import json
import re
from datetime import timedelta
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import admin, uid, utcnow, aware, record_audit
from .db import get_db
from .models import CRMOAuthConnection, CRMRecord, CRMSyncCheckpoint, User
from .relationships import lock_tenant_crm
from .crm_oauth import decrypt, encrypt, oauth_config, TOKEN_ENDPOINTS
from . import pipedrive_sync as pipedrive

router=APIRouter(prefix='/api/v1/integrations/crm',tags=['Multi CRM Migration'])
ENTITIES=('companies','contacts','deals','activities')
MAX_PAGES=5
PAGE_SIZE=100
MAX_BYTES=2*1024*1024
SF_OBJECTS={'companies':'Account','contacts':'Contact','deals':'Opportunity','activities':'Task'}
SF_FIELDS={
 'companies':'Id,Name,LastModifiedDate',
 'contacts':'Id,Name,Email,Phone,AccountId,LastModifiedDate',
 'deals':'Id,Name,Amount,StageName,AccountId,LastModifiedDate',
 'activities':'Id,Subject,Status,ActivityDate,WhoId,WhatId,LastModifiedDate'}
HS_PROPS={
 'companies':'name,hs_lastmodifieddate',
 'contacts':'firstname,lastname,email,phone,associatedcompanyid,lastmodifieddate',
 'deals':'dealname,dealstage,amount,hs_lastmodifieddate'}
class Strict(BaseModel):
 model_config=ConfigDict(extra='forbid')
class SyncInput(Strict):
 provider:str
 kind:str
 confirm:bool=False

def strval(value,limit=180):
 if value is None or isinstance(value,(dict,list,bool)):return None
 return str(value)[:limit]

def normalized(provider,kind,obj):
 if provider=='pipedrive':
  return pipedrive.normalize(kind,obj)
 if not isinstance(obj,dict):raise HTTPException(502,'Objeto CRM inválido')
 if provider=='hubspot':
  external=strval(obj.get('id'))
  props=obj.get('properties')
  if not isinstance(props,dict):raise HTTPException(502,'Propiedades HubSpot inválidas')
  name=(props.get('name') if kind=='companies' else
       ' '.join(str(props.get(k) or '') for k in ('firstname','lastname')).strip() if kind=='contacts' else
       props.get('dealname'))
  company=None # HubSpot associations must be queried separately; do not infer.
  related_contact=None;deal_external=None
  stage=('hubspot_stage_id:'+str(props['dealstage']))[:120] if kind=='deals' and props.get('dealstage') else None
  status=None
  email=strval(props.get('email'),320) if kind=='contacts' else None
  phone=strval(props.get('phone'),80) if kind=='contacts' else None
  amount=strval(props.get('amount'),80) if kind=='deals' else None
  updated=strval(obj.get('updatedAt') or props.get('hs_lastmodifieddate'))
 else:
  external=strval(obj.get('Id'))
  name=obj.get('Name') if kind!='activities' else obj.get('Subject')
  company=strval(obj.get('AccountId')) if kind in ('contacts','deals') else None
  related_contact=strval(obj.get('WhoId')) if kind=='activities' else None
  deal_external=strval(obj.get('WhatId')) if kind=='activities' else None
  # WhatId can be Account, Opportunity or another object. Never auto-link it
  # to a deal without verifying the Salesforce ID prefix/schema.
  deal_external=None
  stage=('salesforce_stage:'+str(obj['StageName']))[:120] if kind=='deals' and obj.get('StageName') else None
  email=strval(obj.get('Email'),320) if kind=='contacts' else None
  phone=strval(obj.get('Phone'),80) if kind=='contacts' else None
  amount=strval(obj.get('Amount'),80) if kind=='deals' else None
  updated=strval(obj.get('LastModifiedDate'))
  status=strval(obj.get('Status')) if kind=='activities' else None
 if not external or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',external) or not isinstance(name,str) or not name.strip():
  raise HTTPException(502,'ID o nombre CRM inválido')
 properties={'origin':provider+'_oauth','updated_at_source':updated,'activity_status':status}
 return external,dict(
  name=name[:300],email=email,phone=phone,company_external_id=company,
  contact_external_id=related_contact,deal_external_id=deal_external,
  stage=stage,owner=None,amount=amount,due_at=strval(obj.get('ActivityDate'),80) if provider=='salesforce' and kind=='activities' else None,
  attributes=json.dumps(properties,ensure_ascii=False,sort_keys=True,separators=(',',':')))

def checked_get(client,url,params,token,expected_host):
 parsed=urlparse(url)
 if parsed.scheme!='https' or parsed.hostname!=expected_host or parsed.username or parsed.password or parsed.fragment:
  raise HTTPException(502,'URL externa no permitida')
 try:
  with client.stream('GET',url,params=params,headers={'Authorization':'Bearer '+token,
     'Accept':'application/json',**({'Sforce-Query-Options':'batchSize=200'} if expected_host.endswith(('.salesforce.com','.force.com')) else {})}) as response:
   if response.status_code in (401,403):raise HTTPException(409,'Acceso CRM revocado o insuficiente')
   if response.status_code==429:raise HTTPException(503,'Límite de API CRM alcanzado')
   if response.status_code!=200:raise HTTPException(502,'La API CRM no pudo consultarse')
   raw=bytearray()
   for chunk in response.iter_bytes():
    raw.extend(chunk)
    if len(raw)>MAX_BYTES:raise HTTPException(502,'Respuesta externa excesiva')
  payload=json.loads(raw)
 except (httpx.HTTPError,ValueError,UnicodeError) as exc:
  raise HTTPException(502,'Respuesta CRM no válida') from exc
 if not isinstance(payload,dict):raise HTTPException(502,'Contrato CRM no válido')
 return payload

def pull_pages(provider,base,token,kind,since,until):
 if kind not in ENTITIES:raise HTTPException(422,'Entidad no soportada')
 if provider=='hubspot' and kind=='activities':
  raise HTTPException(422,'Actividades HubSpot pendientes de permisos y esquema de asociaciones')
 if provider not in ('hubspot','salesforce','pipedrive'):
  raise HTTPException(422,'Proveedor CRM no soportado')
 results=[];seen=set();cursor=None
 parsed=urlparse(base)
 with httpx.Client(timeout=httpx.Timeout(12.0,connect=5.0),
                   follow_redirects=False,trust_env=False) as client:
  for page in range(MAX_PAGES):
   if provider=='hubspot':
    url='https://api.hubapi.com/crm/objects/2026-03/'+kind
    params={'limit':PAGE_SIZE,'properties':HS_PROPS[kind],'archived':'false'}
    if cursor:params['after']=cursor
   elif provider=='salesforce':
    if cursor:
     if not re.fullmatch(r'/services/data/v66[.]0/query/[A-Za-z0-9_-]{1,2048}',cursor):
      raise HTTPException(502,'Cursor Salesforce no permitido')
     url=base+cursor;params=None
    else:
     clauses=[]
     if since:clauses.append("LastModifiedDate >= "+since.strftime('%Y-%m-%dT%H:%M:%SZ'))
     clauses.append("LastModifiedDate <= "+until.strftime('%Y-%m-%dT%H:%M:%SZ'))
     soql='SELECT '+SF_FIELDS[kind]+' FROM '+SF_OBJECTS[kind]+' WHERE '+' AND '.join(clauses)+' ORDER BY LastModifiedDate ASC'
     url=base+'/services/data/v66.0/query';params={'q':soql}
   else:
    url=base+'/api/v2/'+pipedrive.KINDS[kind]
    params={'limit':PAGE_SIZE,'updated_until':until.isoformat().replace('+00:00','Z')}
    if since:params['updated_since']=since.isoformat().replace('+00:00','Z')
    if cursor:params['cursor']=cursor
   response=checked_get(client,url,params,token,parsed.hostname)
   if provider=='hubspot':
    values=response.get('results')
    nxt=(response.get('paging') or {}).get('next') or {}
    next_cursor=nxt.get('after')
   elif provider=='salesforce':
    values=response.get('records')
    next_cursor=None if response.get('done') is True else response.get('nextRecordsUrl')
    if response.get('done') not in (True,False):raise HTTPException(502,'Paginación Salesforce inválida')
   else:
    if response.get('success') is not True:raise HTTPException(502,'Respuesta Pipedrive rechazada')
    values=response.get('data');next_cursor=(response.get('additional_data') or {}).get('next_cursor')
   if not isinstance(values,list) or len(values)>(200 if provider=='salesforce' else PAGE_SIZE):
    # Salesforce server-selected batch can be up to 2,000; refuse >100 until
    # a dedicated paginated streaming import is designed.
    raise HTTPException(502,'Tamaño de lote no soportado')
   for raw in values:
    external,fields=normalized(provider,kind,raw)
    if external in seen:raise HTTPException(502,'ID CRM duplicado en paginación')
    seen.add(external);results.append((external,fields))
    if len(results)>500:raise HTTPException(413,'Se supera el límite de 500 registros; requiere backfill paginado')
   if not next_cursor:return results
   if not isinstance(next_cursor,str) or not 1<=len(next_cursor)<=2048 or next_cursor==cursor:
    raise HTTPException(502,'Cursor CRM inválido')
   cursor=next_cursor
 raise HTTPException(413,'Se supera el límite de 500 registros; requiere backfill paginado')

def access_token(db,connection):
 if connection.expires_at and aware(connection.expires_at)<=utcnow()+timedelta(seconds=60):
  if not connection.refresh_ciphertext:raise HTTPException(409,'Conexión CRM caducada: vuelve a autorizar')
  client_id,secret,_=oauth_config(connection.provider)
  form={'grant_type':'refresh_token','refresh_token':decrypt(connection.refresh_ciphertext)}
  if connection.provider!='pipedrive':
   form.update({'client_id':client_id,'client_secret':secret})
  try:
   with httpx.Client(timeout=12,follow_redirects=False,trust_env=False) as client:
    response=client.post(TOKEN_ENDPOINTS[connection.provider],data=form,auth=(client_id,secret) if connection.provider=='pipedrive' else None)
   if response.status_code!=200 or len(response.content)>65536:raise HTTPException(409,'Renovación OAuth rechazada')
   value=response.json()
  except (httpx.HTTPError,ValueError) as exc:raise HTTPException(502,'Error de renovación OAuth') from exc
  token=value.get('access_token')
  if not isinstance(token,str) or not 12<=len(token)<=8192:raise HTTPException(502,'Token renovado inválido')
  connection.token_ciphertext=encrypt(token)
  if value.get('refresh_token'):connection.refresh_ciphertext=encrypt(value['refresh_token'])
  try:seconds=int(value['expires_in'])
  except (ValueError,TypeError,KeyError):raise HTTPException(502,'Sin expiración renovada')
  if not 30<=seconds<=31536000:raise HTTPException(502,'Expiración renovada fuera de rango')
  connection.expires_at=utcnow()+timedelta(seconds=seconds)
  db.flush()
 return decrypt(connection.token_ciphertext)

@router.get('/providers')
def providers(db:Session=Depends(get_db),user:User=Depends(admin)):
 rows=db.scalars(select(CRMOAuthConnection).where(CRMOAuthConnection.tenant_id==user.tenant_id)).all()
 return {'providers':[{'name':provider,'connected':any(row.provider==provider for row in rows),
                        'kinds':list(ENTITIES if provider!='hubspot' else ENTITIES[:3])}
                       for provider in ('pipedrive','hubspot','salesforce')],
         'readonly':True}

@router.post('/sync')
def sync(data:SyncInput,db:Session=Depends(get_db),user:User=Depends(admin)):
 if data.provider not in ('pipedrive','hubspot','salesforce') or data.kind not in ENTITIES:
  raise HTTPException(422,'Proveedor o entidad desconocidos')
 if data.provider=='hubspot' and data.kind=='activities':
  raise HTTPException(422,'Actividades HubSpot aún no admitidas')
 lock_tenant_crm(db,user.tenant_id)
 connection=db.get(CRMOAuthConnection,(user.tenant_id,data.provider))
 if not connection:raise HTTPException(409,'Primero conecta el CRM con OAuth')
 if connection.account_key=='not_verified':raise HTTPException(409,'Identidad de cuenta CRM no verificada; no se permite importar')
 token=access_token(db,connection)
 cp=db.get(CRMSyncCheckpoint,(user.tenant_id,data.provider,data.kind))
 previous=aware(cp.last_completed_until) if cp else None
 until=utcnow()-timedelta(minutes=1)
 # HubSpot listing is a full scan, not incremental: do NOT claim the watermark
 # filters remote rows. Limit to 500 until search-by-updatedAt is implemented.
 since=None if data.provider=='hubspot' else previous-timedelta(minutes=2) if previous else None
 if data.provider!='hubspot' and previous and until<=previous+timedelta(minutes=1):
  raise HTTPException(409,'Sincronización demasiado reciente')
 rows=pull_pages(data.provider,connection.api_base,token,data.kind,since,until)
 if not data.confirm:
  db.rollback()  # refresh not persisted until a confirmed operation
  return {'preview':True,'provider':data.provider,'kind':data.kind,'count':len(rows),
          'limited':len(rows)>=500,'incremental_filter_applied':data.provider!='hubspot'}
 counts={'created':0,'updated':0,'unchanged':0}
 for external,values in rows:
  record=db.scalar(select(CRMRecord).where(
   CRMRecord.tenant_id==user.tenant_id,CRMRecord.source==data.provider,
   CRMRecord.kind==data.kind,CRMRecord.external_id==external))
  if record:
   try:origin=json.loads(record.attributes or '{}').get('origin')
   except (ValueError,TypeError,AttributeError):origin=None
   allowed_origins=(data.provider+'_oauth','pipedrive_api_v2') if data.provider=='pipedrive' else (data.provider+'_oauth',)
   if origin not in allowed_origins:
    raise HTTPException(409,'Registro de origen CSV/manual: requiere conciliación antes de sobrescribir')
   if all(getattr(record,k)==v for k,v in values.items()):counts['unchanged']+=1
   else:
    for k,v in values.items():setattr(record,k,v)
    counts['updated']+=1
  else:
   db.add(CRMRecord(id=uid(),tenant_id=user.tenant_id,source=data.provider,
    kind=data.kind,external_id=external,**values));counts['created']+=1
 if not cp:
  cp=CRMSyncCheckpoint(tenant_id=user.tenant_id,provider=data.provider,
   kind=data.kind,last_completed_until=until,imported_count=len(rows));db.add(cp)
 else:cp.last_completed_until=until;cp.last_completed_at=utcnow();cp.imported_count=len(rows)
 record_audit(db,user,'crm.oauth.sync',data.provider,{'kind':data.kind,'counts':counts,'source':data.provider})
 db.commit()
 return {'preview':False,'provider':data.provider,'kind':data.kind,**counts,
  'relationship_reconciliation_required':data.kind!='companies',
  'warning':'Las asociaciones, eliminaciones y señales requieren verificación y reglas adicionales.'}
