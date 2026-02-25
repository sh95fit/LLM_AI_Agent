# =============================================================================
#  backend/services/notion_service.py
#
#  Notion API 연동 서비스
#
#  ┌─ 기능 ─────────────────────────────────────────────────────────────────┐
#  │  • Notion 워크스페이스에서 페이지 목록 수집                             │
#  │  • 페이지 본문(블록) 텍스트 추출                                       │
#  │  • 텍스트 청킹 → Qdrant 벡터 스토어 저장                               │
#  │  • 동기(sync) / 비동기(async) 두 버전 제공                              │
#  │  • 중단(cancel) 및 실시간 진행률(status_dict) 지원                      │
#  └────────────────────────────────────────────────────────────────────────┘
#
#  Notion API 흐름:
#    1. databases/{id}/query  → 특정 DB 의 페이지 목록 조회
#       (실패 시 search API 로 폴백 → 전체 워크스페이스 페이지 검색)
#    2. blocks/{page_id}/children → 페이지 본문 블록 조회
#    3. 블록에서 rich_text 추출 → 텍스트 결합
#    4. RecursiveCharacterTextSplitter 로 800자 단위 청킹
#    5. 기존 벡터 삭제 (중복 방지) → 새 벡터 저장
#
#  동기 vs 비동기:
#    - sync_to_vectorstore_sync() : 별도 스레드에서 실행 (ingest.py 용)
#    - sync_to_vectorstore()      : async 래퍼 (하위 호환용)
#
#  설정 값 (config.py / .env):
#    NOTION_TOKEN        : Notion Integration 토큰
#    NOTION_DATABASE_IDS : 쉼표 구분 DB ID 목록 (비어있으면 search 폴백)
# =============================================================================

import logging
import threading
import requests as sync_requests    # 동기 HTTP (스레드에서 사용)

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import get_settings
from core.vector_store import delete_vectors_by_source

logger = logging.getLogger(__name__)
settings = get_settings()


