"""Revenue OS v1.0 entities. Migration data is scoped by tenant at every query boundary."""
from datetime import datetime, timezone
from sqlalchemy import String, ForeignKey, ForeignKeyConstraint, DateTime, Integer, Boolean, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def utcnow():return datetime.now(timezone.utc)
class Base(DeclarativeBase):pass
class Tenant(Base):
 __tablename__='tenants'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 name:Mapped[str]=mapped_column(String(200))
class User(Base):
 __tablename__='users'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 username:Mapped[str]=mapped_column(String(120),unique=True)
 password_hash:Mapped[str]=mapped_column(Text)
 role:Mapped[str]=mapped_column(String(20),default='rep')
 active:Mapped[bool]=mapped_column(Boolean,default=True)
 __table_args__=(UniqueConstraint('tenant_id','id',name='uq_user_tenant_id'),)
class Session(Base):
 __tablename__='sessions'
 token_hash:Mapped[str]=mapped_column(String(64),primary_key=True)
 user_id:Mapped[str]=mapped_column(ForeignKey('users.id'))
 expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
 revoked:Mapped[bool]=mapped_column(Boolean,default=False)
class CRMRecord(Base):
 __tablename__='crm_records'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 kind:Mapped[str]=mapped_column(String(20)) # companies, contacts, deals, activities
 source:Mapped[str]=mapped_column(String(80))
 external_id:Mapped[str]=mapped_column(String(180))
 name:Mapped[str]=mapped_column(String(300))
 email:Mapped[str|None]=mapped_column(String(320),nullable=True)
 phone:Mapped[str|None]=mapped_column(String(80),nullable=True)
 company_external_id:Mapped[str|None]=mapped_column(String(180),nullable=True)
 contact_external_id:Mapped[str|None]=mapped_column(String(180),nullable=True)
 deal_external_id:Mapped[str|None]=mapped_column(String(180),nullable=True)
 stage:Mapped[str|None]=mapped_column(String(120),nullable=True)
 owner:Mapped[str|None]=mapped_column(String(160),nullable=True)
 amount:Mapped[str|None]=mapped_column(String(80),nullable=True)
 due_at:Mapped[str|None]=mapped_column(String(80),nullable=True)
 attributes:Mapped[str]=mapped_column(Text,default='{}')
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
 updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)
 __table_args__=(UniqueConstraint('tenant_id','kind','source','external_id',name='uq_record_origin'),UniqueConstraint('tenant_id','id',name='uq_crm_tenant_id'),Index('ix_record_tenant_kind','tenant_id','kind'))
class CRMLink(Base):
 __tablename__='crm_links'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 record_id:Mapped[str]=mapped_column(String(36),primary_key=True)
 relation:Mapped[str]=mapped_column(String(20),primary_key=True)
 related_id:Mapped[str]=mapped_column(String(36))
 __table_args__=(
  ForeignKeyConstraint(['tenant_id','record_id'],['crm_records.tenant_id','crm_records.id'],name='fk_crm_link_source_tenant',ondelete='CASCADE'),
  ForeignKeyConstraint(['tenant_id','related_id'],['crm_records.tenant_id','crm_records.id'],name='fk_crm_link_related_tenant',ondelete='CASCADE'),
  Index('ix_crm_link_related','tenant_id','related_id'),
 )

class RecordGrant(Base):
 __tablename__='record_grants'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 user_id:Mapped[str]=mapped_column(String(36),primary_key=True)
 record_id:Mapped[str]=mapped_column(String(36),primary_key=True)
 __table_args__=(
  ForeignKeyConstraint(['tenant_id','user_id'],['users.tenant_id','users.id'],name='fk_grant_user_tenant',ondelete='CASCADE'),
  ForeignKeyConstraint(['tenant_id','record_id'],['crm_records.tenant_id','crm_records.id'],name='fk_grant_record_tenant',ondelete='CASCADE'),
  Index('ix_record_grants_record','tenant_id','record_id'),
 )
class PipelineStage(Base):
 __tablename__='pipeline_stages'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 name:Mapped[str]=mapped_column(String(80),primary_key=True)
 position:Mapped[int]=mapped_column(Integer,nullable=False)
 __table_args__=(Index('ix_pipeline_stages_tenant_position','tenant_id','position'),)

