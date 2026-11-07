"""Runtime security gates for production-like deployments.

Only verifiable application configuration is checked here. A successful check
does NOT imply TLS, WAF, IAM, KMS, backup or security monitoring are deployed.
"""
import os
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

def _valid_fernet(value):
 if not value:return False
 try:
  Fernet(value.encode('ascii'))
  return True
 except (ValueError,TypeError,UnicodeError):
  return False

def production_blockers(env=None):
 """Return safe diagnostic codes, never raw credentials or connection URLs."""
 settings=os.environ if env is None else env
 if settings.get('REVENUE_OS_ENV','development').lower()!='production':
  return []
 issues=[]
 if settings.get('REVENUE_OS_REQUIRE_ADMIN_MFA','1')=='0':
  issues.append('admin_mfa_disabled')
 public=settings.get('REVENUE_OS_PUBLIC_ORIGIN','')
 try:
  url=urlsplit(public)
  valid=(url.scheme=='https' and bool(url.hostname) and not url.username
    and not url.password and not url.path and not url.query and not url.fragment
    and public==public.rstrip('/'))
 except ValueError:valid=False
 if not valid:issues.append('public_https_origin_required')
 database=settings.get('REVENUE_OS_DATABASE_URL','')
 if not database.startswith(('postgresql+psycopg://','postgresql://')):
  issues.append('postgresql_runtime_required')
 else:
  try:
   username=urlsplit(database.replace('postgresql+psycopg','postgresql',1)).username
   if username!='revenue_app':issues.append('restricted_database_role_required')
  except ValueError:
   issues.append('restricted_database_role_required')
 mfa=settings.get('REVENUE_OS_MFA_ENCRYPTION_KEY','')
 if not _valid_fernet(mfa):issues.append('mfa_encryption_key_required')
 if settings.get('REVENUE_OS_ENABLE_PIPEDRIVE_SYNC','0')=='1':
  issues.append('legacy_pipedrive_api_key_not_approved_for_production')
 if settings.get('REVENUE_OS_ENABLE_CRM_OAUTH','0')=='1':
  oauth=settings.get('REVENUE_OS_OAUTH_ENCRYPTION_KEY','')
  if not _valid_fernet(oauth):issues.append('oauth_encryption_key_required')
  if oauth==mfa and oauth:issues.append('distinct_encryption_keys_required')
  callback=settings.get('REVENUE_OS_OAUTH_CALLBACK_URL','')
  if callback!=public+'/api/v1/oauth/callback':
   issues.append('oauth_callback_must_match_public_origin')
 return issues

def require_secure_runtime():
 problems=production_blockers()
 if problems:
  # No values or URL userinfo in the error. Fail closed before serving requests.
  raise RuntimeError('Revenue OS insecure production configuration: '+', '.join(problems))
