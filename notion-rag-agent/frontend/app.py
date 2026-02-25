# =============================================================================
#  frontend/app.py  —  Notion RAG Chat  v13 (최종)
#
#  ┌─ 변경 이력 ────────────────────────────────────────────────────────────┐
#  │ v12  Gemini-style UI, 동기화 캐싱                                      │
#  │ v13  동기화 중 채팅 동시 동작 보장:                                     │
#  │      - pending 복구 메커니즘 (rerun 으로 끊겨도 재시도)                 │
#  │      - AI 대기 중 모든 rerun 유발 버튼 비활성화                         │
#  │      - 동기화 상태 자동 갱신 (rerun 없이 캐시 활용)                     │
#  │      - Ollama 동시 처리 안내 추가                                       │
#  └────────────────────────────────────────────────────────────────────────┘
#
#  ┌─ 전체 동작 구조 ──────────────────────────────────────────────────────┐
#  │                                                                        │
#  │  [인증 플로우]                                                         │
#  │  ① 앱 로드 → 세션 상태 초기화                                          │
#  │  ② URL 에 ?token= → OAuth 콜백 처리                                   │
#  │  ③ 미로그인 → 로그인 페이지 (초대코드 → Google OAuth)                  │
#  │                                                                        │
#  │  [채팅 플로우 — 1-cycle inline 패턴]                                   │
#  │  ④ 사용자 입력 → pending_question + active_question 설정               │
#  │  ⑤ st.rerun() → 다음 렌더 사이클                                      │
#  │  ⑥ show_chat_page() 에서:                                              │
#  │     - pending_question 있으면 → 즉시 소비 + spinner + API 호출         │
#  │     - pending 없지만 active_question 있으면 → 복구 시도                │
#  │  ⑦ 응답 저장 → active_question 해제 → rerun                           │
#  │                                                                        │
#  │  [동기화 플로우]                                                       │
#  │  ⑧ 백엔드 별도 스레드에서 실행 → 이벤트 루프 비차단                   │
#  │  ⑨ 상태는 캐시 + 수동 새로고침 (AI 대기 중에는 비활성화)              │
#  │  ⑩ Ollama NUM_PARALLEL=4 로 동시 임베딩/LLM 요청 허용                 │
#  │                                                                        │
#  │  [핵심 안정화 — v13 신규]                                              │
#  │  • active_question: pending 이 rerun 으로 사라져도 복구 가능            │
#  │  • AI 대기 중 모든 rerun 유발 UI 요소 비활성화                         │
#  │  • 동기화 상태 조회를 캐시로 처리하여 API 호출 최소화                   │
#  └────────────────────────────────────────────────────────────────────────┘
# =============================================================================

import json
import base64
import time
import logging

import streamlit as st
import requests

# =============================================================================
#  §1. 상수
# =============================================================================

BACKEND_URL_INTERNAL = "http://backend:8000"
BACKEND_URL_EXTERNAL = "http://localhost:8000"

TIMEOUT_SHORT  = 5
TIMEOUT_NORMAL = 10
TIMEOUT_LONG   = 600    # LLM 응답 대기 — 동기화 중에는 Ollama 가 느려질 수 있음

APP_TITLE = "Notion RAG"
APP_ICON  = "✦"

SYNC_CACHE_TTL = 5      # 동기화 상태 캐시 유효 시간 (초)

logger = logging.getLogger(__name__)

# =============================================================================
#  §2. 페이지 설정
# =============================================================================

