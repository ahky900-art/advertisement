# AI Sales Agent (initial scaffold)

This service is intentionally separate from the existing dashboard.

## Current scope
- `GET /health`: service health
- `POST /agent/chat`: LLM chat
- `GET /agent/capabilities`: explicit capability status
- Existing dashboard APIs are not modified.
- No external write actions are implemented.
- No sales or advertising figures are fabricated.

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-4.1-mini"
export CORS_ORIGINS="http://localhost:8000,https://ahky900-art.github.io"
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Separate Render Web Service
- Root Directory: `agent-api`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set `OPENAI_API_KEY` and `CORS_ORIGINS` as Render environment variables. Never put provider secrets in browser code or GitHub.

## Required next steps before autonomous operation
1. Verify the existing Naver Ads API read endpoints and response schemas.
2. Add read-only adapters for ads, Instagram, products, orders and cost data.
3. Add durable storage for conversations, plans, approvals and audit logs.
4. Implement allow-listed actions with approval tokens, idempotency keys and execution logs.
5. Test read/write boundaries before enabling external actions.

The current chat endpoint is an LLM assistant scaffold, not yet an autonomous sales agent.
