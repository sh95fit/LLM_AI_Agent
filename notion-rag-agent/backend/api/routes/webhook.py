from fastapi import APIRouter, Request

router = APIRouter()

@router.post("/webhook/jandi")
async def jandi_webhook(request: Request):
    """잔디 Outgoing Webhook 수신 (Optional)"""
    data = await request.json()
    print(f"Received from Jandi: {data}")
    # 여기에 챗봇 로직을 연결하여 양방향 대화 구현 가능
    return {"status": "ok"}
