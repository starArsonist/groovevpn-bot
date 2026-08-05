from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config import settings

# Create async engine for SQLite
engine = create_async_engine(
    settings.database_url,
    echo=False,
)

# Create a sessionmaker that returns AsyncSession
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
