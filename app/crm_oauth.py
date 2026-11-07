"""Tenant-scoped OAuth 2.0 authorization-code + PKCE for CRM sandbox onboarding.

The callback never reads tokens or the tenant DB: it posts code/state only to
the same-origin opener, which completes via an authenticated admin API call.
No live provider tests or production readiness claimed.
"""
import base64
import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse
from html import escape

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import admin, uid, utcnow, aware, record_audit
from .db import get_db
from .models import CRMOAuthPending, CRMOAuthConnection, User
from .relationships import lock_tenant_crm

router=APIRouter(prefix='/api/v1/oauth',tags=['CRM OAuth'])
PROVIDERS=('pipedrive','hubspot','salesforce')
OAUTH_HOSTS={'pipedrive':'oauth.pipedrive.com',
             'hubspot':'app.hubspot.com',
             'salesforce':'login.salesforce.com'}
TOKEN_ENDPOINTS={'pipedrive':'https://oauth.pipedrive.com/oauth/token',
 'hubspot':'https://api.hubapi.com/oauth/v1/token',
 'salesforce':'https://login.salesforce.com/services/oauth2/token'}
SCOPES={'hubspot':'crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read',
        'salesforce':'api refresh_token','pipedrive':''}

class Strict(BaseModel):
 model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class Complete(Strict):
 provider:str
 code:str=Field(min_length=8,max_length=2048)
 state:str=Field(min_length=20,max_length=256)

def cipher():
 raw=os.getenv('REVENUE_OS_OAUTH_ENCRYPTION_KEY','')
 try:return Fernet(raw.encode('ascii'))
 except (ValueError,TypeError) as exc:
  raise HTTPException(503,'El cifrado OAuth no está configurado') from exc

def encrypt(value):return cipher().encrypt(value.encode('utf-8')).decode('ascii')
def decrypt(value):
 try:return cipher().decrypt(value.encode('ascii')).decode('utf-8')
 except (InvalidToken,ValueError,UnicodeError,TypeError) as exc:
  raise HTTPException(503,'Credencial OAuth no accesible') from exc

def oauth_config(provider):
 if provider not in PROVIDERS:raise HTTPException(422,'Proveedor CRM no soportado')
 if os.getenv('REVENUE_OS_ENABLE_CRM_OAUTH')!='1':
  raise HTTPException(503,'OAuth CRM no habilitado')
 prefix='REVENUE_OS_'+provider.upper()+'_'
 client_id=os.getenv(prefix+'CLIENT_ID','')
 client_secret=os.getenv(prefix+'CLIENT_SECRET','')
 callback=os.getenv('REVENUE_OS_OAUTH_CALLBACK_URL','')
 parsed=urlparse(callback)
 if (not 6<=len(client_id)<=200 or not 12<=len(client_secret)<=500
     or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
     or parsed.query or parsed.fragment or parsed.path!='/api/v1/oauth/callback'):
  raise HTTPException(503,'Configuración OAuth incompleta')
 if not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}',parsed.hostname):
  raise HTTPException(503,'Redirect OAuth inválido')
 if parsed.hostname in ('localhost','127.0.0.1') or parsed.hostname.endswith('.local'):
  raise HTTPException(503,'OAuth exige HTTPS público para este modo')
 return client_id,client_secret,callback

