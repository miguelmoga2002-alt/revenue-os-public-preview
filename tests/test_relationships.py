import json
from .conftest import login

def test_relationship_report(api):
 h=login(api)
 def load(kind,content,mapping):
  data=content.encode()
  p=api.post('/api/v1/migrations/preview',headers=h,data={'kind':kind},files={'file':('x.csv',data)}).json()
  r=api.post('/api/v1/migrations/commit',headers=h,data={'spec':json.dumps({'kind':kind,'source':'pipedrive','columns':mapping,'digest':p['digest']})},files={'file':('x.csv',data)})
  assert r.status_code==200,r.text
 load('companies','ID,Name\nO1,Acme\n',{'external_id':'ID','name':'Name'})
 load('contacts','ID,Name,Organization ID\nP1,Alice,O1\nP2,Bob,O2\n',{'external_id':'ID','name':'Name','company_external_id':'Organization ID'})
 report=api.get('/api/v1/migrations/relationships?source=pipedrive',headers=h).json()
 assert report['resolved_references']==1 and report['unresolved_count']==1
 assert report['unresolved'][0]['missing_reference']=='O2'
 assert api.get('/api/v1/migrations/relationships?source=pipedrive',headers=login(api,'admin-b')).json()['resolved_references']==0