class ImportBatch(Base):
 __tablename__='import_batches'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 actor_id:Mapped[str]=mapped_column(ForeignKey('users.id'))
 kind:Mapped[str]=mapped_column(String(20))
 source:Mapped[str]=mapped_column(String(80))
 digest:Mapped[str]=mapped_column(String(64))
 status:Mapped[str]=mapped_column(String(30))
 created_count:Mapped[int]=mapped_column(Integer,default=0)
 updated_count:Mapped[int]=mapped_column(Integer,default=0)
 skipped_count:Mapped[int]=mapped_column(Integer,default=0)
 error_count:Mapped[int]=mapped_column(Integer,default=0)
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
class BatchMutation(Base):
 __tablename__='batch_mutations'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 batch_id:Mapped[str]=mapped_column(ForeignKey('import_batches.id'),index=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'))
 record_id:Mapped[str]=mapped_column(ForeignKey('crm_records.id'))
 operation:Mapped[str]=mapped_column(String(10))
 previous:Mapped[str|None]=mapped_column(Text,nullable=True)
class Audit(Base):
 __tablename__='audit_events'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 user_id:Mapped[str]=mapped_column(ForeignKey('users.id'))
 action:Mapped[str]=mapped_column(String(100))
 target:Mapped[str]=mapped_column(String(180))
 detail:Mapped[str]=mapped_column(Text,default='{}')
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
class WorkPolicy(Base):
 __tablename__='work_policies'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 mode:Mapped[str]=mapped_column(String(12),default='guided')
 timezone:Mapped[str]=mapped_column(String(60),default='Europe/Madrid')
 weights:Mapped[str]=mapped_column(Text,default='{"overdue":5,"inbound":15,"proposal":10,"stale":5}')
 revision:Mapped[int]=mapped_column(Integer,default=1)
class WorkItem(Base):
 __tablename__='work_items'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),index=True)
 record_id:Mapped[str|None]=mapped_column(ForeignKey('crm_records.id'),nullable=True)
 title:Mapped[str]=mapped_column(String(300))
 channel:Mapped[str]=mapped_column(String(30),default='call')
 due_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
 state:Mapped[str]=mapped_column(String(24),default='open')
 priority_source:Mapped[str]=mapped_column(String(80),default='manual')
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
 __table_args__=(Index('ix_work_ready','tenant_id','owner_id','state','due_at'),)
class WorkLease(Base):
 __tablename__='work_leases'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'),primary_key=True)
 task_id:Mapped[str]=mapped_column(ForeignKey('work_items.id'))
 expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
class TaskCreateReceipt(Base):
 __tablename__='task_create_receipts'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 idem:Mapped[str]=mapped_column(String(120),primary_key=True)
 actor_id:Mapped[str]=mapped_column(String(36),nullable=False)
 task_id:Mapped[str]=mapped_column(ForeignKey('work_items.id'),unique=True,nullable=False)
 payload_sha:Mapped[str]=mapped_column(String(64),nullable=False)
 __table_args__=(
  ForeignKeyConstraint(['tenant_id','actor_id'],['users.tenant_id','users.id'],name='fk_task_receipt_actor_tenant'),
 )

class WorkResult(Base):
 __tablename__='work_results'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'))
 owner_id:Mapped[str]=mapped_column(ForeignKey('users.id'))
 task_id:Mapped[str]=mapped_column(ForeignKey('work_items.id'))
 idem:Mapped[str]=mapped_column(String(120))
 outcome:Mapped[str]=mapped_column(String(80))
 note:Mapped[str]=mapped_column(Text,default='')
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
 __table_args__=(UniqueConstraint('tenant_id','owner_id','idem',name='uq_work_outcome_idem'),)

# Added in Alembic 009. Business evidence is tenant-scoped and append-corrected.
class CommercialSignal(Base):
 __tablename__='commercial_signals'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),nullable=False,index=True)
 record_id:Mapped[str]=mapped_column(String(36),nullable=False)
 actor_id:Mapped[str]=mapped_column(String(36),nullable=False)
 kind:Mapped[str]=mapped_column(String(40),nullable=False)
 source_type:Mapped[str]=mapped_column(String(30),nullable=False)
 source_ref:Mapped[str]=mapped_column(String(120),nullable=False)
 occurred_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
 recorded_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
 verification:Mapped[str]=mapped_column(String(30),nullable=False)
 status:Mapped[str]=mapped_column(String(20),nullable=False,default='current')
 supersedes_id:Mapped[str|None]=mapped_column(String(36),nullable=True)
 detail:Mapped[str]=mapped_column(String(300),nullable=False,default='')
 __table_args__=(
  ForeignKeyConstraint(['tenant_id','record_id'],['crm_records.tenant_id','crm_records.id'],name='fk_signal_record_tenant'),
  ForeignKeyConstraint(['tenant_id','actor_id'],['users.tenant_id','users.id'],name='fk_signal_actor_tenant'),
  UniqueConstraint('tenant_id','source_type','source_ref',name='uq_signal_source'),
  Index('ix_signal_record_status','tenant_id','record_id','status','occurred_at'),
 )

