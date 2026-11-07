import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
DATABASE_URL=os.getenv('REVENUE_OS_DATABASE_URL','sqlite:///./revenue_os_v10_dev.db')
engine=create_engine(DATABASE_URL,pool_pre_ping=True,connect_args={'check_same_thread':False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal=sessionmaker(bind=engine,expire_on_commit=False)
def get_db():
 with SessionLocal() as db:yield db
