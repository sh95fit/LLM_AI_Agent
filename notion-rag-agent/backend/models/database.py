# ─────────────────────────────────────────────────────────────────────────────
# backend/models/database.py
# ─────────────────────────────────────────────────────────────────────────────

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import get_settings

settings = get_settings()

# ─── 비동기 엔진 생성 ─────────────────────────────────────────
engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,        # mysql+aiomysql:// URL
    echo=False,                         # SQL 로그 (디버그 시 True)
    pool_pre_ping=True,                 # 연결 유효성 사전 검사
    pool_recycle=3600,                  # 1시간마다 연결 재생성
    pool_size=10,                       # 연결 풀 크기
    max_overflow=20,                    # 최대 초과 연결 수
)

# ─── 비동기 세션 팩토리 ───────────────────────────────────────
async_session_maker = async_sessionmaker(
    engine,
    expire_on_commit=False,             # commit 후 객체 만료 안 함
    class_=AsyncSession,
)

# ─── ORM Base 클래스 ──────────────────────────────────────────
class Base(DeclarativeBase):
    pass

# ─── DB 세션 의존성 (리턴 타입 어노테이션 제거) ────────────────
async def get_db():                     # ← 리턴 타입 제거가 핵심
    """FastAPI Depends()용 DB 세션 생성기"""
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