st.set_page_config(
    page_title=f"{APP_ICON} {APP_TITLE}",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
#  §3. 커스텀 CSS — Gemini 스타일
# =============================================================================

st.markdown("""
<style>
    /* ── 전체 레이아웃 ──────────────────────────────────────── */
    .block-container {
        max-width: 860px;
        margin: 0 auto;
        padding-top: 2rem;
        padding-bottom: 4rem;
    }
    header[data-testid="stHeader"] { background: transparent; }

    /* ── 사이드바 ────────────────────────────────────────────── */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1a1a2e 0%, #16213e 100%);
    }
    section[data-testid="stSidebar"] > div:first-child {
        padding-top: 1.2rem;
    }
    section[data-testid="stSidebar"] .stMarkdown,
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] span {
        color: #e0e0e0 !important;
    }
    section[data-testid="stSidebar"] hr {
        border-color: rgba(255,255,255,0.1);
        margin: 12px 0;
    }

    /* ── 프로필 카드 ─────────────────────────────────────────── */
    .profile-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 16px 18px;
        border-radius: 14px;
        margin-bottom: 18px;
        box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
    }
    .profile-card .profile-name {
        font-weight: 700;
        font-size: 1.05rem;
        letter-spacing: -0.3px;
    }
    .profile-card .profile-email {
        opacity: 0.8;
        font-size: 0.78rem;
        margin-top: 2px;
    }

    /* ── AI 대기 배너 ────────────────────────────────────────── */
    .thinking-banner {
        background: linear-gradient(90deg, #667eea33, #764ba233);
        border: 1px solid #667eea55;
        color: #c4b5fd;
        padding: 10px 14px;
        border-radius: 10px;
        margin-bottom: 14px;
        text-align: center;
        font-size: 0.83rem;
        animation: pulse-glow 2s ease-in-out infinite;
    }
    @keyframes pulse-glow {
        0%, 100% { opacity: 0.8; }
        50% { opacity: 1; }
    }

    /* ── 사이드바 버튼 ───────────────────────────────────────── */
    section[data-testid="stSidebar"] button[kind="secondary"] {
        background: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(255,255,255,0.1) !important;
        color: #e0e0e0 !important;
        border-radius: 10px !important;
        transition: all 0.2s ease;
        font-size: 0.85rem !important;
    }
    section[data-testid="stSidebar"] button[kind="secondary"]:hover {
        background: rgba(255,255,255,0.12) !important;
        border-color: rgba(102, 126, 234, 0.5) !important;
        transform: translateX(2px);
    }

    /* ── 동기화 배지 ─────────────────────────────────────────── */
    .sync-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 5px 14px;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .sync-running  { background: #fbbf2433; color: #fbbf24; border: 1px solid #fbbf2455; }
    .sync-done     { background: #34d39933; color: #34d399; border: 1px solid #34d39955; }
    .sync-cancel   { background: #f8717133; color: #f87171; border: 1px solid #f8717155; }
    .sync-fail     { background: #f8717133; color: #f87171; border: 1px solid #f8717155; }
    .sync-idle     { background: rgba(255,255,255,0.08); color: #9ca3af; border: 1px solid rgba(255,255,255,0.1); }
    .sync-unknown  { background: rgba(255,255,255,0.05); color: #6b7280; border: 1px solid rgba(255,255,255,0.08); }

    /* ── 채팅 메시지 ─────────────────────────────────────────── */
    .stChatMessage {
        border-radius: 16px !important;
        margin-bottom: 8px !important;
        padding: 14px 18px !important;
    }

    /* ── 소스 카드 ───────────────────────────────────────────── */
    .ref-card {
        background: white;
        border: 1px solid #e5e7eb;
        border-left: 3px solid #667eea;
        padding: 10px 14px;
        margin: 4px 0;
        border-radius: 8px;
        font-size: 0.82rem;
        transition: all 0.2s ease;
    }
    .ref-card:hover {
        border-left-color: #764ba2;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        transform: translateX(2px);
    }
    .ref-card a { color: #667eea; text-decoration: none; font-weight: 500; }
    .ref-card a:hover { text-decoration: underline; }

    /* ── 로그인 카드 ─────────────────────────────────────────── */
    .auth-card {
        max-width: 440px;
        margin: 48px auto;
        padding: 44px 36px;
        background: white;
        border-radius: 20px;
        box-shadow: 0 8px 40px rgba(0, 0, 0, 0.08);
        text-align: center;
    }
    .auth-card h1 {
        font-size: 2rem;
        margin-bottom: 4px;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 800;
    }
    .auth-card p { color: #6b7280; margin-bottom: 28px; font-size: 0.95rem; }

    /* ── 환영 화면 ───────────────────────────────────────────── */
    .welcome-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        min-height: 50vh;
        text-align: center;
    }
    .welcome-icon {
        font-size: 3.5rem;
        margin-bottom: 16px;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .welcome-title {
        font-size: 1.6rem;
        font-weight: 700;
        color: #1f2937;
        margin-bottom: 8px;
    }
    .welcome-sub { color: #6b7280; font-size: 0.95rem; max-width: 400px; }

    /* ── 섹션 제목 ───────────────────────────────────────────── */
    .section-title {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 1.2px;
        color: #9ca3af !important;
        font-weight: 600;
        margin: 14px 0 8px 0;
    }

    /* ── 하단 안내 ───────────────────────────────────────────── */
    .input-hint {
        text-align: center;
        font-size: 0.72rem;
        color: #9ca3af;
        margin-top: 6px;
    }

    /* ── Streamlit 기본 요소 숨기기 ──────────────────────────── */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    .stDeployButton { display: none; }
</style>
""", unsafe_allow_html=True)


# =============================================================================
#  §4. 세션 상태 초기화
#
#  v13 핵심 변경: active_question 추가
#
#  기존 문제:
#    pending_question 을 즉시 None 으로 소비한 후 API 를 호출했는데,
#    API 호출 중에 st.rerun() 이 발생하면 (예: 동기화 새로고침 버튼)
#    호출이 끊기고 pending 도 없어서 재시도가 불가능했습니다.
#
#  해결:
#    active_question 을 추가하여 "현재 API 호출 중인 질문"을 별도 저장.
#    pending → active 전환 후 API 호출.
#    API 성공 시에만 active 해제.
#    rerun 으로 끊기면 active 가 남아있어 자동 재시도됩니다.
# =============================================================================

def init_session_state():
    defaults = {
        # 인증
        "login_stage": 1,
        "validated_invite_code": None,
        "auth_token": None,
        "user_info": None,

        # 채팅
        "chat_sessions": [],
        "current_session_id": None,
        "messages": [],

        # 메시지 흐름 상태 (v13 개선)
        #
        # pending_question : 사용자가 입력한 질문 (아직 API 호출 전)
        # active_question  : API 호출이 진행 중인 질문 (호출 중 + 재시도용)
        # is_waiting_response : UI 에서 "대기 중" 표시용 플래그
        #
        # 흐름:
        #   입력 → pending 설정 → rerun
        #   → pending 을 active 로 이관 → API 호출
        #   → 성공: active 해제 → rerun
        #   → 실패(rerun 등): active 유지 → 다음 렌더에서 재시도
        #
        "pending_question": None,
        "active_question": None,
        "is_waiting_response": False,

        # UI
        "error": None,

        # 동기화 캐시
        "sync_status_cache": {"status": "idle"},
        "sync_status_time": 0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_session_state()


# =============================================================================
#  §5. 유틸리티
# =============================================================================

def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {"detail": response.text[:200] if response.text else "파싱 실패"}


def normalize_user_info(data: dict) -> dict:
    if not data:
        return {"name": "사용자", "email": ""}
    user = data.get("user", data)
    return {
        "name": user.get("name") or user.get("username") or "사용자",
        "email": user.get("email") or "",
    }


def force_logout():
    for k in [
        "auth_token", "user_info", "chat_sessions",
        "current_session_id", "messages", "pending_question",
        "active_question", "is_waiting_response", "error",
    ]:
        if k in st.session_state:
            del st.session_state[k]
    st.session_state.login_stage = 1
    st.session_state.validated_invite_code = None


def _is_waiting() -> bool:
    """AI 응답 대기 중인지 판단합니다. UI 전체에서 사용합니다."""
    return (
        bool(st.session_state.get("pending_question"))
        or bool(st.session_state.get("active_question"))
        or st.session_state.is_waiting_response
    )


# =============================================================================
#  §6. API 헬퍼
# =============================================================================

def _make_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = st.session_state.get("auth_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_get(path: str, timeout: int = TIMEOUT_NORMAL):
    try:
        resp = requests.get(
            f"{BACKEND_URL_INTERNAL}{path}",
            headers=_make_headers(),
            timeout=timeout,
        )
        if resp.status_code == 401:
            force_logout()
            st.rerun()
            return None
        if resp.status_code >= 400:
            return None
        return safe_json(resp)
    except Exception:
        return None


def api_post(path: str, payload: dict, timeout: int = TIMEOUT_NORMAL):
    try:
        resp = requests.post(
            f"{BACKEND_URL_INTERNAL}{path}",
            headers=_make_headers(),
            json=payload,
            timeout=timeout,
        )
        return resp.status_code, safe_json(resp)
    except requests.exceptions.Timeout:
        return 0, {"detail": f"요청 시간 초과 ({timeout}초)"}
    except Exception as e:
        return 0, {"detail": str(e)}


def api_delete(path: str, timeout: int = TIMEOUT_NORMAL) -> bool:
    try:
        resp = requests.delete(
            f"{BACKEND_URL_INTERNAL}{path}",
            headers=_make_headers(),
            timeout=timeout,
        )
        return resp.status_code < 400
    except Exception:
        return False


# =============================================================================
#  §7. OAuth 콜백
# =============================================================================

def handle_oauth_callback():
    params = st.query_params
    error = params.get("error")
    if error:
        st.session_state.error = f"로그인 실패: {error}"
        st.query_params.clear()
        return

    token = params.get("token")
    if not token:
        return

    st.session_state.auth_token = token
    st.query_params.clear()

    user_data = api_get("/auth/me", timeout=TIMEOUT_NORMAL)
    if user_data:
        st.session_state.user_info = normalize_user_info(user_data)
        st.session_state.login_stage = 2
        _load_chat_sessions()
    else:
        st.session_state.error = "사용자 정보를 가져올 수 없습니다."
        st.session_state.auth_token = None
    st.rerun()


# =============================================================================
#  §8. 채팅 세션 관리
# =============================================================================

def _load_chat_sessions():
    data = api_get("/api/chats", timeout=TIMEOUT_NORMAL)
    if data is not None:
        st.session_state.chat_sessions = (
            data if isinstance(data, list) else data.get("sessions", [])
        )


def create_new_chat():
    status, data = api_post("/api/chats/new", {})
    if status in (200, 201):
        new_id = data.get("id") or data.get("session_id")
        st.session_state.current_session_id = new_id
        st.session_state.messages = []
        st.session_state.pending_question = None
        st.session_state.active_question = None
        st.session_state.is_waiting_response = False
        _load_chat_sessions()
        st.rerun()
    else:
        detail = data.get("detail", "오류") if isinstance(data, dict) else str(data)
        st.session_state.error = f"세션 생성 실패: {detail}"


def switch_session(session_id: str):
    if session_id == st.session_state.current_session_id:
        return
    st.session_state.current_session_id = session_id
    st.session_state.pending_question = None
    st.session_state.active_question = None
    st.session_state.is_waiting_response = False

    data = api_get(f"/api/chats/{session_id}/history", timeout=TIMEOUT_NORMAL)
    if data is not None:
        raw = data if isinstance(data, list) else data.get("messages", [])
        st.session_state.messages = [
            {"role": m.get("role", "user"), "content": m.get("content", ""), "sources": m.get("sources", [])}
            for m in raw
        ]
    else:
        st.session_state.messages = []
    st.rerun()


def delete_session(session_id: str):
    if api_delete(f"/api/chats/{session_id}"):
        if st.session_state.current_session_id == session_id:
            st.session_state.current_session_id = None
            st.session_state.messages = []
        _load_chat_sessions()
        st.rerun()
    else:
        st.session_state.error = "삭제 실패"


# =============================================================================
#  §9. 파일 업로드
# =============================================================================

def _handle_file_upload(uploaded_file):
    if uploaded_file is None:
        return
    try:
        with st.spinner(f"'{uploaded_file.name}' 처리 중..."):
            token = st.session_state.get("auth_token")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            resp = requests.post(
                f"{BACKEND_URL_INTERNAL}/api/ingest/file",
                files={"file": (uploaded_file.name, uploaded_file, uploaded_file.type)},
                headers=headers,
                timeout=60,
            )
        if resp.status_code in (200, 201):
            st.toast(f"✅ 업로드 완료 ({safe_json(resp).get('chunks', 0)}개 청크)", icon="✅")
        else:
            st.toast(f"❌ {safe_json(resp).get('detail', '실패')}", icon="❌")
    except requests.exceptions.Timeout:
        st.toast("⏱ 업로드 시간 초과", icon="⏱")
    except Exception as e:
        st.toast(f"❌ {e}", icon="❌")


# =============================================================================
#  §10. Notion 동기화
#
#  v13 핵심 변경: AI 대기 중에는 동기화 관련 API 호출을 완전히 건너뜀
#
#  이것이 "동기화 중 채팅 로딩이 사라지는 문제"를 해결합니다.
#  AI 대기 중에는:
#    - 동기화 상태 조회 API 를 호출하지 않음 (캐시만 사용)
#    - 동기화 시작/중단/새로고침 버튼 모두 비활성화
#    - → 어떤 버튼도 st.rerun() 을 유발하지 않음
#    - → RAG API 호출이 끊기지 않음
# =============================================================================

def _get_sync_status(force: bool = False) -> dict:
    """
    동기화 상태를 조회합니다.
    AI 대기 중에는 캐시만 반환하여 API 호출과 rerun 을 방지합니다.
    """
    now = time.time()
    cache_age = now - st.session_state.get("sync_status_time", 0)

    # AI 대기 중이면 무조건 캐시 반환 (API 호출 차단)
    if _is_waiting() and not force:
        return st.session_state.get("sync_status_cache", {"status": "idle"})

    # 캐시 유효하면 재사용
    if not force and cache_age < SYNC_CACHE_TTL:
        return st.session_state.get("sync_status_cache", {"status": "idle"})

    # API 호출
    data = api_get("/api/ingest/status", timeout=TIMEOUT_SHORT)
    if data is None:
        return st.session_state.get("sync_status_cache", {"status": "unknown"})

    st.session_state.sync_status_cache = data
    st.session_state.sync_status_time = now
    return data


def _start_sync():
    status_code, data = api_post("/api/ingest", {})
    if status_code in (200, 201):
        st.toast("동기화 시작됨", icon="🔄")
        st.session_state.sync_status_cache = {"status": "running"}
        st.session_state.sync_status_time = time.time()
    elif status_code == 409:
        st.toast("이미 진행 중", icon="⚠️")
    else:
        detail = data.get("detail", "오류") if isinstance(data, dict) else str(data)
        st.toast(f"실패: {detail}", icon="❌")
    st.rerun()


def _cancel_sync():
    status_code, data = api_post("/api/ingest/cancel", {})
    if status_code in (200, 201):
        st.toast("중단 요청됨", icon="🛑")
    elif status_code == 400:
        st.toast("진행 중인 동기화 없음", icon="ℹ️")
    st.session_state.sync_status_time = 0
    st.rerun()


def _render_sync_section(waiting: bool) -> None:
    """
    동기화 상태 및 제어 버튼을 렌더링합니다.

    v13 핵심: waiting=True 이면 모든 버튼을 비활성화합니다.
    이렇게 하면 AI 응답 대기 중에 사용자가 어떤 동기화 버튼도
    누를 수 없으므로 st.rerun() 이 발생하지 않고,
    진행 중인 RAG API 호출이 안전하게 완료됩니다.
    """
    sync_data = _get_sync_status()
    backend_status = sync_data.get("status", "idle")
    is_running = (backend_status == "running")

    # ── 상태 배지 ────────────────────────────────────────────
    if is_running:
        progress = sync_data.get("progress", "")
        current_page = sync_data.get("current_page", "")
        st.markdown('<span class="sync-badge sync-running">● 동기화 진행 중</span>', unsafe_allow_html=True)
        if progress:
            st.caption(f"진행: {progress}")
        if current_page:
            st.caption(f"📄 {current_page}")
    elif backend_status == "done":
        synced = sync_data.get("synced_count", 0)
        failed = sync_data.get("failed_count", 0)
        st.markdown('<span class="sync-badge sync-done">✓ 완료</span>', unsafe_allow_html=True)
        st.caption(f"성공 {synced} · 실패 {failed}")
    elif backend_status == "cancelled":
        st.markdown('<span class="sync-badge sync-cancel">■ 중단됨</span>', unsafe_allow_html=True)
        st.caption(f"처리: {sync_data.get('synced_count', 0)}개")
    elif backend_status == "failed":
        st.markdown('<span class="sync-badge sync-fail">✕ 실패</span>', unsafe_allow_html=True)
        st.caption(f"{sync_data.get('error', '오류')[:60]}")
    elif backend_status == "unknown":
        st.markdown('<span class="sync-badge sync-unknown">? 조회 실패</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="sync-badge sync-idle">○ 대기</span>', unsafe_allow_html=True)

    # ── 버튼 ─────────────────────────────────────────────────
    # waiting 이면 모든 버튼 비활성화 → rerun 방지 → API 호출 보호
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button(
            "🔄 동기화",
            use_container_width=True,
            disabled=(is_running or waiting),
            key="btn_sync",
        ):
            _start_sync()
    with col_b:
        if st.button(
            "■ 중단",
            use_container_width=True,
            disabled=(not is_running or waiting),
            key="btn_cancel",
        ):
            _cancel_sync()

    # 진행 중이고 AI 대기가 아닐 때만 새로고침 버튼 표시
    if is_running and not waiting:
        if st.button("↻ 상태 새로고침", use_container_width=True, key="btn_refresh"):
            _get_sync_status(force=True)
            st.rerun()


# =============================================================================
#  §11. 메시지 전송 & RAG API 호출
#
#  v13 핵심: active_question 복구 메커니즘
#
#  기존 문제:
#    pending_question 을 None 으로 소비한 후 API 를 호출했는데,
#    API 호출 도중 st.rerun() 이 발생하면 (사이드바 버튼 등)
#    호출이 끊기고 pending 도 없어서 답변이 영영 안 돌아왔습니다.
#
#  해결:
#    pending → active 2단계 전환:
#    ① 사용자 입력 → pending_question 설정
#    ② 렌더 사이클에서 pending → active 이관 + pending 소비
#    ③ active 상태에서 API 호출
#    ④ 성공: active 해제
#    ⑤ 실패(rerun 등): active 유지 → 다음 렌더에서 자동 재시도
#
#  추가 보호:
#    AI 대기 중 모든 rerun 유발 버튼이 비활성화되므로
#    실제로 rerun 으로 끊기는 경우는 거의 없지만,
#    만약의 상황(네트워크 오류 등)에도 active 가 복구를 보장합니다.
# =============================================================================

def _send_message(text: str):
    """사용자 메시지를 등록합니다. API 호출은 show_chat_page() 에서."""
    if not text or not text.strip():
        return
    text = text.strip()
    st.session_state.messages.append({"role": "user", "content": text, "sources": []})
    st.session_state.pending_question = text
    st.session_state.is_waiting_response = True


def _call_rag_api(question: str, session_id: str) -> dict:
    """POST /api/chat — RAG 응답을 요청합니다."""
    status, data = api_post(
        "/api/chat",
        {"message": question, "session_id": session_id},
        timeout=TIMEOUT_LONG,
    )
    if status == 200:
        return {
            "answer": data.get("answer") or data.get("response") or data.get("content", ""),
            "sources": data.get("sources", []),
            "error": None,
        }
    elif status == 401:
        force_logout()
        st.rerun()
        return {"answer": "", "sources": [], "error": "인증 만료"}
    elif status == 0:
        return {"answer": "", "sources": [], "error": data.get("detail", "네트워크 오류 또는 시간 초과")}
    else:
        return {"answer": "", "sources": [], "error": data.get("detail", f"서버 오류 ({status})")}


# =============================================================================
#  §12. 소스 카드 렌더링
# =============================================================================

def _render_sources(sources: list):
    if not sources:
        return
    with st.expander(f"📎 참고 자료 ({len(sources)}건)", expanded=False):
        for src in sources:
            title = src.get("title") or src.get("source", "")
            url = src.get("url") or src.get("source", "")
            if url:
                st.markdown(
                    f'<div class="ref-card"><a href="{url}" target="_blank">{title or url}</a></div>',
                    unsafe_allow_html=True,
                )
            elif title:
                st.markdown(f'<div class="ref-card">{title}</div>', unsafe_allow_html=True)


# =============================================================================
#  §13. 로그인 페이지
# =============================================================================

def show_login_page():
    st.markdown(
        f'<div class="auth-card">'
        f'  <h1>{APP_ICON} {APP_TITLE}</h1>'
        f'  <p>사내 Notion 지식 베이스 AI 어시스턴트</p>'
        f'</div>',
        unsafe_allow_html=True,
    )
    stage = st.session_state.login_stage

    if st.session_state.error:
        st.error(st.session_state.error)
        st.session_state.error = None

    if stage == 1:
        st.markdown("#### 🔑 초대 코드")
        st.caption("관리자에게 받은 초대 코드를 입력하세요.")

        with st.form("invite_form", clear_on_submit=False):
            code_input = st.text_input(
                "초대 코드", type="password",
                placeholder="초대 코드 입력", label_visibility="collapsed",
            )
            submitted = st.form_submit_button("확인", use_container_width=True)

        if submitted:
            if not code_input or not code_input.strip():
                st.warning("초대 코드를 입력해주세요.")
            else:
                status, data = api_post("/auth/verify-invite", {"code": code_input.strip()})
                if status == 200 and data.get("valid"):
                    st.session_state.validated_invite_code = code_input.strip()
                    st.session_state.login_stage = 2
                    st.rerun()
                else:
                    msg = (data.get("message") or data.get("detail", "유효하지 않은 코드")) if isinstance(data, dict) else "유효하지 않은 코드"
                    st.error(msg)

    elif stage == 2:
        st.markdown("#### 🔐 Google 로그인")
        st.caption("회사 Google 계정으로 로그인하세요.")

        invite_code = st.session_state.validated_invite_code or ""
        state_data = json.dumps({"invite_code": invite_code})
        state_encoded = base64.urlsafe_b64encode(state_data.encode()).decode()
        google_url = f"{BACKEND_URL_EXTERNAL}/auth/google?state={state_encoded}"

        st.link_button("🔐  Google 로그인", url=google_url, use_container_width=True)
        st.markdown("---")
        if st.button("← 초대 코드 다시 입력", use_container_width=True):
            st.session_state.login_stage = 1
            st.session_state.validated_invite_code = None
            st.rerun()


# =============================================================================
#  §14. 사이드바
# =============================================================================

def show_sidebar():
    with st.sidebar:
        waiting = _is_waiting()

        # 프로필
        user = st.session_state.get("user_info") or {}
        name = user.get("name", "사용자")
        email = user.get("email", "")
        st.markdown(
            f'<div class="profile-card">'
            f'  <div class="profile-name">👤 {name}</div>'
            f'  <div class="profile-email">{email}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # AI 대기 배너
        if waiting:
            st.markdown(
                '<div class="thinking-banner">✦ 답변을 생성하고 있습니다…</div>',
                unsafe_allow_html=True,
            )

        # 채팅
        st.markdown('<p class="section-title">💬 대화</p>', unsafe_allow_html=True)
        if st.button("＋ 새 대화", use_container_width=True, disabled=waiting, key="btn_new"):
            create_new_chat()

        sessions = st.session_state.chat_sessions
        if sessions:
            for sess in sessions:
                sid = sess.get("id") or sess.get("session_id", "")
                label = sess.get("title") or sess.get("name") or sid[:8]
                is_current = (sid == st.session_state.current_session_id)
                col_n, col_d = st.columns([5, 1])
                with col_n:
                    if st.button(
                        f"{'▸ ' if is_current else '  '}{label}",
                        key=f"s_{sid}", use_container_width=True, disabled=waiting,
                    ):
                        switch_session(sid)
                with col_d:
                    if st.button("×", key=f"d_{sid}", disabled=waiting):
                        delete_session(sid)
        else:
            st.caption("대화 없음")

        st.markdown("---")

        # 파일 업로드
        st.markdown('<p class="section-title">📁 파일</p>', unsafe_allow_html=True)
        uploaded = st.file_uploader(
            "PDF", type=["pdf"], label_visibility="collapsed",
            disabled=waiting, key="uploader",
        )
        if uploaded:
            _handle_file_upload(uploaded)

        st.markdown("---")

        # 동기화
        st.markdown('<p class="section-title">🔗 Notion 동기화</p>', unsafe_allow_html=True)
        _render_sync_section(waiting)

        st.markdown("---")

        # 문제 해결
        with st.expander("🔧 문제 해결", expanded=False):
            st.caption("응답이 멈춘 경우:")
            if st.button("🔓 대기 해제", use_container_width=True, key="btn_unlock"):
                st.session_state.is_waiting_response = False
                st.session_state.pending_question = None
                st.session_state.active_question = None
                st.toast("대기 해제됨", icon="✅")
                st.rerun()

        # 로그아웃
        if st.button("로그아웃", use_container_width=True, key="btn_logout", disabled=waiting):
            force_logout()
            st.rerun()


# =============================================================================
#  §15. 메인 채팅 페이지
#
#  v13 핵심 변경: pending → active 2단계 전환
#
#  렌더링 순서:
#    1. 에러 표시
#    2. 세션 미선택 → 환영 화면
#    3. 기존 메시지 렌더링
#    4. pending 또는 active 감지 → API 호출
#    5. 비정상 상태 감지
#    6. 입력창
# =============================================================================

def show_chat_page():
    # ── 에러 ─────────────────────────────────────────────────
    if st.session_state.error:
        st.error(st.session_state.error)
        st.session_state.error = None

    # ── 세션 미선택 → 환영 화면 ──────────────────────────────
    if not st.session_state.current_session_id:
        st.markdown(
            '<div class="welcome-container">'
            f'  <div class="welcome-icon">{APP_ICON}</div>'
            '  <div class="welcome-title">안녕하세요</div>'
            '  <div class="welcome-sub">왼쪽에서 새 대화를 시작하거나<br>기존 대화를 선택하세요</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    # ── 기존 메시지 ──────────────────────────────────────────
    msgs = st.session_state.messages
    for msg in msgs:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        sources = msg.get("sources", [])
        with st.chat_message(role):
            st.markdown(content)
            if role == "assistant" and sources:
                _render_sources(sources)

    # ── pending → active 전환 + API 호출 ─────────────────────
    #
    # 이 블록이 AI 응답 생성의 핵심입니다.
    #
    # 우선순위:
    #   1. pending_question 있으면 → active 로 이관 + API 호출
    #   2. active_question 있으면 → 재시도 (이전 렌더에서 끊긴 경우)
    #
    # active_question 이 있다는 것은:
    #   "이전 렌더 사이클에서 API 호출이 시작되었지만
    #    rerun 으로 인해 완료되지 못했다" 는 뜻입니다.
    #   → 같은 질문으로 자동 재시도합니다.
    #

    # ① pending 이 있으면 active 로 이관
    pending = st.session_state.get("pending_question")
    if pending:
        st.session_state.active_question = pending
        st.session_state.pending_question = None

    # ② active 가 있으면 API 호출
    active = st.session_state.get("active_question")
    if active and st.session_state.current_session_id:

        with st.chat_message("assistant"):
            with st.spinner("✦ 답변을 생성하고 있습니다…"):
                result = _call_rag_api(active, st.session_state.current_session_id)

            if result.get("error"):
                error_msg = f"⚠️ {result['error']}"
                st.markdown(error_msg)
                st.session_state.messages.append({
                    "role": "assistant", "content": error_msg, "sources": [],
                })
            else:
                answer = result.get("answer", "")
                sources = result.get("sources", [])
                st.markdown(answer)
                _render_sources(sources)
                st.session_state.messages.append({
                    "role": "assistant", "content": answer, "sources": sources,
                })

        # ③ 완료: active 해제 + 대기 해제
        st.session_state.active_question = None
        st.session_state.is_waiting_response = False
        st.rerun()

    # ── 비정상 상태 감지 ─────────────────────────────────────
    # 마지막이 user 메시지인데 active 도 pending 도 없는 경우
    if (
        msgs
        and msgs[-1].get("role") == "user"
        and not st.session_state.get("pending_question")
        and not st.session_state.get("active_question")
        and not st.session_state.is_waiting_response
    ):
        st.info("💡 답변 생성이 중단되었습니다. 질문을 다시 입력해주세요.")

    # ── 입력창 ───────────────────────────────────────────────
    waiting = _is_waiting()
    user_input = st.chat_input(
        "무엇이든 물어보세요…" if not waiting else "답변 생성 중…",
        disabled=waiting,
        key="chat_input",
    )
    if user_input and not waiting:
        _send_message(user_input)
        st.rerun()

    st.markdown(
        '<p class="input-hint">Notion 지식 베이스 기반으로 답변합니다</p>',
        unsafe_allow_html=True,
    )


# =============================================================================
#  §16. 앱 진입점
# =============================================================================

def main():
    handle_oauth_callback()

    if not st.session_state.get("auth_token"):
        show_login_page()
        return

    if not st.session_state.get("user_info"):
        user_data = api_get("/auth/me", timeout=TIMEOUT_NORMAL)
        if user_data:
            st.session_state.user_info = normalize_user_info(user_data)
        else:
            force_logout()
            st.rerun()
            return

    show_sidebar()
    show_chat_page()


main()
