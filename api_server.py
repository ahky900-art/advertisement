import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from naver_api import NaverSearchAdClient

app = FastAPI(title="Naver Marketing Dashboard API", version="1.0.0")

allowed_origins = [
    "https://ahky900-art.github.io",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

def get_ad_client():
    required = ["NAVER_SA_API_KEY", "NAVER_SA_SECRET_KEY", "NAVER_SA_CUSTOMER_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Render 환경변수 누락: " + ", ".join(missing))
    return NaverSearchAdClient(
        api_key=os.environ["NAVER_SA_API_KEY"],
        secret_key=os.environ["NAVER_SA_SECRET_KEY"],
        customer_id=os.environ["NAVER_SA_CUSTOMER_ID"],
    )

@app.get("/")
def root():
    return {"service": "Naver Marketing Dashboard API", "docs": "/docs"}

@app.get("/api/health")
def health():
    required = ["NAVER_SA_API_KEY", "NAVER_SA_SECRET_KEY", "NAVER_SA_CUSTOMER_ID"]
    configured = all(os.getenv(name) for name in required)
    return {
        "status": "ok" if configured else "needs_configuration",
        "naver_search_ads_configured": configured,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/api/naver/campaigns")
def campaigns():
    try:
        raw = get_ad_client().get_campaigns()
        # Return only fields needed by the public, read-only dashboard.
        items = [{
            "id": x.get("nccCampaignId"),
            "name": x.get("name"),
            "type": x.get("campaignTp"),
            "status": x.get("status"),
        } for x in raw]
        return {"count": len(items), "items": items}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

@app.get("/api/naver/adgroups")
def adgroups():
    try:
        raw = get_ad_client().get_adgroups()
        items = [{
            "id": x.get("nccAdgroupId"),
            "campaign_id": x.get("nccCampaignId"),
            "name": x.get("name"),
            "status": x.get("status"),
        } for x in raw]
        return {"count": len(items), "items": items}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
