from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from core.scheduler import start_scheduler
from api.routes import chat, ingest, auth, webhook

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시 스케줄러 가동
    start_scheduler()
    yield
    # 앱 종료 시 정리 작업

app = FastAPI(lifespan=lifespan)

# CORS 설정 (프론트엔드 연동)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(chat.router, prefix="/api")
app.include_router(ingest.router, prefix="/api")
app.include_router(auth.router)
app.include_router(webhook.router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}