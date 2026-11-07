from datetime import datetime,timedelta,timezone
from uuid import uuid4
from .conftest import login

def setup(api):
 ha=login(api);hb=login(api,'admin-b')
 a=api.post('/api/v1/admin/reps',headers=ha,json={'username':'seller-a','password':'long-test-password-1234'}).json()['id']
 b=api.post('/api/v1/admin/reps',headers=hb,json={'username':'seller-b','password':'long-test-password-1234'}).json()['id']
 ra=login(api,'seller-a');rb=login(api,'seller-b')
 return ha,hb,a,b,ra,rb

def new(api,h,owner,offset=0,title='Contact client',source='inbound'):
 due=(datetime.now(timezone.utc)+timedelta(minutes=offset)).isoformat()
 return api.post('/api/v1/work/tasks',headers=h,json={'owner_id':owner,'title':title,'channel':'call','priority_source':source,'due_at':due,'idem':'testtask-'+uuid4().hex})

def test_guided_mode_priority_and_no_skip(api):
 h,_,owner,_,rep,_=setup(api)
 f=new(api,h,owner,60);assert f.status_code==200
 p=new(api,h,owner,-15,source='proposal');assert p.status_code==200
 q=api.get('/api/v1/work/queue',headers=rep).json()
 assert len(q['ready'])==1 and q['ready'][0]['id']==p.json()['id']
 assert api.post('/api/v1/work/claim/'+f.json()['id'],headers=rep).status_code==409
 assert api.post('/api/v1/work/claim/'+p.json()['id'],headers=rep).status_code==200
 assert api.get('/api/v1/work/queue',headers=rep).json()['locked']
 assert api.post('/api/v1/work/claim/'+f.json()['id'],headers=rep).status_code==409
 payload={'task_id':p.json()['id'],'idem':'unique-action-100','outcome':'contacted','note':'Talked'}
 complete=api.post('/api/v1/work/complete',headers=rep,json=payload)
 assert complete.status_code==200,complete.text
 assert api.post('/api/v1/work/complete',headers=rep,json=payload).json()['already_recorded']
 assert api.get('/api/v1/work/queue',headers=rep).json()['ready']==[]

def test_scheduled_tasks_visible_to_assignee_but_not_executable_or_cross_tenant(api):
 h,hb,owner,other_owner,rep,other_rep=setup(api)
 due_later=new(api,h,owner,90,title='Preparar llamada agendada')
 assert due_later.status_code==200,due_later.text
 task_id=due_later.json()['id']
 queue=api.get('/api/v1/work/queue',headers=rep)
 assert queue.status_code==200,queue.text
 data=queue.json()
 assert data['ready']==[]
 assert data['active'] is None
 assert data['upcoming_truncated'] is False
 scheduled=next(x for x in data['upcoming'] if x['id']==task_id)
 assert scheduled['title']=='Preparar llamada agendada'
 assert scheduled['state']=='scheduled' and scheduled['executable'] is False
 assert api.post('/api/v1/work/claim/'+task_id,headers=rep).status_code==409
 forbidden=api.get('/api/v1/work/queue',headers=other_rep)
 assert forbidden.status_code==200
 assert not any(x['id']==task_id for x in forbidden.json()['upcoming'])
 overdue=new(api,h,owner,-10,title='Acción vencida')
 assert overdue.status_code==200,overdue.text
 queue=api.get('/api/v1/work/queue',headers=rep).json()
 assert overdue.json()['id'] in {x['id'] for x in queue['ready']}
 assert overdue.json()['id'] in {x['id'] for x in queue['queued']}
 assert all(x['executable_from_list'] is False for x in queue['queued'])
 assert task_id in {x['id'] for x in queue['upcoming']}
 waiting=new(api,h,owner,-1,title='Acción secundaria esperando')
 assert waiting.status_code==200,waiting.text
 assert api.post('/api/v1/work/claim/'+overdue.json()['id'],headers=rep).status_code==200
 locked=api.get('/api/v1/work/queue',headers=rep).json()
 assert locked['locked'] is True
 assert locked['ready']==[]
 assert waiting.json()['id'] in {x['id'] for x in locked['queued']}
 assert task_id in {x['id'] for x in locked['upcoming']}
 assert not any(x['id']==overdue.json()['id'] for x in locked['queued'])


