# =============================================================================
#  backend/api/routes/auth.py  —  인증 라우터
#
#  ┌─ 설계 원칙 ────────────────────────────────────────────────────────────┐
#  │  • 앱 코드(초대 코드) 선검증 후에만 Google OAuth 진행 가능             │
#  │  • 신규 사용자: 초대 코드를 OAuth state에 포함 → 콜백에서 사용        │
#  │  • 기존 사용자: 초대 코드 불필요, 바로 JWT 발급                        │
#  └────────────────────────────────────────────────────────────────────────┘
#
#  엔드포인트 요약:
#    POST /auth/verify-invite  앱 코드 유효성 확인 (프론트엔드 Stage 1)
#    POST /auth/check-invite   위와 동일 (별칭 — 하위 호환)
#    GET  /auth/google         Google OAuth 시작 (state 파라미터에 초대코드 포함)
#    GET  /auth/callback       Google OAuth 콜백 → JWT 발급
#    GET  /auth/me             현재 사용자 정보 조회 (Authorization 헤더)
#    POST /auth/logout         로그아웃
#
#  프론트엔드 연동 흐름:
#    ┌─────────────────────────────────────────────────────────────────────┐
#    │  [Stage 1] 초대 코드 입력                                           │
#    │    프론트: POST /auth/verify-invite {"code": "xxx"}                 │
#    │    백엔드: verify_invite_code() → {"valid": true/false}             │
#    │                                                                     │
#    │  [Stage 2] Google 로그인 버튼 클릭                                  │
#    │    프론트: 브라우저가 GET /auth/google?state=base64(...) 로 이동     │
#    │    백엔드: state 에서 invite_code 추출 → 검증 → Google OAuth 시작   │
#    │    Google: 사용자 동의 → GET /auth/callback?code=...&state=...      │
#    │    백엔드: 코드 교환 → 사용자 조회/생성 → JWT 발급                   │
#    │    백엔드: 302 리다이렉트 → {FRONTEND_URL}?token={JWT}              │
#    │    프론트: handle_oauth_callback() → 토큰 저장 → /auth/me 호출     │
#    └─────────────────────────────────────────────────────────────────────┘
# =============================================================================

import base64
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from services.auth_service import (
    get_google_auth_url,        # Google OAuth 동의 URL 생성
    exchange_code_for_token,    # 인가 코드 → 액세스 토큰 교환
    get_google_user_info,       # 액세스 토큰 → 사용자 정보 조회
    get_user_by_google_id,      # DB에서 google_id로 사용자 조회
    create_user,                # 신규 사용자 DB 저장
    create_jwt_token,           # JWT 토큰 생성
    verify_jwt_token,           # JWT 토큰 검증
    verify_invite_code,         # 초대 코드 유효성 확인
    get_token_from_header,      # Authorization 헤더에서 토큰 추출
)
from config import get_settings

logger = logging.getLogger(__name__)

# ── 라우터 설정 ──────────────────────────────────────────────────────────
# prefix="/auth": 모든 엔드포인트 앞에 /auth 가 자동으로 붙음
# tags=["auth"]: Swagger UI 에서 그룹핑
router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# Streamlit 프론트엔드 URL (OAuth 콜백 후 리다이렉트 대상)
FRONTEND_URL = settings.FRONTEND_URL


# =============================================================================
#  요청/응답 스키마 (Pydantic)
# =============================================================================

class CheckInviteRequest(BaseModel):
    """
    초대 코드 검증 요청

    프론트엔드 로그인 Stage 1 에서 사용합니다.
    사용자가 입력한 코드를 이 스키마로 전송합니다.
    """
    code: str   # 사용자가 입력한 초대 코드


class CheckInviteResponse(BaseModel):
    """
    초대 코드 검증 응답

    valid=True 이면 프론트엔드가 Stage 2 (Google 로그인) 로 전환합니다.
    valid=False 이면 에러 메시지를 표시합니다.
    """
    valid: bool     # True: 유효, False: 무효
    message: str    # 사용자에게 표시할 안내 메시지


# =============================================================================
#  § A. 초대 코드 검증
#
#  프론트엔드 Stage 1 에서 호출합니다.
#  코드의 유효성만 확인하고 사용자를 생성하지 않습니다.
#
#  두 가지 경로를 모두 지원합니다:
#    POST /auth/verify-invite  ← 프론트엔드 app.py 가 호출하는 경로
#    POST /auth/check-invite   ← 하위 호환용 별칭
# =============================================================================

