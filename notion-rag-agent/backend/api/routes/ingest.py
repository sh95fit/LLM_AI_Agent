# =============================================================================
#  backend/api/routes/ingest.py
#
#  Notion 동기화 & 파일 업로드 API
#
#  엔드포인트:
#    POST /api/ingest          Notion 동기화 시작 (백그라운드 스레드)
#    GET  /api/ingest/status   동기화 진행 상태 조회
#    POST /api/ingest/cancel   진행 중인 동기화 중단 요청
#    POST /api/ingest/file     PDF 등 파일 업로드 → 벡터 스토어 저장
#
#  핵심 설계:
#    ┌──────────────────────────────────────────────────────────────────┐
#    │  동기화를 run_in_executor() 로 별도 스레드에서 실행합니다.        │
#    │  이로써 FastAPI 이벤트 루프가 차단되지 않고,                      │
#    │  동기화 중에도 /status, /chat 등 모든 API 가 즉시 응답합니다.     │
#    │                                                                  │
#    │  중단 요청은 threading.Event 로 워커 스레드에 전달됩니다.         │
#    │  워커는 매 페이지 처리 전에 이 이벤트를 확인합니다.               │
#    └──────────────────────────────────────────────────────────────────┘
#
#  인증:
#    모든 엔드포인트는 Authorization: Bearer {JWT} 헤더가 필요합니다.
#    chat.py 와 동일한 get_authenticated_user() 패턴을 사용합니다.
# =============================================================================

import os
import logging
import tempfile
import threading
import asyncio
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, UploadFile, File, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

from models.database import get_db
from services.auth_service import get_current_user, get_token_from_header
from services.file_parser import FileParser
from core.vector_store import get_vector_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
#  공통 인증 헬퍼 (chat.py 와 동일한 패턴)
# ─────────────────────────────────────────────────────────────────────────────

