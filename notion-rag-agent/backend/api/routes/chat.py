# =============================================================================
#  backend/api/routes/chat.py
#
#  채팅 API 라우터 — 세션 관리 및 RAG 응답 생성
#
#  엔드포인트:
#    POST   /api/chat                    메시지 전송 → RAG 응답 생성
#    GET    /api/chats                   채팅 세션 목록 조회
#    POST   /api/chats/new              새 채팅 세션 생성
#    DELETE /api/chats/{session_id}      채팅 세션 삭제
#    GET    /api/chats/{session_id}/history  대화 히스토리 조회
#
#  인증:
#    모든 엔드포인트는 Authorization: Bearer {JWT} 헤더가 필요합니다.
#    get_authenticated_user() 가 JWT 를 검증하고 User 객체를 반환합니다.
#
#  메시지 흐름 (POST /api/chat):
#    1. 사용자 메시지를 Redis 에 저장
#    2. Qdrant 에서 관련 문서를 벡터 검색
#    3. Ollama LLM 으로 답변 생성
#    4. AI 응답을 Redis 에 저장
#    5. 응답 반환: {answer, sources, session_id}
# =============================================================================

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from models.database import get_db
from services.auth_service import (
    get_current_user,            # JWT → DB 사용자 조회
    get_token_from_header,       # "Bearer TOKEN" → TOKEN 추출
)
from core.rag_chain import generate_response    # RAG 응답 생성
from core.memory import (
    create_session,              # 새 채팅 세션 생성 (Redis)
    delete_session,              # 채팅 세션 삭제 (Redis)
    get_all_sessions,            # 전체 세션 목록 (Redis)
    get_session_messages,        # 세션 메시지 조회 (Redis)
    save_message,                # 메시지 저장 (Redis)
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])   # /api 접두사는 main.py 에서 추가


# ── 요청/응답 스키마 ─────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    """
    채팅 메시지 요청

    프론트엔드 _call_rag_api() 에서 전송합니다.
    """
    message: str        # 사용자가 입력한 메시지
    session_id: str     # 현재 채팅 세션 ID


class ChatResponse(BaseModel):
    """
    채팅 응답

    answer:     AI 가 생성한 답변 텍스트
    sources:    답변에 참고된 문서 목록 [{url, title}, ...]
    session_id: 요청과 동일한 세션 ID
    """
    answer: str         # AI 답변
    sources: list       # 출처 목록
    session_id: str     # 세션 ID


# ── 공통 인증 헬퍼 ───────────────────────────────────────────────────────

async def get_authenticated_user(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Authorization 헤더에서 JWT 를 추출하고 DB 에서 사용자를 조회합니다.

    모든 채팅 API 에서 Depends(get_authenticated_user) 로 사용됩니다.

    흐름:
      "Bearer eyJhbGci..." → 토큰 추출 → JWT 검증 → DB 조회 → User 반환

    실패 시:
      401 (토큰 없음/무효) 또는 403 (비활성 계정)
    """
    # ① 헤더에서 토큰 추출
    token = get_token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="인증 토큰이 없습니다")

    # ② JWT 검증 + DB 사용자 조회
    user = await get_current_user(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")

    # ③ 활성 상태 확인
    if not user.is_active:
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    return user


# ─────────────────────────────────────────────────────────────────────────
#  POST /api/chat — 메시지 전송 및 RAG 응답
# ─────────────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    user=Depends(get_authenticated_user),
):
    """
    사용자 메시지를 받아 RAG 기반 AI 응답을 생성합니다.

    처리 흐름:
      1. 사용자 메시지를 Redis 에 저장
      2. RAG 체인 호출:
         - Qdrant 에서 질문과 유사한 문서 청크를 벡터 검색
         - 검색된 문서를 Context 로 포함하여 Ollama LLM 에 전달
         - LLM 이 Context 를 참고하여 답변 생성
      3. AI 응답을 Redis 에 저장
      4. 응답 반환
    """
    try:
        # 1. 사용자 메시지 Redis 에 저장
        await save_message(
            session_id=request.session_id,
            role="user",
            content=request.message,
        )

        # 2. RAG 체인으로 응답 생성
        result = await generate_response(
            question=request.message,
            session_id=request.session_id,
        )

        answer = result.get("answer", "답변을 생성할 수 없습니다.")
        sources = result.get("sources", [])

        # 3. AI 응답 Redis 에 저장
        await save_message(
            session_id=request.session_id,
            role="assistant",
            content=answer,
            sources=sources,
        )

        # 4. 응답 반환
        return ChatResponse(
            answer=answer,
            sources=sources,
            session_id=request.session_id,
        )

    except Exception as e:
        logger.error(f"채팅 응답 생성 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"응답 생성 오류: {str(e)}",
        )


