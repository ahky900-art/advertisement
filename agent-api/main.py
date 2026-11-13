import os, time, base64, json
from datetime import datetime, timezone
from typing import Any
import bcrypt, requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="AI Sales Agent API", version="0.2.0")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "https://ahky900-art.github.io,http://localhost:8000").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["Content-Type","Authorization"])
COMMERCE = "https://api.commerce.naver.com/external"
ADS = os.getenv("NAVER_ADS_API_BASE", "https://advertisement-naver-api-ymb4.onrender.com").rstrip("/")
token_cache = {"token": None, "expires": 0}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)

def commerce_configured():
    return all(os.getenv(k) for k in ("NAVER_COMMERCE_CLIENT_ID","NAVER_COMMERCE_CLIENT_SECRET","NAVER_COMMERCE_ACCOUNT_ID"))

def commerce_token():
    if not commerce_configured():
        raise RuntimeError("Render에 NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET, NAVER_COMMERCE_ACCOUNT_ID를 등록해야 합니다.")
    if token_cache["token"] and token_cache["expires"] > time.time()+90:
        return token_cache["token"]
    client_id=os.environ["NAVER_COMMERCE_CLIENT_ID"].strip()
    secret=os.environ["NAVER_COMMERCE_CLIENT_SECRET"].strip()
    account_id=os.environ["NAVER_COMMERCE_ACCOUNT_ID"].strip()
    timestamp=str(int(time.time()*1000))
    signature=base64.b64encode(bcrypt.hashpw(f"{client_id}_{timestamp}".encode(), secret.encode())).decode()
    r=requests.post(COMMERCE+"/v1/oauth2/token", data={"client_id":client_id,"timestamp":timestamp,"client_secret_sign":signature,"grant_type":"client_credentials","type":"SELLER","account_id":account_id}, headers={"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"}, timeout=25)
    if not r.ok:
        try: detail=r.json().get("message") or r.json().get("code") or "인증 실패"
        except Exception: detail="인증 실패"
        raise RuntimeError(f"네이버 인증 HTTP {r.status_code}: {detail}")
    data=r.json()
    if not data.get("access_token"): raise RuntimeError("인증 응답에 access_token이 없습니다.")
    token_cache.update(token=data["access_token"], expires=time.time()+int(data.get("expires_in",10800)))
    return token_cache["token"]

def first_dict(x):
    if isinstance(x,list): return x[0] if x and isinstance(x[0],dict) else {}
    return x if isinstance(x,dict) else {}

def normalize_product(row):
    origin=first_dict(row.get("originProduct") or row.get("originProductInfo"))
    channel=first_dict(row.get("smartstoreChannelProduct") or row.get("channelProduct") or row.get("channelProductInfo"))
    images=first_dict(origin.get("images") or row.get("images"))
    rep=first_dict(images.get("representativeImage") or images.get("representative"))
    price=origin.get("salePrice",row.get("salePrice"))
    stock=origin.get("stockQuantity",row.get("stockQuantity"))
    return {
      "channelProductNo":str(channel.get("channelProductNo") or row.get("channelProductNo") or ""),
      "originProductNo":str(row.get("originProductNo") or origin.get("originProductNo") or ""),
      "name":str(channel.get("channelProductName") or origin.get("name") or row.get("name") or "상품명 미제공"),
      "salePrice":int(price) if isinstance(price,(int,float)) else None,
      "stockQuantity":int(stock) if isinstance(stock,(int,float)) else None,
      "status":str(channel.get("channelProductDisplayStatusType") or channel.get("channelProductStatusType") or origin.get("statusType") or row.get("statusType") or "상태 미제공"),
      "imageUrl":rep.get("url") if isinstance(rep.get("url"),str) and rep["url"].startswith("https://") else None
    }

