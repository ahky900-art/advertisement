import os, time, base64, json
from datetime import datetime, timezone
import bcrypt, requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="AI Sales Agent API", version="0.2.1")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "https://ahky900-art.github.io,http://localhost:8000").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["Content-Type","Authorization"])
COMMERCE = "https://api.commerce.naver.com/external"
ADS = os.getenv("NAVER_ADS_API_BASE", "https://advertisement-naver-api-ymb4.onrender.com").rstrip("/")
token_cache = {"token": None, "expires": 0}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)

def llm_key():
    return os.getenv("POSTECH_API_KEY") or os.getenv("OPENAI_API_KEY")

def llm_model():
    return os.getenv("POSTECH_MODEL") or os.getenv("OPENAI_MODEL", "")

def llm_client():
    from openai import OpenAI
    key = llm_key()
    if not key:
        raise RuntimeError("POSTECH_API_KEY 또는 OPENAI_API_KEY가 설정되지 않았습니다.")
    base_url = os.getenv("LLM_BASE_URL", "https://genai.postech.ac.kr/agent/openai_compatible/a1/v1").rstrip("/")
    return OpenAI(api_key=key, base_url=base_url)

def llm_configured():
    return bool(llm_key() and llm_model())

def commerce_configured():
    return all(os.getenv(k) for k in ("NAVER_COMMERCE_CLIENT_ID","NAVER_COMMERCE_CLIENT_SECRET","NAVER_COMMERCE_ACCOUNT_ID"))

def commerce_token():
    if not commerce_configured():
        raise RuntimeError("Render에 NAVER_COMMERCE_CLIENT_ID, NAVER_COMMERCE_CLIENT_SECRET, NAVER_COMMERCE_ACCOUNT_ID를 등록해야 합니다.")
    if token_cache["token"] and token_cache["expires"] > time.time()+90:
        return token_cache["token"]
    client_id = os.environ["NAVER_COMMERCE_CLIENT_ID"].strip()
    secret = os.environ["NAVER_COMMERCE_CLIENT_SECRET"].strip()
    account_id = os.environ["NAVER_COMMERCE_ACCOUNT_ID"].strip()
    timestamp = str(int(time.time()*1000))
    signature = base64.b64encode(bcrypt.hashpw(f"{client_id}_{timestamp}".encode(), secret.encode())).decode()
    r = requests.post(COMMERCE+"/v1/oauth2/token", data={
        "client_id":client_id, "timestamp":timestamp, "client_secret_sign":signature,
        "grant_type":"client_credentials", "type":"SELLER", "account_id":account_id
    }, headers={"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"}, timeout=25)
    if not r.ok:
        try:
            body = r.json()
            detail = body.get("message") or body.get("code") or "인증 실패"
        except Exception:
            detail = "인증 실패"
        raise RuntimeError(f"네이버 인증 HTTP {r.status_code}: {detail}")
    data = r.json()
    if not data.get("access_token"):
        raise RuntimeError("인증 응답에 access_token이 없습니다.")
    token_cache.update(token=data["access_token"], expires=time.time()+int(data.get("expires_in",10800)))
    return token_cache["token"]

def first_dict(value):
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else {}
    return value if isinstance(value, dict) else {}

def normalize_product(row):
    origin = first_dict(row.get("originProduct") or row.get("originProductInfo"))
    channel = first_dict(row.get("smartstoreChannelProduct") or row.get("channelProducts") or row.get("channelProduct") or row.get("channelProductInfo"))
    images = first_dict(origin.get("images") or row.get("images"))
    representative = first_dict(images.get("representativeImage") or images.get("representative"))
    price = origin.get("salePrice", row.get("salePrice"))
    stock = origin.get("stockQuantity", row.get("stockQuantity"))
    return {
        "channelProductNo": str(channel.get("channelProductNo") or row.get("channelProductNo") or ""),
        "originProductNo": str(row.get("originProductNo") or origin.get("originProductNo") or ""),
        "name": str(channel.get("channelProductName") or origin.get("name") or row.get("name") or "상품명 미제공"),
        "salePrice": int(price) if isinstance(price, (int, float)) else None,
        "stockQuantity": int(stock) if isinstance(stock, (int, float)) else None,
        "status": str(channel.get("channelProductDisplayStatusType") or channel.get("channelProductStatusType") or origin.get("statusType") or row.get("statusType") or "상태 미제공"),
        "imageUrl": representative.get("url") if isinstance(representative.get("url"), str) and representative["url"].startswith("https://") else None
    }

