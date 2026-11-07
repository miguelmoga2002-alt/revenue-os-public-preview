import io,json,zipfile
from .conftest import login

def csv_bytes(rows):return rows.encode('utf-8')
def preview(api,headers,data,kind='contacts',file='contacts.csv'):
 return api.post('/api/v1/migrations/preview',headers=headers,data={'kind':kind},files={'file':(file,data,'text/csv')})
def commit(api,headers,data,digest,mapping=None,kind='contacts',source='pipedrive',strategy='skip'):
 mapping=mapping or {'external_id':'ID','name':'Name','email':'Email'}
 spec={'kind':kind,'source':source,'columns':mapping,'strategy':strategy,'digest':digest}
 return api.post('/api/v1/migrations/commit',headers=headers,data={'spec':json.dumps(spec)},files={'file':('upload.csv',data,'text/csv')})

def test_login_roles_and_tenant_isolation(api):
 data=csv_bytes('ID,Name,Email\np1,Alfa,alfa@example.com\n')
 ha=login(api);hb=login(api,'admin-b')
 p=preview(api,ha,data).json()
 r=commit(api,ha,data,p['digest']);assert r.status_code==200,r.text
 assert len(api.get('/api/v1/records',headers=ha).json())==1
 assert api.get('/api/v1/records',headers=hb).json()==[]
 assert api.get('/api/v1/migrations/batches',headers=hb).json()==[]
 assert api.get('/api/v1/records').status_code==401
 rep=login(api,'rep-a')
 assert preview(api,rep,data).status_code==403
 assert api.post('/api/v1/migrations/commit',headers=rep,data={'spec':'{}'},files={'file':('x.csv',data)}).status_code==403

def test_preview_mapping_extra_fields_and_idempotency(api):
 h=login(api)
 data=csv_bytes('ID,Name,Email,My custom field\np1,Alice,alice@example.com,IA leasing\np2,Bob,bob@example.com,Other\n')
 p=preview(api,h,data);assert p.status_code==200,p.text
 assert p.json()['total_rows']==2
 assert p.json()['suggested_mapping']['external_id']=='ID'
 d=p.json()['digest']
 r=commit(api,h,data,d);assert r.status_code==200,r.text
 assert r.json()['created']==2
 r2=commit(api,h,data,d);assert r2.status_code==200,r2.text
 assert r2.json()['skipped']==2
 records=api.get('/api/v1/records',headers=h).json()
 assert len(records)==2
 assert {r['attributes']['My custom field'] for r in records}=={'IA leasing','Other'}

def test_atomic_invalid_and_changed_file(api):
 h=login(api)
 data=csv_bytes('ID,Name,Email\np1,Alice,a@example.com\np2,,b@example.com\n')
 d=preview(api,h,data).json()['digest']
 r=commit(api,h,data,d)
 assert r.status_code==422
 assert api.get('/api/v1/records',headers=h).json()==[]
 changed=data+b'\n'
 assert commit(api,h,changed,d).status_code==409

def test_duplicate_ids_rejected(api):
 h=login(api)
 data=csv_bytes('ID,Name,Email\np1,Alice,a@example.com\np1,Bob,b@example.com\n')
 d=preview(api,h,data).json()['digest']
 assert commit(api,h,data,d).status_code==422

def test_upsert_and_batch_rollback(api):
 h=login(api)
 data=csv_bytes('ID,Name,Email\np1,Alice,a@example.com\n')
 d=preview(api,h,data).json()['digest']
 batch1=commit(api,h,data,d).json()['batch_id']
 changed=csv_bytes('ID,Name,Email\np1,Alicia,new@example.com\n')
 d2=preview(api,h,changed).json()['digest']
 batch2=commit(api,h,changed,d2,strategy='upsert').json()['batch_id']
 assert api.post('/api/v1/migrations/'+batch1+'/rollback',headers=h).status_code==409
 assert api.post('/api/v1/migrations/'+batch2+'/rollback',headers=h).status_code==200
 assert api.get('/api/v1/records',headers=h).json()[0]['name']=='Alice'
 assert api.post('/api/v1/migrations/'+batch1+'/rollback',headers=h).status_code==200
 assert api.get('/api/v1/records',headers=h).json()==[]

def test_relations_and_kinds_preserved(api):
 h=login(api)
 data=csv_bytes('Deal ID,Title,Value,Stage,Person ID,Organization ID,Owner,Campaign\nD1,Deal A,42000,Proposal,P1,O1,Eva,Search\n')
 d=preview(api,h,data,'deals').json()['digest']
 mapping={'external_id':'Deal ID','name':'Title','amount':'Value','stage':'Stage','contact_external_id':'Person ID','company_external_id':'Organization ID','owner':'Owner'}
 r=commit(api,h,data,d,mapping,'deals');assert r.status_code==200,r.text
 rec=api.get('/api/v1/records?kind=deals',headers=h).json()[0]
 assert rec['company_external_id']=='O1' and rec['attributes']['Campaign']=='Search'

def test_logout(api):
 h=login(api)
 assert api.delete('/api/v1/auth/session',headers=h).status_code==200
 assert api.get('/api/v1/me',headers=h).status_code==401

def test_malformed_input_and_limits(api):
 h=login(api)
 assert preview(api,h,b'abcd','contacts','contacts.txt').status_code==415
 assert preview(api,h,csv_bytes('ID,ID\n1,2\n')).status_code==422
 data=csv_bytes('ID,Name\n1,Alice\n')
 assert commit(api,h,data,'0'*64,{'external_id':'ID','name':'Name'}).status_code==409

def make_xlsx():
 ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
 body=f'''<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ID</t></is></c><c r="B1" t="inlineStr"><is><t>Name</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>23</t></is></c><c r="B2" t="inlineStr"><is><t>Alice</t></is></c></row></sheetData></worksheet>'''
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:z.writestr('xl/worksheets/sheet1.xml',body)
 return out.getvalue()

def test_xlsx_preview(api):
 h=login(api)
 x=make_xlsx()
 r=api.post('/api/v1/migrations/preview',headers=h,data={'kind':'contacts'},files={'file':('contacts.xlsx',x,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
 assert r.status_code==200,r.text
 assert r.json()['preview'][0]['Name']=='Alice'


def test_oversized_csv_cell_rejected_without_silent_truncation(api):
 h=login(api)
 oversized='X'*10001
 data=csv_bytes('ID,Name\np-oversized,'+oversized+'\n')
 response=preview(api,h,data)
 assert response.status_code==422,response.text
 assert '10000 caracteres' in response.json()['detail']
 assert api.get('/api/v1/records',headers=h).json()==[]


def test_oversized_xlsx_cell_rejected_without_silent_truncation(api):
 h=login(api)
 ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
 content=f'''<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ID</t></is></c><c r="B1" t="inlineStr"><is><t>Name</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>23</t></is></c><c r="B2" t="inlineStr"><is><t>{'X'*10001}</t></is></c></row></sheetData></worksheet>'''
 out=io.BytesIO()
 with zipfile.ZipFile(out,'w') as z:
  z.writestr('xl/worksheets/sheet1.xml',content)
 response=api.post('/api/v1/migrations/preview',headers=h,data={'kind':'contacts'},files={'file':('oversized.xlsx',out.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
 assert response.status_code==422,response.text
 assert '10000 caracteres' in response.json()['detail']
 assert api.get('/api/v1/records',headers=h).json()==[]