@router.post("/verify-invite", response_model=CheckInviteResponse)
async def verify_invite(body: CheckInviteRequest):
    """
    초대 코드 유효성을 확인합니다.

    사용자 생성은 하지 않습니다.
    프론트엔드 _validate_app_code() → api_post("/auth/verify-invite") 에서 호출됩니다.

    Args:
        body: {"code": "사용자가 입력한 초대코드"}

    Returns:
        {"valid": True/False, "message": "안내 메시지"}
    """
    is_valid = verify_invite_code(body.code)

    if not is_valid:
        # HTTP 200 으로 반환 (프론트엔드가 valid 필드로 판단)
        return CheckInviteResponse(
            valid=False,
            message="유효하지 않은 앱 코드입니다. 관리자에게 문의하세요.",
        )

    return CheckInviteResponse(
        valid=True,
        message="앱 코드 확인 완료. Google 로그인을 진행하세요.",
    )


@router.post("/check-invite", response_model=CheckInviteResponse)
async def check_invite(body: CheckInviteRequest):
    """
    /verify-invite 의 하위 호환용 별칭입니다.
    동일한 로직을 수행합니다.
    """
    return await verify_invite(body)


# =============================================================================
#  § B. Google OAuth 로그인 시작
#
#  프론트엔드 Stage 2 에서 Google 로그인 버튼을 클릭하면
#  브라우저가 이 엔드포인트로 이동합니다.
#
#  프론트엔드가 보내는 URL 형태:
#    GET /auth/google?state=base64({"invite_code": "xxx"})
#
#  state 파라미터에서 invite_code 를 추출하여 재검증한 후
#  Google OAuth 동의 화면으로 302 리다이렉트합니다.
# =============================================================================

@router.get("/google")
async def google_login(
    state: str = Query(
        default="",
        description="base64 인코딩된 JSON (invite_code 포함)",
    ),
    invite_code: str = Query(
        default="",
        description="직접 전달된 초대 코드 (하위 호환용)",
    ),
):
    """
    Google OAuth 로그인을 시작합니다.

    두 가지 방식으로 초대 코드를 받을 수 있습니다:
      1. ?state=base64({"invite_code": "xxx"})  ← 프론트엔드 v11+ 방식
      2. ?invite_code=xxx                        ← 직접 전달 방식 (하위 호환)

    초대 코드 검증에 성공하면 Google 동의 화면으로 리다이렉트합니다.
    실패하면 403 오류를 반환합니다.
    """
    # ── state 에서 invite_code 추출 시도 ─────────────────────────
    resolved_invite_code = invite_code   # 기본값: 직접 전달된 값

    if state:
        try:
            state_json = base64.urlsafe_b64decode(
                state.encode("utf-8")
            ).decode("utf-8")
            state_data = json.loads(state_json)
            resolved_invite_code = state_data.get("invite_code", "") or resolved_invite_code
        except Exception:
            # state 파싱 실패 시 직접 전달된 invite_code 사용
            logger.warning("OAuth state 파싱 실패, invite_code 파라미터 사용")

    # ── 초대 코드 검증 (우회 방지) ───────────────────────────────
    if not verify_invite_code(resolved_invite_code):
        raise HTTPException(
            status_code=403,
            detail="유효하지 않은 초대 코드입니다.",
        )

    # ── invite_code 를 OAuth state 에 재인코딩 ───────────────────
    # Google 콜백에서 다시 꺼내 써야 하므로 state 에 포함합니다.
    state_data = json.dumps({"invite_code": resolved_invite_code})
    state_encoded = base64.urlsafe_b64encode(
        state_data.encode("utf-8")
    ).decode("utf-8")

    # ── Google OAuth URL 생성 + 리다이렉트 ───────────────────────
    auth_url = get_google_auth_url(state=state_encoded)
    return RedirectResponse(url=auth_url, status_code=302)


# =============================================================================
#  § C. Google OAuth 콜백 처리
#
#  Google 동의 후 브라우저가 이 엔드포인트로 리다이렉트됩니다.
#
#  처리 순서:
#    ① state 에서 invite_code 추출
#    ② 인가 코드(code) → 액세스 토큰 교환
#    ③ 액세스 토큰 → Google 사용자 정보 조회
#    ④ DB 에서 기존 사용자 확인
#       ├── 기존 사용자: 바로 JWT 발급
#       └── 신규 사용자: invite_code 재검증 → 사용자 생성 → JWT 발급
#    ⑤ 프론트엔드로 리다이렉트: {FRONTEND_URL}?token={JWT}
#    ⑥ 오류 시: {FRONTEND_URL}?error={메시지}
# =============================================================================