def test_admin_controls_mode_and_tenant(api):
 h,hb,owner,b,rep,rb=setup(api)
 pol={'mode':'pipeline','timezone':'Europe/Madrid','weights':{'overdue':5,'inbound':15,'proposal':10,'stale':5}}
 assert api.put('/api/v1/work/policy',headers=rep,json=pol).status_code==403
 assert api.put('/api/v1/work/policy',headers=h,json=pol).status_code==200
 assert api.get('/api/v1/work/queue',headers=rep).status_code==403
 assert api.get('/api/v1/work/queue',headers=rb).status_code==200
 assert new(api,h,b,-5).status_code==422
 assert api.post('/api/v1/work/release/'+b,headers=h).status_code==404

def test_follow_up_required_for_record(api):
 h,_,owner,_,rep,_=setup(api)
 data=b'ID,Name,Email\np1,Alice,a@example.com\n'
 p=api.post('/api/v1/migrations/preview',headers=h,data={'kind':'contacts'},files={'file':('c.csv',data)}).json()
 import json
 spec={'kind':'contacts','source':'pipedrive','digest':p['digest'],'columns':{'external_id':'ID','name':'Name','email':'Email'}}
 assert api.post('/api/v1/migrations/commit',headers=h,data={'spec':json.dumps(spec)},files={'file':('c.csv',data)}).status_code==200
 rec=api.get('/api/v1/records',headers=h).json()[0]['id']
 due=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
 t=api.post('/api/v1/work/tasks',headers=h,json={'owner_id':owner,'record_id':rec,'title':'Call Alice','channel':'call','due_at':due,'idem':'task-alice-'+uuid4().hex}).json()['id']
 assert api.post('/api/v1/work/claim/'+t,headers=rep).status_code==200
 payload={'task_id':t,'idem':'followup-unique-123','outcome':'contacted'}
 assert api.post('/api/v1/work/complete',headers=rep,json=payload).status_code==422
 payload.update({'next_title':'Send proposal','next_due_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()})
 r=api.post('/api/v1/work/complete',headers=rep,json=payload)
 assert r.status_code==200,r.text
 assert api.get('/api/v1/work/queue',headers=rep).json()['locked']==False

def test_release_audit(api):
 h,_,owner,_,rep,_=setup(api)
 t=new(api,h,owner,-5).json()['id']
 assert api.post('/api/v1/work/claim/'+t,headers=rep).status_code==200
 assert api.post('/api/v1/work/release/'+owner,headers=h).status_code==200
 assert api.get('/api/v1/work/queue',headers=rep).json()['ready'][0]['id']==t

def test_force_next_rejected_even_when_both_ready(api):
 h,_,owner,_,rep,_=setup(api)
 older=new(api,h,owner,-120,source='proposal').json()['id']
 newer=new(api,h,owner,-1,source='inbound').json()['id']
 ready=api.get('/api/v1/work/queue',headers=rep).json()['ready']
 assert ready[0]['id']==older
 assert api.post('/api/v1/work/claim/'+newer,headers=rep).status_code==409
 assert api.post('/api/v1/work/claim/'+older,headers=rep).status_code==200

def test_no_cross_tenant_task_creation(api):
 h,hb,owner,b,rep,rb=setup(api)
 a=new(api,h,owner,-30).json()['id']
 assert api.post('/api/v1/work/claim/'+a,headers=rb).status_code==404
 assert api.post('/api/v1/work/release/'+owner,headers=hb).status_code==404
