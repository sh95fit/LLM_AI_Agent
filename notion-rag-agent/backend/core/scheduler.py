# =============================================================================
#  backend/core/scheduler.py
#
#  APScheduler 기반 주기적 Notion 동기화 스케줄러
#
#  변경 사항:
#    - QdrantVectorStore() 직접 호출 → get_vector_store() 사용
#    - NotionService.sync_to_vectorstore_sync() 동기 버전 호출
#    - 스케줄러 job 은 별도 스레드에서 실행되므로 이벤트 루프 차단 없음
# =============================================================================

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# 전역 스케줄러 인스턴스
scheduler = AsyncIOScheduler()


def start_scheduler(vector_store=None):
    """
    APScheduler 시작 — 주기적 Notion 동기화 등록

    Args:
        vector_store: QdrantVectorStore 인스턴스 (현재는 사용하지 않음,
                      job 내부에서 get_vector_store() 로 생성)
    """
    # 기존 job 중복 방지
    if scheduler.get_job("notion_sync"):
        scheduler.remove_job("notion_sync")

    scheduler.add_job(
        func=sync_notion_job,
        trigger=IntervalTrigger(hours=6),   # 6시간마다 실행
        id="notion_sync",
        name="Notion 정기 동기화",
        replace_existing=True,
        misfire_grace_time=300              # 5분 이내 지연은 허용
    )

    if not scheduler.running:
        scheduler.start()

    logger.info("⏰ Notion 동기화 스케줄러 시작 (6시간 간격)")


async def sync_notion_job():
    """
    스케줄러가 주기적으로 호출하는 동기화 함수

    내부에서 NotionService 와 벡터 스토어를 생성하여 사용합니다.
    get_vector_store() 를 통해 client, collection_name, embedding 이
    자동으로 설정됩니다.
    """
    try:
        logger.info("🔄 정기 Notion 동기화 시작...")

        # 내부에서 import (순환참조 방지)
        from services.notion_service import NotionService
        from core.vector_store import get_vector_store

        # get_vector_store() 는 client, collection_name, embedding 을
        # 모두 포함한 QdrantVectorStore 인스턴스를 반환합니다.
        vector_store = get_vector_store()
        notion_service = NotionService()

        # 동기 버전 호출 (스케줄러는 async 이지만 내부적으로 동기 작업)
        result = notion_service.sync_to_vectorstore_sync(vector_store)

        logger.info(
            f"✅ 정기 동기화 완료 - "
            f"성공: {result.get('synced_count', 0)}개, "
            f"실패: {result.get('failed_count', 0)}개"
        )

    except Exception as e:
        logger.error(f"❌ 정기 Notion 동기화 실패: {e}", exc_info=True)


def stop_scheduler():
    """스케줄러 종료 (앱 종료 시 호출)"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("⏹ 스케줄러 종료됨")