class NotionService:
    """
    Notion API 를 통해 페이지를 가져오고 벡터 스토어에 동기화하는 서비스.

    사용 예:
        notion_service = NotionService()
        result = notion_service.sync_to_vectorstore_sync(vector_store)
        # result = {"synced_count": 10, "failed_count": 2}

    Attributes:
        token:          Notion Integration 토큰
        database_ids:   조회할 DB ID 목록
        headers:        Notion API 공통 요청 헤더
        text_splitter:  텍스트를 800자 단위로 분할하는 스플리터
    """

    def __init__(self):
        # ── Notion 토큰 설정 ─────────────────────────────────────
        # NOTION_TOKEN 이 있으면 사용, 없으면 NOTION_API_KEY 를 폴백
        self.token = settings.NOTION_TOKEN or settings.NOTION_API_KEY

        # ── DB ID 목록 설정 ──────────────────────────────────────
        # NOTION_DATABASE_IDS (쉼표 구분) 가 있으면 사용
        # 없으면 NOTION_DATABASE_ID (단일) 를 사용
        raw_ids = getattr(settings, "NOTION_DATABASE_IDS", "") or ""
        if raw_ids.strip():
            self.database_ids = [
                db_id.strip() for db_id in raw_ids.split(",") if db_id.strip()
            ]
        else:
            single_id = getattr(settings, "NOTION_DATABASE_ID", "") or ""
            self.database_ids = [single_id.strip()] if single_id.strip() else []

        # ── Notion API 공통 헤더 ─────────────────────────────────
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2026-02-25",
            "Content-Type": "application/json",
        }

        # ── 텍스트 스플리터 설정 ─────────────────────────────────
        # chunk_size=800 : 300에서 800으로 증가 → 컨텍스트 손실 방지
        # chunk_overlap=100 : 청크 간 100자 겹침 → 문맥 연결 유지
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=100,
            separators=["\n\n", "\n", "。", ".", " ", ""],
        )

    # ═════════════════════════════════════════════════════════════════════
    #  공통 헬퍼
    # ═════════════════════════════════════════════════════════════════════

    def _extract_title(self, page: dict) -> str:
        """
        Notion 페이지 객체에서 제목을 추출합니다.

        Notion 페이지의 properties 중 type="title" 인 속성을 찾아
        plain_text 를 결합하여 반환합니다.

        Args:
            page: Notion API 가 반환한 페이지 객체

        Returns:
            페이지 제목 문자열. 제목이 없으면 "제목 없음".

        예시 (Notion 페이지 구조):
            {
                "properties": {
                    "이름": {
                        "type": "title",
                        "title": [{"plain_text": "12월 4주차 회의록"}]
                    }
                }
            }
            → "12월 4주차 회의록"
        """
        properties = page.get("properties", {})

        for prop_name, prop_value in properties.items():
            if prop_value.get("type") == "title":
                title_list = prop_value.get("title", [])
                if title_list:
                    return "".join(
                        t.get("plain_text", "") for t in title_list
                    ).strip() or "제목 없음"

        return "제목 없음"

    def _extract_text_from_blocks(self, blocks: list) -> str:
        """
        Notion 블록 목록에서 텍스트를 추출합니다.

        Notion 의 모든 블록 타입(paragraph, heading, list 등)에서
        rich_text 배열의 plain_text 를 추출하여 줄바꿈으로 연결합니다.

        Args:
            blocks: Notion blocks/children API 가 반환한 블록 리스트

        Returns:
            추출된 텍스트 (줄바꿈으로 연결)
        """
        texts = []

        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})

            # rich_text 가 있는 블록에서 텍스트 추출
            rich_texts = block_data.get("rich_text", [])
            for rt in rich_texts:
                text = rt.get("plain_text", "")
                if text:
                    texts.append(text)

        return "\n".join(texts)

    # ═════════════════════════════════════════════════════════════════════
    #  비동기(async) 버전 — 기존 호환용
    # ═════════════════════════════════════════════════════════════════════

    async def sync_to_vectorstore(self, vector_store):
        """
        비동기 버전의 동기화 메서드 (기존 호환용).

        실제로는 내부에서 동기 버전을 호출합니다.
        새로운 코드에서는 sync_to_vectorstore_sync() 를 직접 사용하세요.

        Args:
            vector_store: QdrantVectorStore 인스턴스

        Returns:
            {"synced_count": int, "failed_count": int}
        """
        result = self.sync_to_vectorstore_sync(vector_store)
        return result

    # ═════════════════════════════════════════════════════════════════════
    #  동기(blocking) 버전 — 별도 스레드에서 실행
    # ═════════════════════════════════════════════════════════════════════

    def sync_to_vectorstore_sync(
        self,
        vector_store,
        cancel_event: threading.Event = None,
        status_dict: dict = None,
    ):
        """
        동기(blocking) 동기화 메서드.

        ThreadPoolExecutor 의 워커 스레드에서 호출됩니다.
        이벤트 루프를 차단하지 않도록 동기 HTTP (requests) 만 사용합니다.

        처리 흐름:
          1. Notion API 에서 전체 페이지 목록 수집
          2. 각 페이지에 대해:
             a. 취소 이벤트 확인 → 설정되었으면 즉시 중단
             b. 페이지 본문 텍스트 추출
             c. 기존 벡터 삭제 (중복 방지)
             d. 텍스트를 800자 단위로 분할
             e. 각 청크에 제목을 포함하여 Document 생성
             f. Qdrant 벡터 스토어에 저장
          3. 결과 반환

        Args:
            vector_store:  QdrantVectorStore 인스턴스
            cancel_event:  threading.Event — set() 되면 중단
                          (None 이면 중단 없이 전체 처리)
            status_dict:   진행 상황을 실시간으로 갱신할 dict
                          ingest.py 의 _sync_status 가 전달됩니다.
                          (None 이면 갱신 없음)

        Returns:
            {"synced_count": 성공 페이지 수, "failed_count": 실패 페이지 수}
        """
        synced_count = 0
        failed_count = 0

        # ── 1) 페이지 목록 수집 ──────────────────────────────────
        try:
            pages = self._fetch_all_pages_sync()
        except Exception as e:
            logger.error(f"페이지 목록 수집 실패: {e}", exc_info=True)
            return {"synced_count": 0, "failed_count": 0}

        total = len(pages)
        logger.info(f"📄 동기화 대상 페이지: {total}개")

        # ── 2) 페이지별 처리 ─────────────────────────────────────
        for idx, page in enumerate(pages, start=1):

            # ── 2a. 취소 확인 ────────────────────────────────────
            # 매 페이지 처리 전에 cancel_event 를 확인합니다.
            # 사용자가 "중단" 버튼을 누르면 이 이벤트가 set 됩니다.
            if cancel_event and cancel_event.is_set():
                logger.info(
                    f"🛑 동기화 중단 — {idx - 1}/{total} 처리 후 중단됨"
                )
                break

            page_id = page.get("id", "")
            title = self._extract_title(page)

            # 실시간 상태 갱신 (프론트엔드가 /ingest/status 로 조회)
            if status_dict is not None:
                status_dict.update({
                    "current_page": title,
                    "progress": f"{idx}/{total}",
                    "synced_count": synced_count,
                    "failed_count": failed_count,
                })

            try:
                # ── 2b. 페이지 본문 추출 ─────────────────────────
                content = self._get_page_content_sync(page_id)

                # 내용이 너무 적으면 건너뜀 (10자 미만)
                if not content or len(content.strip()) < 10:
                    logger.debug(f"  ⏭ '{title}' — 내용 부족, 건너뜀")
                    continue

                # ── 2c. 기존 벡터 삭제 (중복 방지) ───────────────
                # 같은 페이지를 다시 동기화할 때 이전 벡터가 남아있으면
                # 중복 결과가 검색되므로 먼저 삭제합니다.
                delete_vectors_by_source(page_id)

                # ── 2d. 텍스트 분할 ──────────────────────────────
                chunks = self.text_splitter.split_text(content)

                # ── 2e. Document 생성 ────────────────────────────
                # 제목을 본문 앞에 포함: "[12월 4주차 회의록]\n\n본문..."
                # → 벡터 검색 시 제목 키워드로도 매칭 가능
                docs = []
                for i, chunk in enumerate(chunks):
                    docs.append(Document(
                        page_content=f"[{title}]\n\n{chunk}",
                        metadata={
                            "source_id": page_id,       # Notion 페이지 ID
                            "title": title,              # 페이지 제목
                            "chunk_index": i,            # 청크 순서
                            "type": "notion_page",       # 출처 유형
                        },
                    ))

                # ── 2f. 벡터 스토어에 저장 ───────────────────────
                if docs:
                    vector_store.add_documents(docs)
                    synced_count += 1
                    logger.info(
                        f"  ✅ [{idx}/{total}] '{title}' → {len(docs)}개 청크"
                    )

            except Exception as e:
                failed_count += 1
                logger.error(
                    f"  ❌ [{idx}/{total}] '{title}' 처리 실패: {e}"
                )

        return {"synced_count": synced_count, "failed_count": failed_count}

    # ═════════════════════════════════════════════════════════════════════
    #  동기 헬퍼 — Notion API 호출 (requests 라이브러리)
    # ═════════════════════════════════════════════════════════════════════

    def _fetch_all_pages_sync(self) -> list:
        """
        Notion API 에서 모든 페이지를 동기적으로 가져옵니다.

        2단계 폴백 전략:
          1차: database_ids 로 databases/{id}/query 시도
               → 특정 DB 의 페이지만 정확히 가져옴
          2차: 전체 실패 시 search API 로 폴백
               → 워크스페이스 전체에서 페이지 검색

        페이지네이션을 지원하여 100개 이상의 페이지도 모두 가져옵니다.

        Returns:
            Notion 페이지 객체 리스트
        """
        all_pages = []
        db_success = False

        # ── 1차: databases.query() 시도 ──────────────────────────
        for db_id in self.database_ids:
            try:
                url = f"https://api.notion.com/v1/databases/{db_id}/query"
                has_more = True
                start_cursor = None

                while has_more:
                    body = {}
                    if start_cursor:
                        body["start_cursor"] = start_cursor

                    resp = sync_requests.post(
                        url,
                        headers=self.headers,
                        json=body,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    all_pages.extend(data.get("results", []))
                    has_more = data.get("has_more", False)
                    start_cursor = data.get("next_cursor")

                db_success = True
                logger.info(
                    f"  DB [{db_id[:8]}...] → "
                    f"{len(data.get('results', []))}개 페이지"
                )

            except Exception as e:
                logger.warning(
                    f"  DB [{db_id[:8]}...] 조회 실패: {e} "
                    f"(DB가 아닌 페이지 ID이거나 접근 권한 없음)"
                )

        # ── 2차: search API 폴백 ─────────────────────────────────
        if not db_success:
            logger.info(
                "databases.query() 전체 실패 → search() API로 폴백합니다."
            )

            try:
                url = "https://api.notion.com/v1/search"
                has_more = True
                start_cursor = None

                while has_more:
                    body = {
                        "filter": {
                            "property": "object",
                            "value": "page",
                        }
                    }
                    if start_cursor:
                        body["start_cursor"] = start_cursor

                    resp = sync_requests.post(
                        url,
                        headers=self.headers,
                        json=body,
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    all_pages.extend(data.get("results", []))
                    has_more = data.get("has_more", False)
                    start_cursor = data.get("next_cursor")

                logger.info(f"  search() API → {len(all_pages)}개 페이지")

            except Exception as e:
                logger.error(
                    f"search API 폴백도 실패: {e}", exc_info=True
                )

        return all_pages

    def _get_page_content_sync(self, page_id: str) -> str:
        """
        특정 페이지의 블록 내용을 동기적으로 가져와 텍스트로 변환합니다.

        Notion 의 blocks/{page_id}/children API 를 호출하여
        모든 블록의 텍스트를 추출합니다.
        페이지네이션을 지원하여 블록이 100개 이상이어도 모두 가져옵니다.

        Args:
            page_id: Notion 페이지 ID

        Returns:
            블록 텍스트를 줄바꿈으로 연결한 문자열
        """
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"

        # GET 요청에는 Content-Type 이 필요 없으므로 별도 헤더 사용
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
        }

        all_blocks = []
        has_more = True
        start_cursor = None

        while has_more:
            params = {}
            if start_cursor:
                params["start_cursor"] = start_cursor

            try:
                resp = sync_requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(
                    f"블록 조회 실패 (page={page_id[:8]}...): {e}"
                )
                break

            all_blocks.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        return self._extract_text_from_blocks(all_blocks)