async def get_authenticated_user(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Authorization 헤더에서 JWT 를 추출하고 DB 에서 사용자를 조회합니다.

    chat.py 의 동일 함수와 같은 로직이지만,
    ingest 라우터 내에서 독립적으로 정의하여 import 순환을 방지합니다.
    """
    token = get_token_from_header(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="인증 토큰이 없습니다")

    user = await get_current_user(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다")

    return user


# ─────────────────────────────────────────────────────────────────────────────
#  전역 상태
# ─────────────────────────────────────────────────────────────────────────────

# 동기화 진행 상태를 저장하는 딕셔너리
# 프론트엔드가 GET /ingest/status 로 주기적으로 조회합니다.
# status 값: "idle" | "running" | "done" | "cancelled" | "failed"
_sync_status: dict = {"status": "idle"}

# 스레드 안전한 취소 플래그
# POST /ingest/cancel 이 호출되면 set() 되고,
# 워커 스레드가 매 페이지 처리 전에 is_set() 으로 확인합니다.
# asyncio.Event 가 아닌 threading.Event 를 사용하는 이유:
#   워커가 별도 스레드에서 실행되므로 스레드 안전한 이벤트가 필요합니다.
_cancel_event = threading.Event()

# 동기화 전용 스레드풀 (워커 1개 — 동시에 2개 이상 동기화 방지)
_executor = ThreadPoolExecutor(max_workers=1)


# ─────────────────────────────────────────────────────────────────────────────
#  GET /ingest/status — 동기화 상태 조회
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/ingest/status")
async def get_sync_status(user=Depends(get_authenticated_user)):
    """
    현재 동기화 상태를 반환합니다.

    _sync_status dict 를 그대로 반환하므로 이벤트 루프를 차단하지 않고
    즉시 응답합니다. 동기화 중에도 이 엔드포인트는 항상 빠르게 응답합니다.

    반환 예시 (running 상태):
        {
            "status": "running",
            "started_at": "2025-01-15T10:30:00",
            "synced_count": 3,
            "failed_count": 0,
            "progress": "4/15",
            "current_page": "12월 4주차 회의록"
        }
    """
    return _sync_status


# ─────────────────────────────────────────────────────────────────────────────
#  POST /ingest — 동기화 시작
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest")
async def start_sync(user=Depends(get_authenticated_user)):
    """
    Notion 동기화를 백그라운드 스레드에서 시작합니다.

    이미 실행 중이면 409 Conflict 를 반환합니다.

    동작 흐름:
      1. 중복 실행 확인 → 409
      2. 취소 플래그 초기화
      3. 상태를 "running" 으로 전환
      4. run_in_executor() 로 별도 스레드에서 _run_sync_blocking() 실행
      5. 즉시 200 응답 반환 (동기화는 백그라운드에서 계속 진행)
    """
    global _sync_status

    # 중복 실행 방지
    if _sync_status.get("status") == "running":
        return JSONResponse(
            {"message": "이미 동기화가 진행 중입니다.", "status": "running"},
            status_code=409,
        )

    # 취소 플래그 초기화
    _cancel_event.clear()

    # 상태를 running 으로 전환
    _sync_status = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "synced_count": 0,
        "failed_count": 0,
        "progress": "",
        "current_page": "",
    }

    # 별도 스레드에서 실행 → 이벤트 루프 차단 없음
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_sync_blocking)

    logger.info("🔄 Notion 동기화 시작 요청됨 (백그라운드 스레드)")
    return {"message": "동기화가 시작되었습니다.", "status": "running"}


# ─────────────────────────────────────────────────────────────────────────────
#  POST /ingest/cancel — 동기화 중단
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest/cancel")
async def cancel_sync(user=Depends(get_authenticated_user)):
    """
    진행 중인 동기화를 중단 요청합니다.

    즉시 중단되지는 않습니다.
    워커 스레드가 현재 처리 중인 페이지를 완료한 후,
    다음 페이지 처리 전에 _cancel_event 를 확인하여 중단합니다.

    진행 중인 동기화가 없으면 400 을 반환합니다.
    """
    if _sync_status.get("status") != "running":
        return JSONResponse(
            {"message": "진행 중인 동기화가 없습니다."},
            status_code=400,
        )

    _cancel_event.set()
    logger.info("🛑 동기화 중단 요청됨")
    return {"message": "동기화 중단 요청이 전달되었습니다.", "status": "cancelling"}


# ─────────────────────────────────────────────────────────────────────────────
#  POST /ingest/file — 파일 업로드
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/ingest/file")
async def upload_file(
    file: UploadFile = File(...),
    user=Depends(get_authenticated_user),
):
    """
    PDF 등 파일을 업로드하여 벡터 스토어에 저장합니다.

    처리 흐름:
      1. 업로드된 파일을 임시 파일로 저장
      2. FileParser 로 텍스트 추출 (PDF: PyMuPDF, Word: python-docx 등)
      3. RecursiveCharacterTextSplitter 로 800자 단위 청킹
      4. 각 청크를 Document 객체로 생성 (파일명을 제목으로 포함)
      5. Qdrant 벡터 스토어에 임베딩 후 저장
      6. 임시 파일 삭제
    """
    try:
        # ── 1. 임시 파일로 저장 ──────────────────────────────────
        suffix = os.path.splitext(file.filename)[1] or ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # ── 2. 텍스트 추출 ───────────────────────────────────
            parser = FileParser()
            text = await parser.parse(tmp_path, file.content_type or "")

            if not text or len(text.strip()) < 10:
                return JSONResponse(
                    {"detail": "파일에서 텍스트를 추출할 수 없습니다."},
                    status_code=400,
                )

            # ── 3. 텍스트 청킹 ───────────────────────────────────
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=800,
                chunk_overlap=100,
                separators=["\n\n", "\n", "。", ".", " ", ""],
            )
            chunks = splitter.split_text(text)

            # ── 4. Document 생성 ─────────────────────────────────
            # 파일명을 본문에 포함하여 검색 시 출처 식별 가능
            docs = [
                Document(
                    page_content=f"[{file.filename}]\n\n{chunk}",
                    metadata={
                        "source_id": file.filename,
                        "title": file.filename,
                        "chunk_index": i,
                        "type": "uploaded_file",
                    },
                )
                for i, chunk in enumerate(chunks)
            ]

            # ── 5. 벡터 스토어에 저장 ────────────────────────────
            vector_store = get_vector_store()
            vector_store.add_documents(docs)

            logger.info(
                f"📁 파일 업로드 완료: '{file.filename}' → {len(docs)}개 청크"
            )
            return {
                "message": f"파일 '{file.filename}' 업로드 완료",
                "chunks": len(docs),
            }

        finally:
            # ── 6. 임시 파일 삭제 ────────────────────────────────
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"파일 업로드 실패: {e}", exc_info=True)
        return JSONResponse({"detail": str(e)}, status_code=500)


# ═════════════════════════════════════════════════════════════════════════════
#  내부 함수 — 별도 스레드에서 실행
# ═════════════════════════════════════════════════════════════════════════════

def _run_sync_blocking():
    """
    ThreadPoolExecutor 에서 호출되는 동기화 메인 함수.

    이 함수는 async 가 아닌 동기(blocking) 코드입니다.
    별도 스레드에서 실행되므로 FastAPI 이벤트 루프를 차단하지 않습니다.

    동작:
      1. NotionService 와 벡터 스토어 인스턴스 생성
      2. sync_to_vectorstore_sync() 호출
         - _cancel_event 전달 → 페이지별 중단 지원
         - _sync_status 전달 → 진행 상황 실시간 갱신
      3. 결과에 따라 _sync_status 를 done / cancelled / failed 로 갱신
    """
    global _sync_status

    try:
        # 순환참조 방지를 위해 함수 내부에서 import
        from services.notion_service import NotionService

        vector_store = get_vector_store()
        notion_service = NotionService()

        # 동기 버전 호출 — cancel_event 와 status_dict 전달
        result = notion_service.sync_to_vectorstore_sync(
            vector_store=vector_store,
            cancel_event=_cancel_event,
            status_dict=_sync_status,
        )

        # ── 결과에 따라 최종 상태 갱신 ───────────────────────────
        if _cancel_event.is_set():
            _sync_status.update({
                "status": "cancelled",
                "finished_at": datetime.now().isoformat(),
                "synced_count": result.get("synced_count", 0),
                "failed_count": result.get("failed_count", 0),
                "current_page": "",
                "progress": "",
                "message": "사용자에 의해 동기화가 중단되었습니다.",
            })
            logger.info(
                f"🛑 동기화 중단 완료 — 처리: {result.get('synced_count', 0)}개"
            )
        else:
            _sync_status.update({
                "status": "done",
                "finished_at": datetime.now().isoformat(),
                "synced_count": result.get("synced_count", 0),
                "failed_count": result.get("failed_count", 0),
                "current_page": "",
                "progress": "",
            })
            logger.info(
                f"✅ 동기화 완료 — "
                f"성공: {result.get('synced_count', 0)}개, "
                f"실패: {result.get('failed_count', 0)}개"
            )

    except Exception as e:
        _sync_status.update({
            "status": "failed",
            "finished_at": datetime.now().isoformat(),
            "error": str(e),
            "current_page": "",
            "progress": "",
        })
        logger.error(f"❌ 동기화 실패: {e}", exc_info=True)
