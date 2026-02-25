# ─────────────────────────────────────────────────────────────────────────────
# backend/services/auth_service.py
# Google OAuth 2.0 + JWT + 초대코드 인증 서비스
# ─────────────────────────────────────────────────────────────────────────────

import httpx                                    # 비동기 HTTP 클라이언트
from datetime import datetime, timedelta        # JWT 만료 시간 계산
from typing import Optional                     # 타입 힌트
from jose import jwt, JWTError                 # JWT 생성/검증
from sqlalchemy.ext.asyncio import AsyncSession # 비동기 DB 세션
from sqlalchemy import select                   # SQL SELECT 쿼리
from models.user import User                    # User ORM 모델
from config import get_settings                 # 환경변수 설정

settings = get_settings()                       # 설정 싱글톤 로드

# ─── Google OAuth 엔드포인트 상수 ────────────────────────────────────────────
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo"

# ─── Google OAuth 스코프 (이메일 + 프로필 정보 요청) ────────────────────────
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Google OAuth 로그인 URL 생성
# ─────────────────────────────────────────────────────────────────────────────
def get_google_auth_url(state: str = "") -> str:
    """
    사용자를 Google 로그인 페이지로 보내기 위한 URL 생성
    반환값: https://accounts.google.com/o/oauth2/v2/auth?client_id=...
    """
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,            # Google 클라이언트 ID
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,      # 콜백 URL
        "response_type": "code",                           # 인증 코드 방식
        "scope": " ".join(GOOGLE_SCOPES),                  # 요청할 권한 목록
        "access_type": "offline",                          # refresh_token 발급
        "prompt": "consent",                               # 매번 동의 화면 표시
    }
    
    # state가 있으면 파라미터에 추가
    if state:
        params["state"] = state    
    
    # URL 쿼리스트링 직접 조합
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{GOOGLE_AUTH_URL}?{query_string}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Google 인증 코드 → 액세스 토큰 교환
# ─────────────────────────────────────────────────────────────────────────────
async def exchange_code_for_token(code: str) -> str:
    """
    Google이 콜백으로 보내준 code를 access_token으로 교환
    
    Args:
        code: Google OAuth 콜백 URL의 ?code= 파라미터 값
    Returns:
        access_token: Google API 호출에 사용할 액세스 토큰
    Raises:
        Exception: 토큰 교환 실패 시
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,        # 클라이언트 ID
                "client_secret": settings.GOOGLE_CLIENT_SECRET, # 클라이언트 시크릿
                "code": code,                                   # 인증 코드
                "grant_type": "authorization_code",             # 인증 방식
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,   # 콜백 URL (등록된 것과 동일해야 함)
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0                                        # 15초 타임아웃
        )

    if response.status_code != 200:
        raise Exception(f"Google 토큰 교환 실패: {response.text}")

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise Exception("access_token이 응답에 없습니다")

    return access_token


# ─────────────────────────────────────────────────────────────────────────────
# 3. Google 사용자 정보 조회
# ─────────────────────────────────────────────────────────────────────────────
async def get_google_user_info(access_token: str) -> dict:
    """
    액세스 토큰으로 Google에서 사용자 프로필 정보 조회
    
    Args:
        access_token: exchange_code_for_token()으로 받은 액세스 토큰
    Returns:
        dict: {
            "id": "Google 고유 ID",
            "email": "이메일",
            "name": "이름",
            "picture": "프로필 사진 URL",
            "verified_email": True
        }
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},  # Bearer 토큰 방식
            timeout=10.0
        )

    if response.status_code != 200:
        raise Exception(f"Google 사용자 정보 조회 실패: {response.text}")

    user_info = response.json()

    # 필수 필드 확인
    if not user_info.get("id") or not user_info.get("email"):
        raise Exception("Google 사용자 정보에 필수 필드가 없습니다")

    return user_info


# ─────────────────────────────────────────────────────────────────────────────
# 4. DB에서 Google ID로 사용자 조회
# ─────────────────────────────────────────────────────────────────────────────
async def get_user_by_google_id(
    db: AsyncSession,
    google_id: str
) -> Optional[User]:
    """
    Google ID로 MySQL DB에서 사용자 조회
    
    Args:
        db: SQLAlchemy 비동기 세션
        google_id: Google 사용자 고유 ID
    Returns:
        User 객체 또는 None (없으면)
    """
    result = await db.execute(
        select(User).where(User.google_id == google_id)  # google_id 컬럼으로 조회
    )
    return result.scalar_one_or_none()                   # 1개 또는 None 반환


# ─────────────────────────────────────────────────────────────────────────────
# 5. 이메일로 사용자 조회 (중복 가입 방지용)
# ─────────────────────────────────────────────────────────────────────────────
async def get_user_by_email(
    db: AsyncSession,
    email: str
) -> Optional[User]:
    """이메일로 사용자 조회"""
    result = await db.execute(
        select(User).where(User.email == email)
    )
    return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# 6. 신규 사용자 생성
