import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://localhost/deviated_septa_dev",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=2)
Session = scoped_session(sessionmaker(bind=engine))
