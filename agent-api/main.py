import os
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="AI Sales Agent API", version="0.1.0")
allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
if not allowed_origins:
    allowed_origins = ["http://localhost:8000", "http://127.0.0.1:8000"]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Authorization"])

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)

@app.get("/health")
def health():
    return {"status":"ok","service":"ai-sales-agent","version":"0.1.0",
            "timestamp":datetime.now(timezone.utc).isoformat(),
            "llm_configured":bool(os.getenv("OPENAI_API_KEY")),
            "data_adapters_configured":False}

@app.post("/agent/chat")
def chat(req: ChatRequest):
    api_key=os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY가 서버에 설정되지 않았습니다.")
    try:
        from openai import OpenAI
        client=OpenAI(api_key=api_key)
        model=os.getenv("OPENAI_MODEL","gpt-4.1-mini")
        response=client.responses.create(model=model, instructions=(
            "당신은 과메기 온라인 판매대행 업무를 지원하는 AI 분석 보조자다. "
            "실제 조회하지 않은 광고·매출·원가 수치를 사실처럼 말하지 않는다. "
            "수치가 없으면 필요한 데이터와 분석 방법을 설명하고 가정은 명확히 표시한다. "
            "광고 예산 변경, 게시, 가격 변경, 주문 변경 등 외부 상태를 바꾸는 행동을 했다고 주장하지 않는다. "
            "모든 외부 변경 작업은 별도의 승인·실행 시스템이 연결되기 전까지 제안만 한다."
        ), input=req.message)
        return {"reply":response.output_text,"model":model}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM 요청 실패: {type(exc).__name__}") from exc

@app.get("/agent/capabilities")
def capabilities():
    return {"chat":True,"read_only_data_connectors":False,"task_approval":False,
            "external_execution":False,
            "message":"초기 골격입니다. 기존 광고·SNS·원가 API 어댑터와 승인 기반 실행 모듈은 다음 단계에서 연결해야 합니다."}