# ─────────────────────────────────────────────────────────────────────────────
async def create_user(db: AsyncSession, user_info: dict) -> User:
    """
    Google 사용자 정보로 MySQL DB에 신규 사용자 생성
    
    Args:
        db: SQLAlchemy 비동기 세션
        user_info: get_google_user_info()가 반환한 dict
    Returns:
        생성된 User 객체
    """
    # 이미 존재하는 사용자인지 확인 (중복 방지)
    existing = await get_user_by_google_id(db, user_info["id"])
    if existing:
        # 이미 있으면 마지막 로그인 시간만 업데이트
        existing.last_login_at = datetime.utcnow()
        await db.commit()
        await db.refresh(existing)
        return existing

    # 신규 사용자 생성
    new_user = User(
        email=user_info["email"],                        # 이메일
        name=user_info.get("name", "사용자"),             # 이름 (없으면 기본값)
        google_id=user_info["id"],                       # Google 고유 ID
        picture_url=user_info.get("picture", ""),        # 프로필 사진 URL
        is_active=True,                                  # 활성 상태
        last_login_at=datetime.utcnow(),                 # 최초 로그인 시간
    )

    db.add(new_user)                                     # DB에 추가
    await db.commit()                                    # 트랜잭션 커밋
    await db.refresh(new_user)                           # DB에서 최신 데이터 재로드 (id 등)

    return new_user


# ─────────────────────────────────────────────────────────────────────────────
# 7. 마지막 로그인 시간 업데이트
# ─────────────────────────────────────────────────────────────────────────────
async def update_last_login(db: AsyncSession, user: User) -> None:
    """로그인 시 마지막 접속 시간 갱신"""
    user.last_login_at = datetime.utcnow()
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 8. JWT 토큰 생성
# ─────────────────────────────────────────────────────────────────────────────
def create_jwt_token(data: dict) -> str:
    """
    사용자 정보를 JWT 토큰으로 인코딩
    
    Args:
        data: {"sub": "user_id", "email": "이메일"} 형태의 dict
    Returns:
        서명된 JWT 토큰 문자열
    """
    payload = data.copy()                                # 원본 데이터 복사

    # 만료 시간 설정 (현재 시간 + EXPIRE_HOURS)
    expire = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload.update({"exp": expire})                      # 만료 시간 추가

    # JWT 토큰 서명 (HS256 알고리즘)
    token = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,                         # 서명 비밀키
        algorithm=settings.JWT_ALGORITHM                 # HS256
    )
    return token


# ─────────────────────────────────────────────────────────────────────────────
# 9. JWT 토큰 검증 및 디코딩
# ─────────────────────────────────────────────────────────────────────────────
def verify_jwt_token(token: str) -> Optional[dict]:
    """
    JWT 토큰 서명 검증 및 페이로드 반환
    
    Args:
        token: Bearer 토큰 문자열
    Returns:
        payload dict 또는 None (유효하지 않으면)
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,                     # 검증에 사용할 비밀키
            algorithms=[settings.JWT_ALGORITHM]          # 허용할 알고리즘
        )
        return payload
    except JWTError:
        # 서명 불일치, 만료, 형식 오류 등 모두 None 반환
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 10. JWT 토큰에서 현재 사용자 조회
# ─────────────────────────────────────────────────────────────────────────────
async def get_current_user(
    db: AsyncSession,
    token: str
) -> Optional[User]:
    """
    JWT 토큰으로 현재 로그인한 사용자 DB 조회
    FastAPI 라우터의 Depends()에서 사용
    
    Args:
        db: DB 세션
        token: Authorization 헤더의 Bearer 토큰
    Returns:
        User 객체 또는 None
    """
    payload = verify_jwt_token(token)                    # 토큰 검증
    if not payload:
        return None

    user_id = payload.get("sub")                         # sub 필드에서 user_id 추출
    if not user_id:
        return None

    # DB에서 사용자 조회
    result = await db.execute(
        select(User).where(User.id == int(user_id))
    )
    return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# 11. FastAPI Depends용 토큰 추출 + 검증 함수
# ─────────────────────────────────────────────────────────────────────────────
def get_token_from_header(authorization: str) -> Optional[str]:
    """
    Authorization 헤더에서 Bearer 토큰 추출
    
    Args:
        authorization: "Bearer eyJhbGci..." 형태의 헤더 값
    Returns:
        토큰 문자열 또는 None
    """
    if not authorization:
        return None
    parts = authorization.split(" ")                     # "Bearer TOKEN" 분리
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]                                      # TOKEN 부분만 반환


# ─────────────────────────────────────────────────────────────────────────────
# 12. 초대코드 검증
# ─────────────────────────────────────────────────────────────────────────────
def verify_invite_code(code: str) -> bool:
    """
    입력된 초대코드가 .env의 INVITE_CODE와 일치하는지 확인
    
    Args:
        code: 사용자가 입력한 초대코드
    Returns:
        True (일치) / False (불일치)
    """
    if not code or not settings.INVITE_CODE:
        return False                                     # 둘 중 하나라도 비어있으면 거부
    return code.strip() == settings.INVITE_CODE.strip() # 앞뒤 공백 제거 후 비교