@router.get("/callback")
async def auth_callback(
    code: str = Query(..., description="Google 인가 코드"),
    state: str = Query(default="", description="base64 인코딩된 state"),
    db: AsyncSession = Depends(get_db),
):
    """
    Google OAuth 콜백을 처리하고 JWT 를 발급합니다.

    성공: {FRONTEND_URL}?token={JWT} 로 리다이렉트
    실패: {FRONTEND_URL}?error={메시지} 로 리다이렉트
    """
    try:
        # ── ① state 에서 invite_code 추출 ────────────────────────
        invite_code = ""
        if state:
            try:
                state_json = base64.urlsafe_b64decode(
                    state.encode("utf-8")
                ).decode("utf-8")
                state_data = json.loads(state_json)
                invite_code = state_data.get("invite_code", "")
            except Exception:
                logger.warning("OAuth callback: state 파싱 실패")

        # ── ② 인가 코드 → 액세스 토큰 교환 ──────────────────────
        access_token = await exchange_code_for_token(code)

        # ── ③ 액세스 토큰 → Google 사용자 정보 ──────────────────
        user_info = await get_google_user_info(access_token)
        google_id = user_info["id"]

        # ── ④ DB 에서 기존 사용자 확인 ───────────────────────────
        user = await get_user_by_google_id(db, google_id)

        if user and user.is_active:
            # 기존 활성 사용자: 바로 JWT 발급
            token = create_jwt_token({
                "sub":   str(user.id),
                "email": user.email,
                "name":  getattr(user, "name", user.email.split("@")[0]),
            })
        else:
            # 신규 사용자: invite_code 재검증 후 계정 생성
            if not verify_invite_code(invite_code):
                return RedirectResponse(
                    url=f"{FRONTEND_URL}?error=invalid_invite_code",
                    status_code=302,
                )

            user = await create_user(db, {
                **user_info,
                "invite_code": invite_code,
            })

            token = create_jwt_token({
                "sub":   str(user.id),
                "email": user.email,
                "name":  getattr(user, "name", ""),
            })

        # ── ⑤ 프론트엔드로 JWT 전달 ─────────────────────────────
        return RedirectResponse(
            url=f"{FRONTEND_URL}?token={token}",
            status_code=302,
        )

    except Exception as e:
        # ── ⑥ 오류 시 에러 메시지와 함께 리다이렉트 ──────────────
        logger.error(f"OAuth 콜백 오류: {e}", exc_info=True)
        error_msg = str(e)[:200]
        return RedirectResponse(
            url=f"{FRONTEND_URL}?error={error_msg}",
            status_code=302,
        )


# =============================================================================
#  § D. 현재 사용자 정보 조회
#
#  프론트엔드가 OAuth 콜백 후 토큰을 받으면
#  GET /auth/me 를 호출하여 사용자 정보를 가져옵니다.
#
#  토큰은 Authorization: Bearer {token} 헤더로 전달됩니다.
#  하위 호환을 위해 ?token= 쿼리 파라미터도 지원합니다.
# =============================================================================

@router.get("/me", response_model=None)
async def get_me(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None, description="JWT 토큰 (하위 호환)"),
):
    """
    현재 로그인한 사용자의 정보를 반환합니다.

    토큰 전달 방법 (우선순위):
      1. Authorization: Bearer {token} 헤더  ← 프론트엔드 기본 방식
      2. ?token={token} 쿼리 파라미터          ← 하위 호환

    Returns:
        {"sub": "user_id", "email": "이메일", "name": "이름"}
    """
    # Authorization 헤더에서 토큰 추출 시도
    resolved_token = get_token_from_header(authorization) if authorization else None

    # 헤더에 없으면 쿼리 파라미터에서 가져옴
    if not resolved_token:
        resolved_token = token

    if not resolved_token:
        raise HTTPException(status_code=401, detail="인증 토큰이 없습니다.")

    # JWT 토큰 검증
    payload = verify_jwt_token(resolved_token)
    if not payload:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    return {
        "sub":   payload.get("sub", ""),
        "email": payload.get("email", ""),
        "name":  payload.get("name", ""),
    }


# =============================================================================
#  § E. 로그아웃
#
#  JWT 는 stateless 이므로 서버에서 개별 토큰을 무효화할 수 없습니다.
#  프론트엔드가 session_state 를 초기화하는 방식으로 로그아웃합니다.
#  이 엔드포인트는 프론트엔드 요청을 받아 성공 응답만 반환합니다.
# =============================================================================

@router.post("/logout")
async def logout():
    """
    로그아웃 처리.

    서버 측에서는 별도 토큰 무효화 없음.
    프론트엔드 force_logout() 에서 session_state 초기화로 로그아웃 완료.
    """
    return {"message": "로그아웃 완료"}
