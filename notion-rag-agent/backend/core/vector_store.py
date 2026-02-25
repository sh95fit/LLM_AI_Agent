from qdrant_client import QdrantClient
from qdrant_client.http import models
from langchain_ollama import OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore
from config import get_settings

settings = get_settings()

# Qdrant 클라이언트 연결
client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)

# 임베딩 모델 설정
embeddings = OllamaEmbeddings(
    base_url=settings.OLLAMA_BASE_URL,
    model=settings.OLLAMA_EMBED_MODEL
)

def init_collection():
    """컬렉션이 없으면 생성"""
    collections = client.get_collections().collections
    exists = any(c.name == settings.QDRANT_COLLECTION for c in collections)
    
    if not exists:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=models.VectorParams(
                # size=768,  # nomic-embed-text 차원 수
                size=1024,  # bge-m3 차원 수
                distance=models.Distance.COSINE
            )
        )

def get_vector_store():
    """LangChain 벡터 스토어 객체 반환"""
    init_collection()
    return QdrantVectorStore(
        client=client,
        collection_name=settings.QDRANT_COLLECTION,
        embedding=embeddings,
    )

def delete_vectors_by_source(source_id: str):
    """특정 소스(페이지)의 벡터 삭제 (업데이트 시 사용)"""
    client.delete(
        collection_name=settings.QDRANT_COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="metadata.source_id",
                        match=models.MatchValue(value=source_id)
                    )
                ]
            )
        )
    )