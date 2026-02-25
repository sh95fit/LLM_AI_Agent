import httpx
from config import get_settings

settings = get_settings()

async def send_webhook(message: str, color: str = "#00C473"):
    """잔디 웹훅으로 메시지 전송"""
    if not settings.JANDI_WEBHOOK_URL:
        return
        
    payload = {
        "body": message,
        "connectColor": color,
        "connectInfo": [{
            "title": "Notion RAG Agent",
            "description": "알림 시스템"
        }]
    }
    
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                settings.JANDI_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            print(f"Jandi webhook failed: {e}")

async def send_answer_to_jandi(question: str, answer: str):
    """질문과 답변을 잔디로 전송"""
    msg = f"**[질문]** {question}\n\n**[답변]**\n{answer}"
    await send_webhook(msg)

async def send_sync_notification(count: int):
    """동기화 완료 알림"""
    msg = f"✅ Notion 동기화 완료: 총 {count}개 페이지 업데이트됨."
    await send_webhook(msg, color="#3498db")