def get_products(size=20):
    if not commerce_configured():
        return {"configured":False,"count":0,"totalElements":None,"items":[],"message":"커머스 API 인증정보 미설정","updatedAt":datetime.now(timezone.utc).isoformat()}
    token=commerce_token()
    r=requests.post(COMMERCE+"/v1/products/search",json={"page":1,"size":size},headers={"Authorization":"Bearer "+token,"Content-Type":"application/json","Accept":"application/json;charset=UTF-8"},timeout=30)
    if not r.ok:
        try: detail=r.json().get("message") or r.json().get("code") or "상품 조회 실패"
        except Exception: detail="상품 조회 실패"
        raise RuntimeError(f"상품 목록 조회 HTTP {r.status_code}: {detail}")
    data=r.json()
    raw=data.get("contents") or data.get("items") or data.get("products") or data.get("data") or []
    if isinstance(raw,dict): raw=raw.get("contents") or raw.get("items") or []
    items=[normalize_product(x) for x in raw if isinstance(x,dict)]
    total=data.get("totalElements") or data.get("totalCount") or data.get("total")
    return {"configured":True,"count":len(items),"totalElements":total if isinstance(total,int) else None,"items":items,"message":"상품 목록 조회 성공","updatedAt":datetime.now(timezone.utc).isoformat()}

def get_ads():
    try:
        h=requests.get(ADS+"/api/health",timeout=12).json()
        c=requests.get(ADS+"/api/naver/campaigns",timeout=20).json()
        g=requests.get(ADS+"/api/naver/adgroups",timeout=20).json()
        st=requests.get(ADS+"/api/naver/adgroup-stats?days=7",timeout=25).json()
        return {"connected":h.get("status")=="ok","campaignCount":c.get("count",len(c.get("items") or [])),"adgroupCount":g.get("count",len(g.get("items") or [])),"stats":{"since":st.get("since"),"until":st.get("until"),"daily":st.get("daily") or []}}
    except Exception as e: return {"connected":False,"error":type(e).__name__}

@app.get("/")
def root(): return {"service":"AI Sales Agent API","docs":"/docs"}

@app.get("/health")
def health():
    return {"status":"ok","service":"ai-sales-agent","version":"0.2.0","timestamp":datetime.now(timezone.utc).isoformat(),"llm_configured":bool(os.getenv("OPENAI_API_KEY")),"commerce_configured":commerce_configured()}

@app.get("/data/products")
def products(size:int=Query(default=20,ge=1,le=50)):
    try: return get_products(size)
    except Exception as e: raise HTTPException(status_code=502,detail=str(e))

@app.get("/data/overview")
def overview():
    return {"updatedAt":datetime.now(timezone.utc).isoformat(),"products":{"configured":commerce_configured()},"naverAds":get_ads(),"instagram":{"connected":True,"source":"dashboard snapshot file","liveApi":False},"ordersAndSales":{"connected":False}}

@app.post("/agent/chat")
def chat(req:ChatRequest):
    key=os.getenv("OPENAI_API_KEY")
    if not key: raise HTTPException(status_code=503,detail="OPENAI_API_KEY가 Render에 설정되지 않았습니다.")
    try:
        from openai import OpenAI
        model=os.getenv("OPENAI_MODEL","gpt-4.1-mini")
        context={"smartStoreProducts":None,"naverAds":get_ads(),"ordersAndSales":"미연결"}
        try: context["smartStoreProducts"]=get_products(20)
        except Exception as e: context["smartStoreProducts"]={"error":str(e)}
        response=OpenAI(api_key=key).responses.create(model=model,instructions=("과메기 온라인 판매대행 분석 보조자. 제공된 실제 조회 데이터만 사실로 사용하고 조회 실패·미연결은 수치 추정하지 말 것. 수익성은 판매가에서 원물·손질·야채·포장·배송정산·플랫폼수수료·광고비를 차감한 공헌이익으로 평가. 확인되지 않은 비용은 가정으로 분리. 게시·가격·재고·광고예산·주문 등 외부 변경은 제안만 하며 실행했다고 주장하지 말 것."),input="연결 데이터:
"+json.dumps(context,ensure_ascii=False,default=str)+"

사용자 요청:
"+req.message)
        return {"reply":response.output_text,"model":model}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=502,detail=f"LLM 요청 실패: {type(e).__name__}")

@app.get("/agent/capabilities")
def capabilities():
    return {"chat":bool(os.getenv("OPENAI_API_KEY")),"read_only_data_connectors":{"naver_commerce_products":commerce_configured(),"naver_search_ads":True,"instagram_snapshot":True,"orders_and_sales":False},"task_approval":False,"external_execution":False}