# ─────────────────────────────────────────────────────────────────────────
#  GET /api/chats — 채팅 세션 목록 조회
# ─────────────────────────────────────────────────────────────────────────

@router.get("/chats")
async def get_chats(user=Depends(get_authenticated_user)):
    """
    현재 사용자의 전체 채팅 세션 목록을 반환합니다.

    Redis 에서 user_id 기반으로 세션 목록을 조회합니다.
    최신 세션이 먼저 나옵니다.

    Returns:
        {"sessions": [{"session_id": "...", "title": "...", ...}, ...]}
    """
    try:
        sessions = await get_all_sessions(user_id=str(user.id))
        return {"sessions": sessions}
    except Exception as e:
        logger.error(f"세션 목록 조회 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"세션 목록 조회 오류: {str(e)}",
        )


# ─────────────────────────────────────────────────────────────────────────
#  POST /api/chats/new — 새 채팅 세션 생성
# ─────────────────────────────────────────────────────────────────────────

@router.post("/chats/new")
async def new_chat(user=Depends(get_authenticated_user)):
    """
    새 채팅 세션을 생성합니다.

    Redis 에 세션 메타데이터를 저장하고 세션 ID 를 반환합니다.
    프론트엔드 create_new_chat() 에서 호출됩니다.

    Returns:
        {"session_id": "uuid", "message": "새 채팅이 시작되었습니다"}
    """
    try:
        session_id = await create_session(user_id=str(user.id))
        return {
            "session_id": session_id,
            "message": "새 채팅이 시작되었습니다",
        }
    except Exception as e:
        logger.error(f"세션 생성 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"세션 생성 오류: {str(e)}",
        )


# ─────────────────────────────────────────────────────────────────────────
#  DELETE /api/chats/{session_id} — 채팅 세션 삭제
# ─────────────────────────────────────────────────────────────────────────

@router.delete("/chats/{session_id}")
async def remove_chat(
    session_id: str,
    user=Depends(get_authenticated_user),
):
    """
    채팅 세션 및 관련 메시지를 모두 삭제합니다.

    user_id 를 함께 확인하여 본인의 세션만 삭제 가능합니다.

    Args:
        session_id: 삭제할 채팅 세션 ID
    """
    try:
        await delete_session(
            session_id=session_id,
            user_id=str(user.id),
        )
        return {
            "message": "채팅이 삭제되었습니다",
            "session_id": session_id,
        }
    except Exception as e:
        logger.error(f"세션 삭제 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"세션 삭제 오류: {str(e)}",
        )


# ─────────────────────────────────────────────────────────────────────────
#  GET /api/chats/{session_id}/history — 대화 히스토리 조회
# ─────────────────────────────────────────────────────────────────────────

@router.get("/chats/{session_id}/history")
async def get_history(
    session_id: str,
    user=Depends(get_authenticated_user),
):
    """
    특정 세션의 전체 대화 히스토리를 반환합니다.

    user_id 를 함께 확인하여 본인의 세션만 조회 가능합니다.
    프론트엔드 switch_session() 에서 호출됩니다.

    Returns:
        {
            "session_id": "...",
            "messages": [
                {"role": "user", "content": "...", "sources": []},
                {"role": "assistant", "content": "...", "sources": [...]},
                ...
            ]
        }
    """
    try:
        messages = await get_session_messages(
            session_id=session_id,
            user_id=str(user.id),
        )
        return {
            "session_id": session_id,
            "messages": messages,
        }
    except Exception as e:
        logger.error(f"히스토리 조회 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"히스토리 조회 오류: {str(e)}",
        )
