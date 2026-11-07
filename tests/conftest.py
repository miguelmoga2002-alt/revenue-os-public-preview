import os,tempfile
from pathlib import Path
from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def api(tmp_path,monkeypatch):
 monkeypatch.setenv('REVENUE_OS_REQUIRE_ADMIN_MFA','0')
 import app.db as db
 from sqlalchemy import create_engine
 from sqlalchemy.orm import sessionmaker
 from app.models import Base,Tenant,User
 from app.auth import hash_password,uid
 from app.main import app
 from app.db import get_db
 engine=create_engine('sqlite:///'+str(tmp_path/'test.db'),connect_args={'check_same_thread':False})
 Base.metadata.create_all(engine)
 Session=sessionmaker(bind=engine,expire_on_commit=False)
 with Session.begin() as s:
  for t in ('a','b'):
   s.add(Tenant(id=t,name=t))
   s.add(User(id=uid(),tenant_id=t,username='admin-'+t,role='admin',password_hash=hash_password('long-test-password-1234')))
   s.add(User(id=uid(),tenant_id=t,username='rep-'+t,role='rep',password_hash=hash_password('long-test-password-1234')))
 def override():
  with Session() as s:yield s
 app.dependency_overrides[get_db]=override
 app.state.test_session_factory=Session
 with TestClient(app) as c:yield c
 del app.state.test_session_factory
 app.dependency_overrides.clear()
 engine.dispose()

def login(api,user='admin-a'):
 r=api.post('/api/v1/auth/login',json={'username':user,'password':'long-test-password-1234'})
 assert r.status_code==200,r.text
 return {'Authorization':'Bearer '+r.json()['access_token']}
