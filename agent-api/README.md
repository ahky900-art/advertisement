# AI Sales Agent API

독립형 AI 판매대행 에이전트 백엔드입니다. 기존 대시보드 및 네이버 광고 API 서버와 분리되어 있습니다.

## Endpoints
- GET /health
- GET /data/products?size=20 — 네이버 스마트스토어 상품 목록(읽기 전용)
- GET /data/overview — 데이터 소스 연결 상태
- POST /agent/chat — 상품·광고 데이터 문맥을 사용하는 AI 채팅
- GET /agent/capabilities

## Render service configuration
- Repository: https://github.com/ahky900-art/advertisement
- Branch: feature/smartstore-products-agent-integration (merge 후 main)
- Root Directory: (blank)
- Build Command: pip install -r agent-api/requirements.txt
- Start Command: uvicorn main:app --app-dir agent-api --host 0.0.0.0 --port $PORT
- Region: Singapore
- Plan: Free

## Environment variables (Render dashboard only)
- OPENAI_API_KEY: OpenAI API key
- OPENAI_MODEL: gpt-4.1-mini
- CORS_ORIGINS: https://ahky900-art.github.io
- NAVER_ADS_API_BASE: https://advertisement-naver-api-ymb4.onrender.com
- NAVER_COMMERCE_CLIENT_ID: Commerce API application ID
- NAVER_COMMERCE_CLIENT_SECRET: Commerce API application secret (bcrypt salt string)
- NAVER_COMMERCE_ACCOUNT_ID: seller account ID/UID used with type=SELLER

Commerce API authentication follows Naver's OAuth2 client-credentials flow: bcrypt(client_id + "_" + timestamp, client_secret), Base64 encoded, with type=SELLER and account_id. Product list lookup is POST /external/v1/products/search. All integration calls are read-only.

## Security and limitations
- Provider credentials must never be committed to GitHub or placed in browser code.
- No product, price, inventory, order, ad budget, or social-post write operations are implemented.
- Orders and sales are not connected yet.
- No durable approval queue or audit log exists yet.
