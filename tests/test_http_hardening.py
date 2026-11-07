"""Regression tests for browser CSP, origin controls and secret-safe responses."""
import base64
import hashlib
import re
from app.http_hardening import SCRIPT_PATTERN

def test_all_rendered_pages_hash_pinned_and_not_cached(api):
 for path in ('/','/focus','/crm','/deals/00000000-0000-4000-8000-000000000001','/api/v1/oauth/callback'):
  response=api.get(path)
  assert response.status_code==200,response.text
  assert response.headers['Cache-Control']=='no-store'
  csp=response.headers['Content-Security-Policy']
  assert "default-src 'none'" in csp
  assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
  assert "'unsafe-eval'" not in csp
  assert "script-src 'self' 'sha256-" in csp
  # Every inline script hash in the response must be covered by CSP.
  scripts=SCRIPT_PATTERN.findall(response.content)
  assert len(scripts)==1
  expected="'sha256-"+base64.b64encode(hashlib.sha256(scripts[0]).digest()).decode('ascii')+"'"
  assert expected in csp
  assert 'X-Frame-Options' in response.headers

def test_deal_deep_link_serves_no_data_without_auth_and_rejects_bad_id(api):
 # The page shell is public, not the CRM record or the tenant's secrets.
 valid='00000000-0000-4000-8000-000000000001'
 page=api.get('/deals/'+valid)
 assert page.status_code==200
 assert b'Acceso al CRM' in page.content
 assert b'Cuenta ficticia privada' not in page.content
 assert page.headers['Cache-Control']=='no-store'
 assert page.headers['Content-Security-Policy']
 # Invalid UUIDs never become a fallback to unscoped data.
 assert api.get('/deals/not-a-uuid').status_code in (404,422)
 assert api.get('/api/v1/crm/'+valid+'/360').status_code==401


def test_cross_site_browser_write_rejected_without_tenant_state(api):
 # Unauthorized and authenticated paths are blocked before reaching the API.
 endpoint='/api/v1/auth/login'
 malicious='https://external.attacker.invalid'
 response=api.post(endpoint,headers={'Origin':malicious},
                   json={'username':'anyone','password':'irrelevant'})
 assert response.status_code==403
 assert 'permitido' in response.json()['detail']
 # A credentialed admin endpoint also rejects browser cross-origin writes.
 response=api.post('/api/v1/oauth/hubspot/start',headers={'Origin':malicious})
 assert response.status_code==403
 # Non-browser REST clients without an Origin are unaffected (auth still checked).
 assert api.post('/api/v1/oauth/hubspot/start').status_code==401

def test_hsts_only_under_tls_and_no_csp_for_json(api):
 page=api.get('/crm')
 assert 'Strict-Transport-Security' not in page.headers
 api_response=api.get('/health')
 assert api_response.status_code==200
 assert 'Content-Security-Policy' not in api_response.headers
 assert api_response.headers['X-Content-Type-Options']=='nosniff'

def test_release_candidate_metadata_consistent_between_health_and_openapi(api):
 from app.main import APP_VERSION
 health=api.get('/health')
 schema=api.get('/openapi.json')
 assert health.status_code==200 and schema.status_code==200
 assert APP_VERSION=='1.1.0-rc.1'
 assert health.json()['status']=='up'
 assert health.json()['version']==APP_VERSION
 assert schema.json()['info']['version']==APP_VERSION
 assert 'producci' in health.json()['warning'].lower()


def test_origin_validation_exact_scheme_and_host(monkeypatch,api):
 from app.http_hardening import allowed_browser_origin
 from starlette.requests import Request
 monkeypatch.setenv('REVENUE_OS_PUBLIC_ORIGIN','https://revenue.example.invalid')
 def request(origin):
  scope={'type':'http','method':'POST','path':'/api/v1/auth/login',
         'scheme':'http','server':('testserver',80),
         'headers':[(b'origin',origin.encode())]}
  return Request(scope)
 assert allowed_browser_origin(request('https://revenue.example.invalid'))
 assert not allowed_browser_origin(request('http://revenue.example.invalid'))
 assert not allowed_browser_origin(request('https://revenue.example.invalid.attacker.test'))
 assert not allowed_browser_origin(request('null'))

def test_windows_crlf_checkout_produces_browser_compatible_csp(monkeypatch,tmp_path):
 """Windows Git CRLF checkouts must hash the inline JS that HTML parsing runs."""
 from app import http_hardening
 script=b'\r\nlet value = 1;\r\nwindow.value = value;\r\n'
 html=b'<html><script>'+script+b'</script></html>'
 (tmp_path/'page.html').write_bytes(html)
 monkeypatch.setattr(http_hardening,'WEB_ROOT',tmp_path)
 got=http_hardening.script_hashes('page.html')
 normalized=script.replace(b'\r\n',b'\n').replace(b'\r',b'\n')
 expected="'sha256-"+base64.b64encode(hashlib.sha256(normalized).digest()).decode('ascii')+"'"
 assert got==[expected]
 assert expected != "'sha256-"+base64.b64encode(hashlib.sha256(script).digest()).decode('ascii')+"'"