# Added by Alembic 010: per-tenant, per-entity successful sync watermarks.
class CRMSyncCheckpoint(Base):
 __tablename__='crm_sync_checkpoints'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 provider:Mapped[str]=mapped_column(String(40),primary_key=True)
 kind:Mapped[str]=mapped_column(String(20),primary_key=True)
 last_completed_until:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
 last_completed_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
 imported_count:Mapped[int]=mapped_column(Integer,default=0,nullable=False)

# Alembic 011 — OAuth handshake and encrypted per-tenant provider tokens.
class CRMOAuthPending(Base):
 __tablename__='crm_oauth_pending'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),nullable=False,index=True)
 actor_id:Mapped[str]=mapped_column(String(36),nullable=False)
 provider:Mapped[str]=mapped_column(String(40),nullable=False)
 state_hash:Mapped[str]=mapped_column(String(64),nullable=False,unique=True)
 verifier_ciphertext:Mapped[str]=mapped_column(Text,nullable=False)
 expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
 __table_args__=(ForeignKeyConstraint(['tenant_id','actor_id'],['users.tenant_id','users.id'],name='fk_oauth_pending_actor_tenant'),)

class CRMOAuthConnection(Base):
 __tablename__='crm_oauth_connections'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 provider:Mapped[str]=mapped_column(String(40),primary_key=True)
 account_key:Mapped[str]=mapped_column(String(180),nullable=False)
 token_ciphertext:Mapped[str]=mapped_column(Text,nullable=False)
 refresh_ciphertext:Mapped[str|None]=mapped_column(Text,nullable=True)
 expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
 api_base:Mapped[str]=mapped_column(String(250),nullable=False)
 scopes:Mapped[str]=mapped_column(Text,nullable=False,default='')
 connected_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False,default=utcnow)

# Introduced only by Alembic 008; excluded from historical 001 dynamic metadata.
class AdminTOTP(Base):
 __tablename__='admin_totp'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 user_id:Mapped[str]=mapped_column(String(36),primary_key=True)
 secret_ciphertext:Mapped[str]=mapped_column(Text)
 last_step:Mapped[int]=mapped_column(Integer,default=-1)
 __table_args__=(ForeignKeyConstraint(['tenant_id','user_id'],['users.tenant_id','users.id'],name='fk_totp_user_tenant'),)

class AuthLoginAttempt(Base):
 __tablename__='auth_login_attempts'
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),primary_key=True)
 user_id:Mapped[str]=mapped_column(String(36),primary_key=True)
 failures:Mapped[int]=mapped_column(Integer,default=0)
 window_started_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
 locked_until:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
 __table_args__=(ForeignKeyConstraint(['tenant_id','user_id'],['users.tenant_id','users.id'],name='fk_auth_attempt_user_tenant'),)

class SessionProof(Base):
 __tablename__='session_proofs'
 token_hash:Mapped[str]=mapped_column(ForeignKey('sessions.token_hash',ondelete='CASCADE'),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),index=True)
 mfa_verified:Mapped[bool]=mapped_column(Boolean,default=False)
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


# Alembic 012: immutable-by-default user notes attached to one deal.
# Notes are never treated as evidence of a verified call or email.
class DealNote(Base):
 __tablename__='deal_notes'
 id:Mapped[str]=mapped_column(String(36),primary_key=True)
 tenant_id:Mapped[str]=mapped_column(ForeignKey('tenants.id'),nullable=False,index=True)
 deal_id:Mapped[str]=mapped_column(String(36),nullable=False)
 author_id:Mapped[str]=mapped_column(String(36),nullable=False)
 idem:Mapped[str]=mapped_column(String(120),nullable=False)
 body:Mapped[str]=mapped_column(Text,nullable=False)
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)
 __table_args__=(
  ForeignKeyConstraint(['tenant_id','deal_id'],['crm_records.tenant_id','crm_records.id'],name='fk_deal_note_record_tenant'),
  ForeignKeyConstraint(['tenant_id','author_id'],['users.tenant_id','users.id'],name='fk_deal_note_author_tenant'),
  UniqueConstraint('tenant_id','author_id','idem',name='uq_deal_note_idem'),
  Index('ix_deal_note_timeline','tenant_id','deal_id','created_at'),
 )
