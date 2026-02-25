FROM python:3.11-slim

# ─── 작업 디렉토리 설정 ───────────────────────────────────────
# 컨테이너 내부에서 명령어가 실행될 기본 경로
WORKDIR /app

# ─── 시스템 패키지 설치 ───────────────────────────────────────
# curl: 헬스체크 및 디버깅용
# build-essential: 일부 Python 패키지 컴파일에 필요
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*   
    # apt 캐시 삭제로 이미지 크기 최소화

# ─── Python 패키지 설치 ──────────────────────────────────────
# requirements.txt 먼저 복사 후 설치
# (코드 변경 시 패키지 재설치를 막는 Docker 레이어 캐시 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --no-cache-dir: pip 캐시 저장 안 함 (이미지 크기 절약)

# ─── 앱 소스 코드 복사 ───────────────────────────────────────
# . (현재 디렉토리 = frontend/) 의 모든 파일을 /app 에 복사
COPY . .

# ─── Streamlit 포트 개방 ─────────────────────────────────────
# Streamlit 기본 포트 8501을 외부에 노출
EXPOSE 8501

# ─── 헬스체크 ────────────────────────────────────────────────
# 30초마다 Streamlit 서버 상태 확인
# Docker가 컨테이너 상태를 healthy/unhealthy로 관리
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# ─── 실행 명령어 ─────────────────────────────────────────────
# Streamlit 앱 실행 옵션 설명:
#   --server.port=8501         : 포트 지정
#   --server.address=0.0.0.0   : 모든 IP에서 접근 허용 (컨테이너 외부 접근 필수)
#   --server.headless=true     : 브라우저 자동 실행 안 함 (서버 환경)
#   --server.fileWatcherType=none : 파일 변경 감지 비활성화 (프로덕션 안정성)
#   --browser.gatherUsageStats=false : Streamlit 사용 통계 수집 비활성화
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.fileWatcherType=none", \
     "--browser.gatherUsageStats=false"]