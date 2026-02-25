# backend/core/memory.py

import json
import uuid
import logging
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from config import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)

# Redis TTL: 30일
SESSION_TTL = 60 * 60 * 24 * 30

# ── Redis 클라이언트 싱글톤 ────────────────────────────────────────────────────
_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Redis 클라이언트 반환 (싱글톤)"""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(
            host=getattr(settings, "REDIS_HOST", "redis"),
            port=getattr(settings, "REDIS_PORT", 6379),
            db=getattr(settings, "REDIS_DB", 0),
            decode_responses=True
        )
    return _redis_client


# ── 세션 생성 ─────────────────────────────────────────────────────────────────
async def create_session(user_id: str) -> str:
    """
    새 채팅 세션 생성
    - Redis key: user:{user_id}:sessions → session_id 집합
    - Redis key: session:{session_id}:meta → 세션 메타데이터
    """
    r = await get_redis()
    session_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    # 세션 메타데이터 저장
    meta = {
        "session_id": session_id,
        "user_id": user_id,
        "title": "새 채팅",
        "created_at": now,
        "updated_at": now,
    }
    await r.hset(f"session:{session_id}:meta", mapping=meta)
    await r.expire(f"session:{session_id}:meta", SESSION_TTL)

    # 사용자의 세션 목록에 추가
    await r.zadd(
        f"user:{user_id}:sessions",
        {session_id: datetime.utcnow().timestamp()}
    )
    await r.expire(f"user:{user_id}:sessions", SESSION_TTL)

    logger.info(f"새 세션 생성: {session_id} (user: {user_id})")
    return session_id


# ── 세션 삭제 ─────────────────────────────────────────────────────────────────
async def delete_session(session_id: str, user_id: str):
    """세션 및 관련 메시지 삭제"""
    r = await get_redis()

    await r.delete(f"session:{session_id}:meta")
    await r.delete(f"session:{session_id}:messages")
    await r.zrem(f"user:{user_id}:sessions", session_id)

    logger.info(f"세션 삭제: {session_id}")


# ── 전체 세션 목록 조회 ───────────────────────────────────────────────────────
async def get_all_sessions(user_id: str) -> list:
    """사용자의 전체 세션 목록 반환 (최신순)"""
    r = await get_redis()

    # 최신순으로 세션 ID 목록 가져오기
    session_ids = await r.zrevrange(f"user:{user_id}:sessions", 0, -1)

    sessions = []
    for sid in session_ids:
        meta = await r.hgetall(f"session:{sid}:meta")
        if meta:
            sessions.append({
                "session_id": sid,
                "title": meta.get("title", "새 채팅"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
            })

    return sessions


# ── 메시지 저장 ───────────────────────────────────────────────────────────────
async def save_message(
    session_id: str,
    role: str,
    content: str,
    sources: list = None
):
    """
    메시지 Redis 리스트에 저장
    - Redis key: session:{session_id}:messages → JSON 문자열 리스트
    """
    r = await get_redis()

    message = {
        "role": role,
        "content": content,
        "sources": sources or [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    # 리스트 오른쪽에 추가 (시간순)
    await r.rpush(
        f"session:{session_id}:messages",
        json.dumps(message, ensure_ascii=False)
    )
    await r.expire(f"session:{session_id}:messages", SESSION_TTL)

    # 첫 번째 사용자 메시지로 세션 제목 업데이트
    if role == "user":
        msg_count = await r.llen(f"session:{session_id}:messages")
        if msg_count == 1:
            title = content[:30] + ("..." if len(content) > 30 else "")
            await r.hset(f"session:{session_id}:meta", "title", title)

        # updated_at 갱신
        await r.hset(
            f"session:{session_id}:meta",
            "updated_at",
            datetime.utcnow().isoformat()
        )


# ── 세션 전체 메시지 조회 ─────────────────────────────────────────────────────
async def get_session_messages(session_id: str, user_id: str = None) -> list:
    """세션의 전체 메시지 목록 반환"""
    r = await get_redis()

    raw_messages = await r.lrange(f"session:{session_id}:messages", 0, -1)

    messages = []
    for raw in raw_messages:
        try:
            messages.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    return messages


# ── 최근 N개 메시지 조회 (RAG 컨텍스트용) ────────────────────────────────────
async def get_recent_messages(session_id: str, limit: int = 10) -> list:
    """최근 N개 메시지 반환 (LLM 컨텍스트 히스토리용)"""
    r = await get_redis()

    # 리스트 끝에서 limit개 가져오기
    raw_messages = await r.lrange(
        f"session:{session_id}:messages",
        -limit,
        -1
    )

    messages = []
    for raw in raw_messages:
        try:
            msg = json.loads(raw)
            messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
            })
        except json.JSONDecodeError:
            continue

    return messages
