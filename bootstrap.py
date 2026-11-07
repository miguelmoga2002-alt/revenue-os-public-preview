"""Create initial tenant/admin after Alembic upgrade head."""
import getpass,sys
from app.db import SessionLocal
from app.models import Tenant,User
from app.auth import uid,hash_password
from sqlalchemy import select
if len(sys.argv)!=3:
 print('Usage: python bootstrap.py <tenant-slug> <admin-username>');raise SystemExit(2)
tenant,username=sys.argv[1:]
if not tenant.replace('-','').isalnum():raise SystemExit('Invalid tenant slug')
pw=getpass.getpass('Password (14+ chars): ')
if len(pw)<14:raise SystemExit('Password too short')
with SessionLocal.begin() as db:
 if db.scalar(select(User).where(User.username==username)):raise SystemExit('Username already taken')
 if not db.get(Tenant,tenant):db.add(Tenant(id=tenant,name=tenant))
 db.add(User(id=uid(),tenant_id=tenant,username=username,password_hash=hash_password(pw),role='admin'))
print('Created initial admin. Never share credentials.')
