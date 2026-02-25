# =============================================================================
#  backend/config.py
#
#  애플리케이션 전역 설정
#
#  Pydantic Settings 를 사용하여 .env 파일에서 환경변수를 로드합니다.
#  get_settings() 를 호출하면 싱글톤 패턴으로 한 번만 로드됩니다.
#
#  ┌─ 설정 항목 ────────────────────────────────────────────────────────────┐
#  │  APP_NAME, DEBUG           : 앱 기본 정보                              │
#  │  NOTION_*                  : Notion API 연동                           │
#  │  OLLAMA_*                  : Ollama LLM / 임베딩 모델                  │
#  │  QDRANT_*                  : Qdrant 벡터 데이터베이스                   │
#  │  REDIS_*                   : Redis 세션 저장소                          │
#  │  MYSQL_*                   : MySQL 사용자 데이터베이스                  │
#  │  GOOGLE_*                  : Google OAuth 2.0 인증                     │
#  │  JWT_*                     : JWT 토큰 설정                             │
#  │  INVITE_CODE               : 앱 접근용 초대 코드                       │
#  │  JANDI_WEBHOOK_URL         : 잔디 알림 웹훅                            │
#  │  FRONTEND_URL              : 프론트엔드 URL (OAuth 리다이렉트)          │
#  └────────────────────────────────────────────────────────────────────────┘
#
#  .env 파일 예시:
#    NOTION_API_KEY=ntn_xxxxx
#    NOTION_TOKEN=ntn_xxxxx          # NOTION_API_KEY 와 동일해도 됨
#    NOTION_DATABASE_ID=abcd1234
#    NOTION_DATABASE_IDS=abcd1234,efgh5678
#    GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
#    GOOGLE_CLIENT_SECRET=GOCSPX-xxxxx
#    GOOGLE_REDIRECT_URI=http://localhost:8000/auth/callback
#    JWT_SECRET_KEY=your-secret-key
#    INVITE_CODE=your-invite-code
#    MYSQL_PASSWORD=your-password
#    JANDI_WEBHOOK_URL=https://wh.jandi.com/xxxxx
#    FRONTEND_URL=http://localhost:8501
# =============================================================================

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """
    애플리케이션 환경변수 설정 클래스

    .env 파일에서 자동으로 값을 로드합니다.
    기본값이 있는 항목은 .env 에 없어도 동작합니다.
    기본값이 없는 항목은 .env 에 반드시 있어야 합니다.
    """

    # ── 앱 기본 설정 ─────────────────────────────────────────────
    APP_NAME: str = "Notion RAG Chat"       # 앱 이름 (로그 등에서 사용)
    DEBUG: bool = False                      # 디버그 모드 (True: SQL 로그 출력 등)

    # ── Notion API ───────────────────────────────────────────────
    # NOTION_API_KEY : 기존 호환용 (다른 파일에서 참조할 수 있음)
    # NOTION_TOKEN   : notion_service.py 에서 사용하는 실제 토큰
    # 두 값이 같아도 되고, NOTION_TOKEN 만 있어도 됩니다.
    NOTION_API_KEY: str                      # Notion Integration 토큰
    NOTION_DATABASE_ID: str                  # 단일 DB ID (기존 호환)
    NOTION_TOKEN: str = ""                   # Notion Integration 토큰 (동기화용)
    NOTION_DATABASE_IDS: str = ""            # 쉼표 구분 DB ID 목록 (동기화용)
    #   예: "abcd1234,efgh5678"
    #   비어있으면 search API 폴백으로 전체 페이지를 검색합니다.

    # ── Ollama LLM ───────────────────────────────────────────────
    OLLAMA_BASE_URL: str = "http://ollama:11434"    # Ollama 서버 URL
    OLLAMA_MODEL: str = "llama3.2:3b"                # 대화용 LLM 모델
    # OLLAMA_EMBED_MODEL: str = "nomic-embed-text"     # 임베딩 모델
    OLLAMA_EMBED_MODEL: str = "bge-m3"     # 임베딩 모델

    # ── Qdrant 벡터 DB ───────────────────────────────────────────
    QDRANT_HOST: str = "qdrant"              # Qdrant 서버 호스트
    QDRANT_PORT: int = 6333                  # Qdrant 서버 포트
    QDRANT_COLLECTION: str = "notion_docs"   # 컬렉션 이름

    # ── Redis 세션 저장소 ────────────────────────────────────────
    REDIS_HOST: str = "redis"                # Redis 서버 호스트
    REDIS_PORT: int = 6379                   # Redis 서버 포트
    REDIS_DB: int = 0                        # Redis DB 번호

    # ── MySQL 사용자 DB ──────────────────────────────────────────
    MYSQL_USER: str = "lunchlab_user"        # MySQL 사용자명
    MYSQL_PASSWORD: str                      # MySQL 비밀번호
    MYSQL_HOST: str = "mysql"                # MySQL 서버 호스트
    MYSQL_PORT: int = 3306                   # MySQL 서버 포트
    MYSQL_DATABASE: str = "notion_rag"       # MySQL 데이터베이스명

    # ── Google OAuth 2.0 ─────────────────────────────────────────
    GOOGLE_CLIENT_ID: str                    # Google OAuth 클라이언트 ID
    GOOGLE_CLIENT_SECRET: str                # Google OAuth 클라이언트 시크릿
    GOOGLE_REDIRECT_URI: str                 # Google OAuth 콜백 URL
    #   예: http://localhost:8000/auth/callback

    # ── JWT 인증 토큰 ────────────────────────────────────────────
    JWT_SECRET_KEY: str                      # JWT 서명 비밀키
    JWT_ALGORITHM: str = "HS256"             # JWT 서명 알고리즘
    JWT_EXPIRE_HOURS: int = 24               # JWT 토큰 만료 시간 (시간)

    # ── 초대 코드 ────────────────────────────────────────────────
    INVITE_CODE: str                         # 앱 접근용 초대 코드

    # ── 잔디 웹훅 ────────────────────────────────────────────────
    JANDI_WEBHOOK_URL: str = ""              # 잔디 웹훅 URL (비어있으면 미사용)

    # ── 동기화 설정 ──────────────────────────────────────────────
    SYNC_INTERVAL_HOURS: int = 1             # 자동 동기화 간격 (시간)

    # ── 프론트엔드 URL ───────────────────────────────────────────
    # OAuth 콜백 후 JWT 토큰을 전달하며 리다이렉트할 Streamlit 주소
    FRONTEND_URL: str = "http://localhost:8501"

    # ── 파생 속성: DB 연결 URL ───────────────────────────────────

    @property
    def DATABASE_URL_ASYNC(self) -> str:
        """비동기 MySQL 연결 URL (aiomysql 드라이버)"""
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """동기 MySQL 연결 URL (pymysql 드라이버)"""
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
        )

    class Config:
        env_file = ".env"       # .env 파일에서 환경변수 로드
        extra = "ignore"        # .env 에 정의되지 않은 키는 무시


@lru_cache()
def get_settings() -> Settings:
    """
    설정 싱글톤을 반환합니다.

    @lru_cache 데코레이터로 한 번만 로드하여 메모리를 절약합니다.
    이후 호출에서는 캐시된 인스턴스를 반환합니다.
    """
    return Settings()


# 편의용: from config import settings 로 바로 import 가능
settings = get_settings()
