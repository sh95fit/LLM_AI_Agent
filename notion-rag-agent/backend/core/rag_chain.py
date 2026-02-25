# backend/core/rag_chain.py

import logging
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── LLM (모듈 로드 시 초기화) ─────────────────────────────────────────────────
llm = ChatOllama(
    base_url=settings.OLLAMA_BASE_URL,
    model=settings.OLLAMA_MODEL
)

# ── 프롬프트 ──────────────────────────────────────────────────────────────────
prompt_template = """당신은 회사의 지식 베이스를 담당하는 AI 비서입니다.
아래의 [Context]에 제공된 문서 내용을 바탕으로 질문에 답변하세요.
Context에 있는 정보를 최대한 활용하여 답변하고, 
Context에 정말로 관련 내용이 전혀 없는 경우에만 "관련 정보를 찾을 수 없습니다"라고 답하세요.

[Context]
{context}

[Question]
{question}

[Answer]
"""

prompt = ChatPromptTemplate.from_template(prompt_template)


def format_docs(docs) -> str:
    parts = []
    for doc in docs:
        source = doc.metadata.get("source", "Unknown")
        parts.append(f"Source: {source}\nContent: {doc.page_content}")
    return "\n\n".join(parts)


def get_sources(docs) -> list:
    sources = []
    seen = set()
    for doc in docs:
        url = doc.metadata.get("source", "")
        title = doc.metadata.get("title", "관련 문서")
        if url and url not in seen:
            sources.append({"url": url, "title": title})
            seen.add(url)
    return sources


async def generate_response(question: str, session_id: str = None) -> dict:
    """
    RAG 응답 생성 - vector_store를 함수 호출 시점에 초기화 (lazy)
    """
    try:
        # ✅ 함수 호출 시점에 import (모듈 로드 시 즉시 실행 방지)
        from core.vector_store import get_vector_store
        vector_store = get_vector_store()
        
        # score_threshold를 추가하여 관련 없는 문서 필터링
        retriever = vector_store.as_retriever(
            # search_type="similarity_score_threshold",
            search_type="similarity",  # threshold 없이 상위 k개 반환
            search_kwargs={
                "k": 8,
                # "score_threshold": 0.3   # 0.3 미만 유사도는 제외
                
            }
        )

        # 관련 문서 검색
        docs = await retriever.ainvoke(question)
        sources = get_sources(docs)
        context = format_docs(docs)

        # LLM 호출
        formatted_prompt = prompt.format(
            context=context,
            question=question
        )
        response = await llm.ainvoke(formatted_prompt)
        answer = response.content if hasattr(response, "content") else str(response)

        return {"answer": answer, "sources": sources}

    except Exception as e:
        logger.error(f"RAG 응답 생성 실패: {e}", exc_info=True)
        return {
            "answer": "죄송합니다, 답변 생성 중 오류가 발생했습니다.",
            "sources": []
        }
