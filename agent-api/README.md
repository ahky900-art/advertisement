# AI Sales Agent API

독립형 AI 판매대행 에이전트의 백엔드입니다. 기존 대시보드 API 서버와 분리되어 있습니다.

## Endpoints
- GET /health: 서비스 및 연동 구성 상태
- GET /data/products?size=20: 네이버 스마트스토어 상품 목록(읽기 전용)
- GET /data/overview: 연결된 데이터 소스 상태
- POST /agent/chat: 현재 조회 가능한 상품/광고 데이터를 문맥으로 제공하는 AI 채팅
- GET /agent/capabilities: 기능 및 실행 권한 상태

## Render deployment
- Repository: https://github.com/ahky900-art/advertisement
- Branch: feature/smartstore-products-agent-integration (merge 후 main으로 변경)
- Root Directory: agent-api
- Build Command: pip install -r requirements.txt
- Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
- Region: Singapore (한국 사용자와 가까운 리전)

## Render environment variables
Set secrets only in Render dashboard, never in HTML or GitHub:
- OPENAI_API_KEY: OpenAI API key
- OPENAI_MODEL: gpt-4.1-mini
- CORS_ORIGINS: https://ahky900-art.github.io
- NAVER_ADS_API_BASE: https://advertisement-naver-api-ymb4.onrender.com
- NAVER_COMMERCE_CLIENT_ID: Commerce API application ID
- NAVER_COMMERCE_CLIENT_SECRET: Commerce API application secret (bcrypt salt string)
- NAVER_COMMERCE_ACCOUNT_ID: seller account_id required for SELLER token

Commerce API authentication uses OAuth2 client credentials with type=SELLER. The signature is bcrypt(client_id + "_" + timestamp, client_secret), then Base64 encoded. Product lookup uses POST /external/v1/products/search. All calls are read-only.

## Security and current boundaries
- API credentials are server-side environment variables only.
- Product and advertising integrations are read-only.
- No product edits, price/stock changes, order changes, ad budget changes, posting, or other external write operations are implemented.
- Orders and sales data are not connected yet.
- The in-memory token cache is per service process; it is not a persistent job/audit store.
