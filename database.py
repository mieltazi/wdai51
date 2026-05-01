import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

# Код берет ссылку из скрытых настроек Vercel
DATABASE_URL = os.getenv("DATABASE_URL")

# ВАЖНО: Добавляем connect_args для совместимости с пулером Supabase/PGBouncer
engine = create_async_engine(
    DATABASE_URL, 
    echo=False, 
    connect_args={"statement_cache_size": 0}
)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session