def get_products(size=20):
    if not commerce_configured():
        return {"configured":False,"count":0,"totalElements":None,"items":[],"message":"커머스 API 인증정보 미설정","updatedAt":datetime.now(timezone.utc).isoformat()}
    token = commerce_token()
    r = requests.post(COMMERCE+"/v1/products/search", json={"page":1,"size":size},
        headers={"Authorization":"Bearer "+token,"Content-Type":"application/json","Accept":"application/json;charset=UTF-8"}, timeout=30)
    if not r.ok:
        try:
            body = r.json()
            detail = body.get("message") or body.get("code") or "상품 조회 실패"
        except Exception:
            detail = "상품 조회 실패"
        raise RuntimeError(f"상품 목록 조회 HTTP {r.status_code}: {detail}")
    data = r.json()
    raw = data.get("contents") or data.get("items") or data.get("products") or data.get("data") or []
    if isinstance(raw, dict):
        raw = raw.get("contents") or raw.get("items") or []
    items = [normalize_product(item) for item in raw if isinstance(item, dict)]
    total = data.get("totalElements") or data.get("totalCount") or data.get("total")
    return {"configured":True,"count":len(items),"totalElements":total if isinstance(total,int) else None,"items":items,"message":"상품 목록 조회 성공","updatedAt":datetime.now(timezone.utc).isoformat()}

def get_ads():
    try:
        health = requests.get(ADS+"/api/health", timeout=12).json()
        campaigns = requests.get(ADS+"/api/naver/campaigns", timeout=20).json()
        adgroups = requests.get(ADS+"/api/naver/adgroups", timeout=20).json()
        stats = requests.get(ADS+"/api/naver/adgroup-stats?days=7", timeout=25).json()
        return {"connected":health.get("status")=="ok","campaignCount":campaigns.get("count",len(campaigns.get("items") or [])),"adgroupCount":adgroups.get("count",len(adgroups.get("items") or [])),"stats":{"since":stats.get("since"),"until":stats.get("until"),"daily":stats.get("daily") or []}}
    except Exception as exc:
        return {"connected":False,"error":type(exc).__name__}

@app.get("/")
def root():
    return {"service":"AI Sales Agent API","docs":"/docs"}

@app.get("/health")
def health():
    return {"status":"ok","service":"ai-sales-agent","version":"0.2.1","timestamp":datetime.now(timezone.utc).isoformat(),"llm_configured":llm_configured(),"commerce_configured":commerce_configured()}

@app.get("/data/products")
def products(size:int=Query(default=20,ge=1,le=50)):
    try:
        return get_products(size)
    except Exception as exc:
        raise HTTPException(status_code=502,detail=str(exc)) from exc

@app.get("/data/overview")
def overview():
    return {"updatedAt":datetime.now(timezone.utc).isoformat(),"products":{"configured":commerce_configured()},"naverAds":get_ads(),"instagram":{"connected":True,"source":"dashboard snapshot file","liveApi":False},"ordersAndSales":{"connected":False}}

@app.post("/agent/chat")
def chat(req:ChatRequest):
    key = llm_key()
    model = llm_model()
    if not key or not model:
        raise HTTPException(status_code=503,detail="POSTECH_API_KEY와 POSTECH_MODEL을 Render에 설정해야 합니다.")
    try:
        context = {"smartStoreProducts":None,"naverAds":get_ads(),"ordersAndSales":"미연결","research":None}
        try:
            context["smartStoreProducts"] = get_products(20)
        except Exception as exc:
            context["smartStoreProducts"] = {"error":str(exc)}
        prompt = "연결 데이터(YouTube API 미사용, 링크 기반 참고만 가능):\\n" + json.dumps(context,ensure_ascii=False,default=str) + "\\n\\n사용자 요청:\\n" + req.message
        response = llm_client().chat.completions.create(
            model=model,
            messages=[
                {"role":"system","content":"과메기 온라인 판매대행 마케팅 분석 보조자. YouTube Data API를 호출하지 않는다. 유튜브 참고가 필요하면 사용자가 제공한 링크 또는 YouTube 검색 결과 링크만 제안하고, 영상 제목·조회수·좋아요·댓글을 확인했다고 주장하지 않는다. 검색하지 않은 웹사이트나 시장 데이터를 검색했다고 주장하지 않는다. 수익성은 판매가에서 원물·손질·야채·포장·배송정산·플랫폼수수료·광고비를 차감한 공헌이익으로 평가. 확인되지 않은 비용은 가정으로 분리. 게시·가격·재고·광고예산·주문 등 외부 변경은 제안만 하며 실행했다고 주장하지 말 것."},
                {"role":"user","content":prompt}
            ]
        )
        answer = response.choices[0].message.content or ""
        return {"reply":answer,"model":model}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502,detail=f"LLM 요청 실패: {type(exc).__name__}") from exc

