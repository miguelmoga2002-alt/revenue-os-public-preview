"""Attribution, corrections and deterministic NBA signals, isolated SQLite fixture."""
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from .conftest import login
from .test_crm_workspace import load

def seed(api):
 admin,rep=login(api),login(api,'rep-a')
 load(api,admin,'contacts','ID,Name\nS1,Test Signal Lead\n',
      {'external_id':'ID','name':'Name'})
 record=api.get('/api/v1/records',headers=admin).json()[0]['id']
 owner=next(r['id'] for r in api.get('/api/v1/admin/reps',headers=admin).json()
            if r['username']=='rep-a')
 from datetime import datetime,timedelta,timezone
 due=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
 payload={'owner_id':owner,'record_id':record,'title':'Llamar','channel':'call',
          'due_at':due,'idem':'sig-task-'+uuid4().hex}
 task=api.post('/api/v1/work/tasks',headers=admin,json=payload)
 assert task.status_code==200,task.text
 return admin,rep,record,task.json()['id'],owner

def test_focused_outcome_becomes_attributed_signal_and_changes_nba(api):
 admin,rep,record,first,owner=seed(api)
 assert api.post('/api/v1/work/claim/'+first,headers=rep).status_code==200
 response=api.post('/api/v1/work/complete',headers=rep,json={
  'task_id':first,'idem':'sig-complete-'+uuid4().hex,'outcome':'no_answer',
  'next_title':'Intentar de nuevo','next_due_at':(
   datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()})
 assert response.status_code==200,response.text
 records=api.get('/api/v1/signals/records/'+record,headers=rep)
 assert records.status_code==200
 items=records.json()['items']
 assert len(items)==1
 assert items[0]['source_type']=='work_result'
 assert items[0]['verification']=='recorded_action'
 assert items[0]['kind']=='no_answer'
 assert items[0]['status']=='current'
 ready=api.get('/api/v1/work/queue',headers=rep).json()['ready']
 assert len(ready)==1
 assert ready[0]['score_breakdown']['verified_signal']==12
 assert any(row['rule']=='recent_no_answer' and row['source_ref']==items[0]['source_ref']
            for row in ready[0]['evidence'])
 assert ready[0]['score']==sum(ready[0]['score_breakdown'].values())
 # Correct the user's logged outcome without altering the original WorkResult;
 # the superseded event must no longer influence the recommendation.
 corrected=api.post('/api/v1/signals/'+items[0]['id']+'/correct',headers=rep,json={
  'kind':'contacted','occurred_at':datetime.now(timezone.utc).isoformat(),
  'detail':'Rectificación: sí hubo respuesta',
  'idem':'sig-correct-work-result-'+uuid4().hex})
 assert corrected.status_code==200,corrected.text
 assert corrected.json()['signal']['verification']=='user_attested'
 history=api.get('/api/v1/signals/records/'+record,headers=rep).json()['items']
 assert len(history)==2 and any(x['source_type']=='work_result' and
                               x['status']=='superseded' for x in history)
 afterwards=api.get('/api/v1/work/queue',headers=rep).json()['ready'][0]
 assert afterwards['score_breakdown']['verified_signal']==0


def test_manual_signal_idempotence_append_correction_and_cross_tenant(api):
 admin,rep,record,_,_=seed(api)
 foreign=login(api,'rep-b')
 when=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
 data={'record_id':record,'kind':'proposal_sent','occurred_at':when,
       'detail':'Confirmado verbalmente por comercial','idem':'signal-rep-1-operation'}
 assert api.post('/api/v1/signals/reports',headers=foreign,json=data).status_code==404
 assert api.get('/api/v1/signals/records/'+record,headers=foreign).status_code==404
 first=api.post('/api/v1/signals/reports',headers=rep,json=data)
 assert first.status_code==200,first.text
 assert first.json()['signal']['verification']=='user_attested'
 again=api.post('/api/v1/signals/reports',headers=rep,json=data)
 assert again.status_code==200 and again.json()['already_recorded']
 assert api.post('/api/v1/signals/reports',headers=rep,json={**data,'kind':'meeting_held'}).status_code==409
 signal_id=first.json()['signal']['id']
 corrected={'kind':'meeting_held','occurred_at':when,'detail':'Reunión declarada',
            'idem':'signal-rep-correction-1'}
 assert api.post(f'/api/v1/signals/{signal_id}/correct',headers=foreign,json=corrected).status_code==404
 fixed=api.post(f'/api/v1/signals/{signal_id}/correct',headers=rep,json=corrected)
 assert fixed.status_code==200,fixed.text
 assert fixed.json()['signal']['supersedes_id']==signal_id
 assert api.post(f'/api/v1/signals/{signal_id}/correct',headers=rep,
                 json={**corrected,'idem':'different-correction'}).status_code==409
 assert api.post(f'/api/v1/signals/{signal_id}/correct',headers=rep,
                 json=corrected).json()['already_recorded']
 history=api.get('/api/v1/signals/records/'+record,headers=rep).json()['items']
 assert len(history)==2 and {row['status'] for row in history}=={'superseded','current'}
 assert len([x for x in history if x['status']=='current'])==1
 assert all(x['source_type']=='manual_report' for x in history)
 assert api.get('/api/v1/signals/records/'+record).status_code==401

def test_no_future_unverified_or_unowned_report(api):
 admin,rep,record,_,_=seed(api)
 payload={'record_id':record,'kind':'meeting_held','detail':'',
          'idem':'sig-future-event','occurred_at':(
          datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
 assert api.post('/api/v1/signals/reports',headers=rep,json=payload).status_code==422
 assert api.post('/api/v1/signals/reports',headers=rep,json={
  **payload,'occurred_at':datetime.now(timezone.utc).isoformat(),
  'source_type':'gmail'}).status_code==422
 assert api.get('/api/v1/signals/records/not-a-record',headers=rep).status_code==404