@router.post('/{provider}/start')
def start(provider:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 client_id,_,callback=oauth_config(provider)
 verifier=secrets.token_urlsafe(48)
 state=secrets.token_urlsafe(40)
 challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
 lock_tenant_crm(db,user.tenant_id)
 db.add(CRMOAuthPending(id=uid(),tenant_id=user.tenant_id,actor_id=user.id,
  provider=provider,state_hash=hashlib.sha256(state.encode()).hexdigest(),
  verifier_ciphertext=encrypt(verifier),expires_at=utcnow()+timedelta(minutes=10)))
 record_audit(db,user,'crm.oauth.start',provider,{'provider':provider})
 db.commit()
 query={'client_id':client_id,'redirect_uri':callback,'response_type':'code',
        'state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
 if provider=='hubspot':query['scope']=SCOPES['hubspot']
 if provider=='salesforce':query['scope']=SCOPES['salesforce']
 path={'pipedrive':'/oauth/authorize','hubspot':'/oauth/authorize','salesforce':'/services/oauth2/authorize'}[provider]
 return {'authorization_url':'https://'+OAUTH_HOSTS[provider]+path+'?'+urlencode(query),
         'expires_in_seconds':600,'provider':provider}

@router.get('/callback',response_class=HTMLResponse,include_in_schema=False)
def callback():
 # Static no secrets; code is removed from visible URL immediately in the popup.
 return HTMLResponse("""<!doctype html><html lang="es"><head><meta charset="utf-8">
 <meta name="referrer" content="no-referrer"><title>Revenue OS OAuth</title></head>
 <body><p id="status">Autorización recibida. Vuelve a Revenue OS.</p>
 <script>
 const url=new URL(location.href);
 const code=url.searchParams.get('code'),state=url.searchParams.get('state');
 const error=url.searchParams.get('error');
 history.replaceState(null,'',location.pathname);
 if(window.opener && window.opener!==window){
  window.opener.postMessage({type:'revenue-os-crm-oauth',code,state,error},location.origin);
  window.close();
 }else document.getElementById('status').textContent='Regresa a Revenue OS e inicia la conexión desde su panel de administración.';
 </script></body></html>""",headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'})

def exchange(provider,client_id,client_secret,callback,code,verifier):
 endpoint=TOKEN_ENDPOINTS[provider]
 data={'grant_type':'authorization_code','code':code,
       'redirect_uri':callback,'client_id':client_id,'client_secret':client_secret,
       'code_verifier':verifier}
 try:
  with httpx.Client(timeout=httpx.Timeout(12.0,connect=5.0),follow_redirects=False,trust_env=False) as client:
   if provider=='pipedrive':
    data.pop('client_id',None);data.pop('client_secret',None)
    response=client.post(endpoint,data=data,auth=(client_id,client_secret),headers={'Accept':'application/json'})
   else:
    response=client.post(endpoint,data=data,headers={'Accept':'application/json'})
  if response.status_code!=200 or len(response.content)>65536:
   raise HTTPException(502,'Intercambio OAuth rechazado por el proveedor')
  result=response.json()
 except (httpx.HTTPError,ValueError) as exc:
  raise HTTPException(502,'No se pudo verificar la autorización OAuth') from exc
 if not isinstance(result,dict):raise HTTPException(502,'Respuesta OAuth inválida')
 token=result.get('access_token');refresh=result.get('refresh_token')
 if not isinstance(token,str) or not 12<=len(token)<=8192:
  raise HTTPException(502,'Token OAuth inválido')
 if refresh is not None and (not isinstance(refresh,str) or len(refresh)>8192):
  raise HTTPException(502,'Token de renovación inválido')
 api_base={'hubspot':'https://api.hubapi.com','salesforce':result.get('instance_url'),
           'pipedrive':result.get('api_domain')}[provider]
 if provider=='pipedrive' and not api_base:
  raise HTTPException(502,'Falta dominio API Pipedrive')
 if provider=='salesforce' and not api_base:
  raise HTTPException(502,'Falta instancia Salesforce')
 if not isinstance(api_base,str):raise HTTPException(502,'Dominio API inválido')
 parsed=urlparse(api_base)
 allowed=(provider=='hubspot' and parsed.hostname=='api.hubapi.com') or (
  provider=='pipedrive' and parsed.hostname and parsed.hostname.endswith('.pipedrive.com')) or (
  provider=='salesforce' and parsed.hostname and (parsed.hostname.endswith('.salesforce.com') or parsed.hostname.endswith('.force.com')))
 if not allowed or parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
  raise HTTPException(502,'Dominio API externo no permitido')
 expires=result.get('expires_in')
 try:expires=int(expires) if expires is not None else None
 except (TypeError,ValueError):raise HTTPException(502,'Expiración OAuth inválida')
 if expires is not None and not 30<=expires<=31536000:
  raise HTTPException(502,'Expiración OAuth fuera de rango')
 # Do not mistake a rotating access token for an account identity.
 account_hint=(parsed.hostname if provider=='pipedrive' else
               result.get('hub_id') if provider=='hubspot' else
               result.get('id')) or 'not_verified'
 return token,refresh,api_base.rstrip('/'),expires,str(account_hint)[:180]

@router.post('/complete')
def complete(data:Complete,db:Session=Depends(get_db),user:User=Depends(admin)):
 provider=data.provider
 client_id,client_secret,callback_url=oauth_config(provider)
 digest=hashlib.sha256(data.state.encode()).hexdigest()
 # Tenant and user are derived from the admin session, never callback parameters.
 lock_tenant_crm(db,user.tenant_id)
 pending=db.scalar(select(CRMOAuthPending).where(
  CRMOAuthPending.tenant_id==user.tenant_id,
  CRMOAuthPending.actor_id==user.id,
  CRMOAuthPending.provider==provider,CRMOAuthPending.state_hash==digest
 ).with_for_update())
 if not pending:raise HTTPException(404,'Estado OAuth inexistente o de otro usuario')
 if aware(pending.expires_at)<=utcnow():
  db.delete(pending);db.commit();raise HTTPException(410,'Autorización caducada')
 verifier=decrypt(pending.verifier_ciphertext)
 # Consume the state before contacting provider. A failed exchange requires restart.
 db.delete(pending);db.commit()
 token,refresh,api_base,expires,account_key=exchange(provider,client_id,client_secret,callback_url,data.code,verifier)
 lock_tenant_crm(db,user.tenant_id)
 existing=db.get(CRMOAuthConnection,(user.tenant_id,provider))
 if existing:
  if existing.account_key not in ('not_verified',account_key) and account_key!='not_verified':
   raise HTTPException(409,'Identidad externa distinta: desconecta antes de cambiar de cuenta')
 else:
  existing=CRMOAuthConnection(tenant_id=user.tenant_id,provider=provider,
   account_key=account_key,token_ciphertext='',api_base=api_base)
  db.add(existing)
 existing.token_ciphertext=encrypt(token)
 existing.refresh_ciphertext=encrypt(refresh) if refresh else None
 existing.api_base=api_base
 existing.account_key=account_key
 existing.scopes=SCOPES[provider]
 existing.expires_at=utcnow()+timedelta(seconds=expires) if expires else None
 record_audit(db,user,'crm.oauth.connect',provider,{'provider':provider,'account_identified':account_key!='not_verified'})
 db.commit()
 return {'provider':provider,'connected':True,'identity_verified':account_key!='not_verified'}

@router.get('/connections')
def connections(db:Session=Depends(get_db),user:User=Depends(admin)):
 rows=db.scalars(select(CRMOAuthConnection).where(
  CRMOAuthConnection.tenant_id==user.tenant_id)).all()
 return [{'provider':row.provider,'connected':True,'account_identified':row.account_key!='not_verified',
          'scopes':row.scopes,'expires_at':aware(row.expires_at).isoformat() if row.expires_at else None}
         for row in rows]

@router.delete('/connections/{provider}')
def disconnect(provider:str,db:Session=Depends(get_db),user:User=Depends(admin)):
 if provider not in PROVIDERS:raise HTTPException(422,'Proveedor inválido')
 lock_tenant_crm(db,user.tenant_id)
 row=db.get(CRMOAuthConnection,(user.tenant_id,provider))
 if not row:raise HTTPException(404,'Conexión inexistente')
 db.delete(row);record_audit(db,user,'crm.oauth.disconnect',provider,{'provider':provider})
 db.commit()
 # Revokes local access only; provider-side revocation requires provider integration.
 return {'disconnected':True,'remote_revocation':False}