@app.get("/agent/capabilities/legacy")
def capabilities():
    return {"chat":llm_configured(),"read_only_data_connectors":{"naver_commerce_products":commerce_configured(),"naver_search_ads":True,"instagram_snapshot":True,"orders_and_sales":False},"task_approval":False,"external_execution":False}


# --- Agent workflows: analysis first, approval before every external write ---
import sqlite3
from pathlib import Path
from typing import Optional
from fastapi import Body

DB_PATH = os.getenv("AGENT_DB_PATH", "/tmp/agent-approvals.sqlite3")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS approvals (
      id TEXT PRIMARY KEY, agent_type TEXT NOT NULL, channel TEXT NOT NULL,
      action_type TEXT NOT NULL, payload TEXT NOT NULL, rationale TEXT NOT NULL,
      status TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT
    )""")
    return con

class PricingInput(BaseModel):
    product_id: str
    sale_price: float = Field(gt=0)
    raw_material_cost: float = Field(ge=0)
    labor_cost: float = Field(ge=0, default=0)
    vegetable_cost: float = Field(ge=0, default=0)
    packaging_cost: float = Field(ge=0, default=0)
    shipping_cost: float = Field(ge=0, default=0)
    platform_fee_rate: float = Field(ge=0, le=1, default=0)
    ad_cost_per_order: float = Field(ge=0, default=0)
    target_margin_rate: float = Field(ge=0, le=0.9, default=0.1)
    competitor_price: Optional[float] = Field(default=None, gt=0)
    stock_quantity: Optional[int] = Field(default=None, ge=0)

class SourcingInput(BaseModel):
    product_name: str = Field(min_length=1, max_length=200)
    keyword_metrics: list[dict] = Field(default_factory=list)
    competitor_observations: list[dict] = Field(default_factory=list)
    social_observations: list[dict] = Field(default_factory=list)
    seasonality_observations: list[dict] = Field(default_factory=list)

class CSDraftInput(BaseModel):
    channel: str
    customer_message: str = Field(min_length=1, max_length=5000)
    product_facts: dict = Field(default_factory=dict)
    approved_guides: list[str] = Field(default_factory=list)

class ApprovalInput(BaseModel):
    agent_type: str
    channel: str
    action_type: str
    payload: dict
    rationale: str = Field(min_length=1, max_length=3000)

@app.post("/agent/sourcing/analyze")
def sourcing_analyze(req: SourcingInput):
    # Never claim private competitor sales volume. Only score the observations supplied.
    def avg(values):
        nums = [float(v) for v in values if isinstance(v, (int,float))]
        return sum(nums)/len(nums) if nums else None
    keyword_rows = []
    for row in req.keyword_metrics:
        keyword_rows.append({
            "keyword": str(row.get("keyword","")),
            "trend_score": row.get("trend_score"),
            "search_volume": row.get("search_volume"),
            "source": row.get("source","not_specified"),
            "recommendation": "검토 대상" if row.get("trend_score") is not None else "추세 데이터 필요"
        })
    return {
      "agent":"sourcing","product":req.product_name,"status":"analysis_only",
      "keywords":keyword_rows,
      "competitor_observations":req.competitor_observations,
      "social_observations":req.social_observations,
      "seasonality_observations":req.seasonality_observations,
      "limitations":["경쟁사 실제 주문량·매출은 제공되지 않으면 추정하지 않습니다.","관측값이 없는 항목은 미확인으로 유지합니다."],
      "next_step":"추천 키워드·상품명·광고문구는 승인 요청으로 등록한 뒤 별도 승인 전에는 게시하지 않습니다.",
      "updated_at":datetime.now(timezone.utc).isoformat()
    }

@app.post("/agent/pricing/analyze")
def pricing_analyze(req: PricingInput):
    cost = req.raw_material_cost + req.labor_cost + req.vegetable_cost + req.packaging_cost + req.shipping_cost + req.ad_cost_per_order
    denominator = 1 - req.platform_fee_rate - req.target_margin_rate
    if denominator <= 0:
        raise HTTPException(status_code=400, detail="수수료율과 목표 마진율의 합은 100% 미만이어야 합니다.")
    min_price = cost / (1 - req.platform_fee_rate)
    target_price = cost / denominator
    fee = req.sale_price * req.platform_fee_rate
    contribution = req.sale_price - cost - fee
    margin = contribution / req.sale_price
    return {
      "agent":"pricing","product_id":req.product_id,"status":"analysis_only",
      "current_sale_price":round(req.sale_price),"total_variable_cost":round(cost),
      "platform_fee_estimate":round(fee),"contribution_profit":round(contribution),
      "contribution_margin_rate":round(margin,4),
      "break_even_price":round(min_price),"target_margin_price":round(target_price),
      "below_break_even":req.sale_price < min_price,
      "below_target_margin":req.sale_price < target_price,
      "competitor_price":req.competitor_price,"stock_quantity":req.stock_quantity,
      "recommendation":"가격·구성 조정 검토" if req.sale_price < target_price else "현재 가격 유지 검토",
      "warning":"수수료·원가·배송 부담 주체를 실제 계약 및 정산 데이터로 확인해야 합니다.",
      "updated_at":datetime.now(timezone.utc).isoformat()
    }

@app.post("/agent/cs/draft")
def cs_draft(req: CSDraftInput):
    key = llm_key()
    model = llm_model()
    if not key or not model:
        raise HTTPException(status_code=503, detail="POSTECH_API_KEY와 POSTECH_MODEL을 Render에 설정해야 합니다.")
    try:
        prompt = {"channel":req.channel,"customer_message":req.customer_message,"verified_product_facts":req.product_facts,"approved_guides":req.approved_guides}
        response = llm_client().chat.completions.create(
          model=model,
          messages=[
            {"role":"system","content":"한국 온라인 식품 판매 CS 초안 작성자. 제공된 검증 사실과 승인된 가이드만 사용한다. 사실이 없으면 확인 필요라고 말한다. 환불·보상·식품안전·원산지 분쟁은 확답하지 말고 담당자 확인으로 분류한다. 메시지를 전송하지 말고 답변 초안과 위험 분류만 반환한다."},
            {"role":"user","content":json.dumps(prompt,ensure_ascii=False)}
          ]
        )
        answer = response.choices[0].message.content or ""
        return {"agent":"cs","status":"draft_only","reply_draft":answer,"model":model,"send_allowed":False,"requires_human_review":True}
    except Exception as exc:
        raise HTTPException(status_code=502,detail="CS 초안 생성 실패: "+type(exc).__name__)

@app.post("/agent/approvals")
def create_approval(req: ApprovalInput):
    import uuid
    approval_id = "APR-" + uuid.uuid4().hex[:12].upper()
    created = datetime.now(timezone.utc).isoformat()
    con = db()
    con.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?)", (
      approval_id,req.agent_type,req.channel,req.action_type,
      json.dumps(req.payload,ensure_ascii=False),req.rationale,"pending",created,None
    ))
    con.commit(); con.close()
    return {"approval_id":approval_id,"status":"pending","created_at":created,"execution_performed":False}

@app.get("/agent/approvals")
def list_approvals(status: str = Query(default="pending")):
    con=db()
    rows=con.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC",(status,)).fetchall()
    con.close()
    return {"items":[{**dict(x),"payload":json.loads(x["payload"])} for x in rows]}

@app.post("/agent/approvals/{approval_id}/approve")
def approve_approval(approval_id: str):
    # Approval records authorization only. No channel write is performed by this MVP.
    con=db()
    cur=con.execute("UPDATE approvals SET status='approved',decided_at=? WHERE id=? AND status='pending'",
      (datetime.now(timezone.utc).isoformat(),approval_id))
    con.commit(); con.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404,detail="대기 중인 승인 건을 찾을 수 없습니다.")
    return {"approval_id":approval_id,"status":"approved","execution_performed":False,"message":"승인 기록 완료. 외부 채널 실행 어댑터는 아직 연결되지 않았습니다."}

@app.post("/agent/approvals/{approval_id}/reject")
def reject_approval(approval_id: str):
    con=db()
    cur=con.execute("UPDATE approvals SET status='rejected',decided_at=? WHERE id=? AND status='pending'",
      (datetime.now(timezone.utc).isoformat(),approval_id))
    con.commit(); con.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404,detail="대기 중인 승인 건을 찾을 수 없습니다.")
    return {"approval_id":approval_id,"status":"rejected","execution_performed":False}

def youtube_search_public(query: str, max_results: int = 8):
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="유튜브 검색을 사용하려면 Render 환경변수 YOUTUBE_API_KEY를 먼저 등록해야 합니다.")
    search_response = requests.get("https://www.googleapis.com/youtube/v3/search", params={
        "part":"snippet", "type":"video", "q":query,
        "maxResults":max(1,min(max_results,10)), "order":"relevance",
        "regionCode":"KR", "relevanceLanguage":"ko", "key":api_key
    }, timeout=20)
    search_data = search_response.json()
    if not search_response.ok:
        reason = (search_data.get("error") or {}).get("errors", [{}])[0].get("reason", "YouTube 검색 API 오류")
        raise HTTPException(status_code=502, detail="YouTube 검색 실패: "+reason)
    ids = [x.get("id",{}).get("videoId") for x in search_data.get("items",[]) if x.get("id",{}).get("videoId")]
    if not ids:
        return {"query":query,"items":[],"source":"YouTube Data API v3","searched_at":datetime.now(timezone.utc).isoformat()}
    stats_response = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
        "part":"snippet,statistics,contentDetails", "id":",".join(ids), "key":api_key
    }, timeout=20)
    stats_data = stats_response.json()
    if not stats_response.ok:
        raise HTTPException(status_code=502, detail="YouTube 영상 통계 조회 실패")
    items=[]
    for item in stats_data.get("items",[]):
        sn=item.get("snippet",{}); st=item.get("statistics",{}); vid=item.get("id")
        items.append({"video_id":vid,"title":sn.get("title"),"channel_title":sn.get("channelTitle"),
            "published_at":sn.get("publishedAt"),"description":(sn.get("description") or "")[:500],
            "view_count":int(st["viewCount"]) if "viewCount" in st else None,
            "like_count":int(st["likeCount"]) if "likeCount" in st else None,
            "comment_count":int(st["commentCount"]) if "commentCount" in st else None,
            "duration":item.get("contentDetails",{}).get("duration"),
            "url":"https://www.youtube.com/watch?v="+vid})
    return {"query":query,"items":items,"source":"YouTube Data API v3","searched_at":datetime.now(timezone.utc).isoformat()}

@app.get("/youtube/search")
def youtube_search_endpoint(q: str = Query(min_length=1,max_length=200), max_results: int = Query(default=8,ge=1,le=10)):
    return youtube_search_public(q,max_results)

@app.get("/youtube/video/{video_id}")
def youtube_public_video(video_id: str):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="유효하지 않은 YouTube 영상 ID입니다.")
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Render 환경변수 YOUTUBE_API_KEY가 설정되지 않았습니다.")
    try:
        response = requests.get("https://www.googleapis.com/youtube/v3/videos", params={
            "part":"snippet,statistics,contentDetails",
            "id":video_id,
            "key":api_key
        }, timeout=20)
        data = response.json()
        if not response.ok:
            reason = (data.get("error") or {}).get("errors", [{}])[0].get("reason", "YouTube API 오류")
            raise HTTPException(status_code=502, detail=f"YouTube Data API 오류: {reason}")
        items = data.get("items") or []
        if not items:
            raise HTTPException(status_code=404, detail="영상을 찾을 수 없거나 공개 조회가 제한되어 있습니다.")
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        return {
            "video_id":video_id,
            "url":"https://www.youtube.com/watch?v="+video_id,
            "title":snippet.get("title"),
            "channel_title":snippet.get("channelTitle"),
            "channel_id":snippet.get("channelId"),
            "published_at":snippet.get("publishedAt"),
            "description":snippet.get("description"),
            "tags":snippet.get("tags", []),
            "thumbnail":(snippet.get("thumbnails", {}).get("high") or snippet.get("thumbnails", {}).get("default") or {}).get("url"),
            "view_count":int(stats["viewCount"]) if "viewCount" in stats else None,
            "like_count":int(stats["likeCount"]) if "likeCount" in stats else None,
            "comment_count":int(stats["commentCount"]) if "commentCount" in stats else None,
            "duration":item.get("contentDetails", {}).get("duration"),
            "source":"YouTube Data API v3",
            "updated_at":datetime.now(timezone.utc).isoformat()
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="YouTube 공개 데이터 조회 실패: "+type(exc).__name__)

@app.get("/agent/capabilities")
def capabilities_v2():
    return {
      "chat":bool(os.getenv("OPENAI_API_KEY")),
      "sourcing_analysis":True,"pricing_calculation":True,"cs_draft":bool(os.getenv("OPENAI_API_KEY")),
      "youtube_public_data":False,
      "approval_queue":True,
      "external_execution":False,
      "connected_channels":{"naver_smartstore_products":commerce_configured(),"naver_ads":True,"instagram_live_api":False},
      "limitations":["승인 기능은 승인 이력만 기록하며 외부 변경은 실행하지 않습니다.","승인 DB는 AGENT_DB_PATH 기본값(/tmp)의 임시 저장입니다. 영구 보존에는 PostgreSQL 연결이 필요합니다.","Instagram은 현재 라이브 API가 아닌 기존 대시보드 스냅샷만 확인 가능합니다."]
    }
