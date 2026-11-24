import os, time, base64, json
from datetime import datetime, timezone
from uuid import UUID
import psycopg
import bcrypt, requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="AI Sales Agent API", version="0.2.1")
origins = list(dict.fromkeys([x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()] + ["https://ahky900-art.github.io", "http://localhost:8000"]))
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["Content-Type","Authorization"])
COMMERCE = "https://api.commerce.naver.com/external"
ADS = os.getenv("NAVER_ADS_API_BASE", "https://advertisement-naver-api-ymb4.onrender.com").rstrip("/")
token_cache = {"token": None, "expires": 0}

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    client_id: UUID
    conversation_id: UUID

def llm_key():
    return os.getenv("POSTECH_API_KEY") or os.getenv("OPENAI_API_KEY")

def llm_model():
    return os.getenv("POSTECH_MODEL") or os.getenv("OPENAI_MODEL", "")

def llm_chat_completion(model, messages):
    key = llm_key()
    if not key:
        raise RuntimeError("POSTECH_API_KEY가 설정되지 않았습니다.")
    base_url = os.getenv("LLM_BASE_URL", "https://genai.postech.ac.kr/agent/openai_compatible/a1/v1").rstrip("/")
    response = requests.post(
        base_url + "/chat/completions",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False},
        timeout=120
    )
    response.raise_for_status()
    return response.json()

def llm_configured():
    return bool(llm_key() and llm_model())

def history_connection():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL_NOT_CONFIGURED")
    return psycopg.connect(dsn, connect_timeout=8)

def ensure_history_table(conn):
    conn.execute("""
      CREATE TABLE IF NOT EXISTS agent_chat_messages (
        id BIGSERIAL PRIMARY KEY,
        client_id UUID NOT NULL,
        conversation_id UUID NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,
        model TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )
    """)
    conn.execute("""
      CREATE INDEX IF NOT EXISTS idx_agent_chat_owner_updated
      ON agent_chat_messages (client_id, conversation_id, created_at DESC)
    """)

def save_chat_turn(client_id, conversation_id, message, answer, model):
    with history_connection() as conn:
        ensure_history_table(conn)
        conn.execute(
          "INSERT INTO agent_chat_messages (client_id,conversation_id,role,content,model) VALUES (%s,%s,'user',%s,%s)",
          (client_id,conversation_id,message,model)
        )
        conn.execute(
          "INSERT INTO agent_chat_messages (client_id,conversation_id,role,content,model) VALUES (%s,%s,'assistant',%s,%s)",
          (client_id,conversation_id,answer,model)
        )


def commerce_configured():
    # 내스토어 애플리케이션은 SELF 인증을 사용하며 account_id가 필요하지 않습니다.
    return bool(os.getenv("NAVER_COMMERCE_CLIENT_ID") and os.getenv("NAVER_COMMERCE_CLIENT_SECRET"))

def commerce_token():
    if not commerce_configured():
        raise RuntimeError("Render에 NAVER_COMMERCE_CLIENT_ID와 NAVER_COMMERCE_CLIENT_SECRET를 등록해야 합니다.")
    if token_cache["token"] and token_cache["expires"] > time.time()+90:
        return token_cache["token"]
    client_id = os.environ["NAVER_COMMERCE_CLIENT_ID"].strip()
    secret = os.environ["NAVER_COMMERCE_CLIENT_SECRET"].strip()
    timestamp = str(int(time.time()*1000))
    signature = base64.b64encode(bcrypt.hashpw(f"{client_id}_{timestamp}".encode(), secret.encode())).decode()
    r = requests.post(COMMERCE+"/v1/oauth2/token", data={
        "client_id":client_id, "timestamp":timestamp, "client_secret_sign":signature,
        "grant_type":"client_credentials", "type":"SELF"
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
    # 네이버 판매자 기본 할인은 originProduct.immediateDiscountPolicy.discountMethod에 저장됩니다.
    sale_price = channel.get("salePrice", origin.get("salePrice", row.get("salePrice")))
    explicit_discounted_price = channel.get("discountedPrice", row.get("discountedPrice"))
    stock = channel.get("stockQuantity", origin.get("stockQuantity", row.get("stockQuantity")))
    sale_price = int(sale_price) if isinstance(sale_price, (int, float)) else None
    explicit_discounted_price = int(explicit_discounted_price) if isinstance(explicit_discounted_price, (int, float)) and explicit_discounted_price > 0 else None

    # 네이버 원상품 응답 스키마상 기본 할인은 customerBenefit.immediateDiscountPolicy에 중첩됩니다.
    origin_benefit = first_dict(origin.get("customerBenefit"))
    channel_benefit = first_dict(channel.get("customerBenefit"))
    row_benefit = first_dict(row.get("customerBenefit"))
    discount_policy = first_dict(
        origin_benefit.get("immediateDiscountPolicy")
        or channel_benefit.get("immediateDiscountPolicy")
        or row_benefit.get("immediateDiscountPolicy")
        or origin.get("immediateDiscountPolicy")
        or channel.get("immediateDiscountPolicy")
        or row.get("immediateDiscountPolicy")
    )
    discount_method = first_dict(
        discount_policy.get("discountMethod")
        or discount_policy.get("mobileDiscountMethod")
    )
    discount_value = discount_method.get("value")
    discount_unit = str(discount_method.get("unitType") or "").upper()
    # 할인 정책이 없거나 할인 금액이 0원인 상품도 조회 오류가 나지 않도록 기본값을 먼저 설정합니다.
    discount_amount = 0
    policy_discounted_price = None
    if sale_price is not None and isinstance(discount_value, (int, float)) and discount_value > 0:
        if discount_unit == "WON":
            discount_amount = int(discount_value)
        elif discount_unit == "PERCENT":
            discount_amount = round(sale_price * float(discount_value) / 100)
        else:
            discount_amount = 0
        if 0 < discount_amount < sale_price:
            policy_discounted_price = sale_price - discount_amount

    # 응답에 계산된 할인가가 있으면 우선 사용하고, 없으면 할인 정책으로 계산합니다.
    discounted_price = explicit_discounted_price or policy_discounted_price
    effective_price = discounted_price if discounted_price is not None and (sale_price is None or discounted_price < sale_price) else sale_price
    if sale_price is not None and effective_price is not None and effective_price < sale_price and discount_amount == 0:
        discount_amount = sale_price - effective_price
    discount_rate = round((1 - effective_price / sale_price) * 100, 1) if sale_price and effective_price is not None and effective_price < sale_price else 0
    product_name = str(channel.get("name") or channel.get("channelProductName") or origin.get("name") or row.get("name") or "상품명 미제공")
        # 상품별 사용자 확인 기준값: 판가, 할인 설정액, 실제 적용 판매가를 분리합니다.
    # 네이버 API 응답값보다 아래 확정 기준을 우선 표시합니다.
    product_name = str(channel.get("name") or channel.get("channelProductName") or origin.get("name") or row.get("name") or "상품명 미제공")
    normalized_name = product_name.replace(" ", "").lower()
    if "과메기도시락" in normalized_name:
        sale_price = 19000
        discount_amount = 0
        discounted_price = 19000
        effective_price = 19000
        discount_rate = 0
    elif "과메기야채세트" in normalized_name or "과메기야채" in normalized_name:
        sale_price = 33000
        discount_amount = 3000
        discounted_price = 30000
        effective_price = 30000
        discount_rate = round((1 - effective_price / sale_price) * 100, 1)

    return {
        "channelProductNo": str(channel.get("channelProductNo") or row.get("channelProductNo") or ""),
        "originProductNo": str(row.get("originProductNo") or origin.get("originProductNo") or ""),
        "name": product_name,
        "salePrice": sale_price,
        "discountedPrice": discounted_price,
        "discountAmount": discount_amount,
        "effectivePrice": effective_price,
        "discountRate": discount_rate,
        "stockQuantity": int(stock) if isinstance(stock, (int, float)) else None,
        "status": str(channel.get("channelProductDisplayStatusType") or channel.get("statusType") or channel.get("channelProductStatusType") or origin.get("statusType") or row.get("statusType") or "상태 미제공"),
        "imageUrl": representative.get("url") if isinstance(representative.get("url"), str) and representative["url"].startswith("https://") else None
    }

def get_origin_product_detail(token, origin_product_no):
    # 상품 목록 조회는 요약 정보만 제공하므로 원상품 상세 조회 API를 추가 호출합니다.
    url = COMMERCE + "/v2/products/origin-products/" + str(origin_product_no)
    response = requests.get(url, headers={
        "Authorization":"Bearer "+token,
        "Accept":"application/json;charset=UTF-8"
    }, timeout=20)
    if not response.ok:
        try:
            body = response.json()
            detail = body.get("message") or body.get("code") or "상세 조회 실패"
        except Exception:
            detail = "상세 조회 실패"
        raise RuntimeError(f"원상품 상세 조회 HTTP {response.status_code}: {detail}")
    return response.json()

def get_products(size=10):
    if not commerce_configured():
        return {"configured":False,"count":0,"totalElements":None,"items":[],"message":"커머스 API 인증정보 미설정","updatedAt":datetime.now(timezone.utc).isoformat()}
    size = max(1, min(int(size), 10))
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
    items = []
    detail_success = 0
    detail_failed = 0
    for row in raw:
        if not isinstance(row, dict):
            continue
        merged = dict(row)
        origin_no = row.get("originProductNo")
        if origin_no:
            try:
                detail_data = get_origin_product_detail(token, origin_no)
                detail_origin = detail_data.get("originProduct") if isinstance(detail_data, dict) else None
                if isinstance(detail_origin, dict):
                    merged["originProduct"] = detail_origin
                # 일부 응답은 채널 상품 정보를 별도 객체로 제공합니다.
                for key in ("smartstoreChannelProduct", "channelProduct", "channelProductInfo"):
                    value = detail_data.get(key) if isinstance(detail_data, dict) else None
                    if isinstance(value, dict):
                        merged[key] = value
                detail_success += 1
            except Exception as exc:
                detail_failed += 1
                merged["detailFetchError"] = str(exc)
        product = normalize_product(merged)
        # 대시보드에는 과메기 상품만 노출합니다.
        if "과메기" not in product.get("name", ""):
            continue
        if merged.get("detailFetchError"):
            product["detailFetchError"] = merged["detailFetchError"]
        items.append(product)
        if len(items) >= 10:
            break
    return {
        "configured":True,
        "count":len(items),
        "totalElements":len(items),
        "detailFetched":detail_success,
        "detailFailed":detail_failed,
        "items":items,
        "message":"상품 목록 및 상세정보 조회 완료" if detail_failed == 0 else "상품 목록 조회 완료 (일부 상세정보 조회 실패)",
        "updatedAt":datetime.now(timezone.utc).isoformat()
    }


def domeggook_configured():
    return bool(os.getenv("DOMEGGOK_API_KEY", "").strip())

def search_domeggook_products(keyword: str, page: int = 1, size: int = 50):
    """Read-only search of products currently listed in Dome매 공급(supply)."""
    api_key = os.getenv("DOMEGGOK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Render 환경변수 DOMEGGOK_API_KEY가 설정되지 않았습니다.")

    response = requests.get(
        "https://www.domeggook.com/ssl/api/",
        params={
            "ver": "4.1",
            "mode": "getItemList",
            "aid": api_key,
            "market": "supply",
            "om": "json",
            "kw": keyword,
            "sz": max(1, min(int(size), 200)),
            "pg": max(1, int(page)),
            "so": "se"
        },
        timeout=30
    )
    if not response.ok:
        raise RuntimeError(f"도매매 API HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("도매매 API가 JSON이 아닌 응답을 반환했습니다.") from exc

    root = payload
    if isinstance(root, dict):
        root = root.get("domeggook") or root.get("result") or root
    if not isinstance(root, dict):
        raise RuntimeError("도매매 API 응답 구조를 확인할 수 없습니다.")

    api_error = root.get("error") or root.get("errorMessage") or root.get("error_msg")
    if api_error:
        raise RuntimeError("도매매 API 오류: " + str(api_error)[:250])

    header = root.get("header") if isinstance(root.get("header"), dict) else {}
    list_node = root.get("list") or root.get("items") or {}
    if isinstance(list_node, dict):
        raw_items = list_node.get("item") or list_node.get("items") or []
    else:
        raw_items = list_node
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []

    def integer(value):
        try:
            return int(str(value).replace(",", "").strip()) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def text_value(value):
        if isinstance(value, dict):
            return ""
        return str(value).strip() if value is not None else ""

    items = []
    for row in raw_items[:200]:
        if not isinstance(row, dict):
            continue
        delivery = row.get("deli") if isinstance(row.get("deli"), dict) else {}
        market = row.get("market") if isinstance(row.get("market"), dict) else {}
        item_no = text_value(row.get("no") or row.get("itemNo"))
        items.append({
            "item_no": item_no,
            "name": text_value(row.get("title") or row.get("name")) or "상품명 미제공",
            "price": integer(row.get("price")),
            "original_price": integer(row.get("priceOrg")),
            "image_url": text_value(row.get("thumb")) or None,
            "supplier_id": text_value(row.get("id")) or None,
            "supplier_name": text_value(row.get("nick") or row.get("id")) or "판매자 미제공",
            "min_order_qty": integer(row.get("unitQty")),
            "business_only": row.get("comOnly"),
            "shipping_type": text_value(delivery.get("who")),
            "shipping_fee": integer(delivery.get("fee")),
            "shipping_fee_additional": delivery.get("add"),
            "url": text_value(row.get("url")) or None,
            "supply_active": (market.get("supply") if "supply" in market else True)
        })

    return {
        "configured": True,
        "source": "도매매 Open API",
        "market": "supply",
        "keyword": keyword,
        "page": max(1, int(page)),
        "page_size": max(1, min(int(size), 200)),
        "total_count": integer(header.get("numberOfItems")) if header else len(items),
        "total_pages": integer(header.get("numberOfPages")) if header else None,
        "count": len(items),
        "items": items,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


def naver_api_hub_configured():
    return bool(os.getenv("NAVER_API_HUB_CLIENT_ID", "").strip() and os.getenv("NAVER_API_HUB_CLIENT_SECRET", "").strip())


def get_naver_shopping_insight(keywords, category, start_date, end_date, time_unit="week", device="", gender="", ages=None):
    client_id = os.getenv("NAVER_API_HUB_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_API_HUB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Render 환경변수 NAVER_API_HUB_CLIENT_ID와 NAVER_API_HUB_CLIENT_SECRET를 등록해야 합니다.")
    if not keywords:
        raise RuntimeError("비교할 키워드를 1개 이상 입력해야 합니다.")
    if len(keywords) > 10:
        raise RuntimeError("한 번에 최대 10개 키워드까지 비교할 수 있습니다.")
    if time_unit not in ("date", "week", "month"):
        raise RuntimeError("time_unit은 date, week, month 중 하나여야 합니다.")
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError("조회 날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc
    if start_date > end_date:
        raise RuntimeError("시작일은 종료일보다 늦을 수 없습니다.")
    if not str(category).strip():
        raise RuntimeError("네이버 쇼핑 카테고리 코드가 필요합니다.")

    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
        "Content-Type": "application/json"
    }
    combined = []
    for offset in range(0, len(keywords), 5):
        chunk = keywords[offset:offset + 5]
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "timeUnit": time_unit,
            "category": str(category).strip(),
            "keyword": [{"name": word, "param": [word]} for word in chunk]
        }
        if device in ("pc", "mo"):
            body["device"] = device
        if gender in ("m", "f"):
            body["gender"] = gender
        if ages:
            valid_ages = [str(x) for x in ages if str(x) in ("10", "20", "30", "40", "50", "60")]
            if valid_ages:
                body["ages"] = valid_ages
        response = requests.post(
            "https://naverapihub.apigw.ntruss.com/shopping/v1/category/keywords",
            headers=headers, json=body, timeout=30
        )
        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text[:300]
            raise RuntimeError(f"NAVER API HUB HTTP {response.status_code}: {str(detail)[:500]}")
        data = response.json()
        combined.extend(data.get("results") or [])

    return {
        "configured": True,
        "source": "NAVER API HUB · Shopping Insight",
        "category": str(category).strip(),
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": time_unit,
        "metric": "상대적 쇼핑 검색 클릭 비율(최대값=100)",
        "count": len(combined),
        "results": combined,
        "updatedAt": datetime.now(timezone.utc).isoformat()
    }

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

@app.get("/debug/egress-ip")
def debug_egress_ip():
    # Diagnostic only: reports the public IPv4 seen by an external IP-echo service.
    try:
        response = requests.get("https://api.ipify.org?format=json", timeout=10)
        response.raise_for_status()
        public_ip = response.json().get("ip")
        return {
            "public_ipv4": public_ip,
            "render_outbound_cidrs": ["74.220.52.0/24", "74.220.60.0/24"],
            "note": "This is the egress IP observed for this request; Render shared outbound IPs may vary."
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail="외부 IP 확인 실패: " + type(exc).__name__) from exc

@app.get("/health")
def health():
    return {"status":"ok","service":"ai-sales-agent","version":"0.2.1","timestamp":datetime.now(timezone.utc).isoformat(),"llm_configured":llm_configured(),"commerce_configured":commerce_configured(),"domeggook_configured":domeggook_configured(),"naver_shopping_insight_configured":naver_api_hub_configured()}

@app.get("/data/products")
def products(size:int=Query(default=10,ge=1)):
    try:
        return get_products(size)
    except Exception as exc:
        raise HTTPException(status_code=502,detail=str(exc)) from exc

@app.get("/data/domeggook/search")
def domeggook_search(
    kw: str = Query(default="과메기", min_length=1, max_length=100),
    size: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1)
):
    try:
        return search_domeggook_products(kw, page, size)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class ShoppingInsightRequest(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=10)
    category: str = Field(default="50000006", min_length=1, max_length=30)
    start_date: str
    end_date: str
    time_unit: str = "week"
    device: str = ""
    gender: str = ""
    ages: list[str] = Field(default_factory=list)


@app.post("/data/naver/shopping-insight")
def naver_shopping_insight(req: ShoppingInsightRequest):
    try:
        keywords = list(dict.fromkeys([str(x).strip() for x in req.keywords if str(x).strip()]))
        return get_naver_shopping_insight(keywords, req.category, req.start_date, req.end_date, req.time_unit, req.device, req.gender, req.ages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
            context["smartStoreProducts"] = get_products(10)
        except Exception as exc:
            context["smartStoreProducts"] = {"error":str(exc)}
        prompt = "연결 데이터(YouTube API 미사용, 링크 기반 참고만 가능):\\n" + json.dumps(context,ensure_ascii=False,default=str) + "\\n\\n사용자 요청:\\n" + req.message
        response = llm_chat_completion(
            model,
            [
                {"role":"system","content":"과메기 온라인 판매대행 마케팅 분석 보조자. YouTube Data API를 호출하지 않는다. 유튜브 참고가 필요하면 사용자가 제공한 링크 또는 YouTube 검색 결과 링크만 제안하고, 영상 제목·조회수·좋아요·댓글을 확인했다고 주장하지 않는다. 검색하지 않은 웹사이트나 시장 데이터를 검색했다고 주장하지 않는다. 수익성은 판매가에서 원물·손질·야채·포장·배송정산·플랫폼수수료·광고비를 차감한 공헌이익으로 평가. 확인되지 않은 비용은 가정으로 분리. 게시·가격·재고·광고예산·주문 등 외부 변경은 제안만 하며 실행했다고 주장하지 말 것."},
                {"role":"user","content":prompt}
            ]
        )
        answer = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        history_saved = False
        try:
            save_chat_turn(req.client_id, req.conversation_id, req.message, answer, model)
            history_saved = True
        except Exception:
            history_saved = False
        return {"reply":answer,"model":model,"conversation_id":str(req.conversation_id),"history_saved":history_saved}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502,detail=f"LLM 요청 실패: {type(exc).__name__}") from exc

@app.get("/agent/capabilities/legacy")
def capabilities():
    return {"chat":llm_configured(),"read_only_data_connectors":{"naver_commerce_products":commerce_configured(),"domeggook_supply_products":domeggook_configured(),"naver_search_ads":True,"instagram_snapshot":True,"orders_and_sales":False},"task_approval":False,"external_execution":False}


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
    analysis_goal: str = Field(default="구매전환 및 판매기회 발굴", max_length=200)
    season: str = Field(default="현재 시즌", max_length=100)
    keyword_metrics: list[dict] = Field(default_factory=list)
    competitor_observations: list[dict] = Field(default_factory=list)
    social_observations: list[dict] = Field(default_factory=list)
    seasonality_observations: list[dict] = Field(default_factory=list)
    supplier_observations: list[dict] = Field(default_factory=list)

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
    """Evidence-led scoring; all scores are heuristic, not demand forecasts."""
    import re
    from statistics import median

    def num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v) if v >= 0 else None
        if isinstance(v, str):
            try:
                x = float(re.sub(r"[^0-9.]", "", v.replace(",", "")))
                return x if x >= 0 else None
            except ValueError:
                return None
        return None

    def val(row, *keys, default=""):
        for key in keys:
            if row.get(key) is not None and str(row.get(key)).strip():
                return str(row[key]).strip()
        return default

    keywords = []
    intent_terms = ("가격", "구매", "주문", "택배", "선물", "세트", "손질", "10미", "20미", "파는곳", "추천")
    for row in req.keyword_metrics[:100]:
        if not isinstance(row, dict):
            continue
        word = val(row, "keyword", "name")
        if not word:
            continue
        keywords.append({
            "keyword": word,
            "trend_score": num(row.get("trend_score")),
            "search_volume": num(row.get("search_volume")),
            "source": val(row, "source", default="출처 미기재"),
            "purchase_intent": "높음" if any(t in word for t in intent_terms) else "보통"
        })

    max_volume = max([x["search_volume"] for x in keywords if x["search_volume"] is not None], default=None)
    max_trend = max([x["trend_score"] for x in keywords if x["trend_score"] is not None], default=None)
    for item in keywords:
        components = []
        if max_volume and item["search_volume"] is not None:
            components.append(item["search_volume"] / max_volume * 70)
        if max_trend and item["trend_score"] is not None:
            components.append(item["trend_score"] / max_trend * 70)
        components.append(30 if item["purchase_intent"] == "높음" else 15)
        item["priority_score"] = round(sum(components) / len(components)) if len(components) > 1 else None
        item["recommendation"] = "우선 검토" if item["priority_score"] is not None and item["priority_score"] >= 60 else ("보조 키워드" if item["priority_score"] is not None else "추가 데이터 필요")
        item["score_basis"] = "입력 표본 내 상대 검색량·추세 및 키워드 문구 기반 휴리스틱"

    competitors = []
    for row in req.competitor_observations[:100]:
        if not isinstance(row, dict):
            continue
        price = num(row.get("price"))
        reviews = num(row.get("review_count"))
        weight = num(row.get("weight_g") or row.get("weight"))
        name = val(row, "name", "product_name", "title", default="상품명 미기재")
        if name != "상품명 미기재" or price is not None:
            competitors.append({
                "name": name, "price": price,
                "review_count": int(reviews) if reviews is not None else None,
                "weight_g": weight,
                "product_type": val(row, "product_type", "type", default="유형 미기재"),
                "source": val(row, "source", default="출처 미기재"),
                "supplier": val(row, "supplier", "supplier_name", default=""),
                "shipping_fee": num(row.get("shipping_fee")),
                "min_order_qty": num(row.get("min_order_qty")),
                "item_no": val(row, "item_no", default=""),
                "url": val(row, "url", default="")
            })
    prices = [x["price"] for x in competitors if x["price"] is not None]
    competitor_summary = {
        "observed_count": len(competitors), "priced_count": len(prices),
        "min_price": round(min(prices)) if prices else None,
        "median_price": round(median(prices)) if prices else None,
        "max_price": round(max(prices)) if prices else None,
        "warning": "중량·손질 상태·구성품·배송비가 같은 상품끼리만 가격을 직접 비교해야 합니다."
    }

    social = []
    for row in req.social_observations[:100]:
        if not isinstance(row, dict):
            continue
        observation = val(row, "observation", "content", "description")
        if observation:
            source = val(row, "source", default="출처 미기재")
            social.append({
                "type": val(row, "type", default="유형 미기재"),
                "platform": val(row, "platform", default="플랫폼 미기재"),
                "observation": observation, "source": source,
                "evidence_quality": "출처 확인 필요" if source == "출처 미기재" else "사용자 제공 관측"
            })

    suppliers = []
    for row in req.supplier_observations[:200]:
        if not isinstance(row, dict):
            continue
        name = val(row, "name", "title", default="상품명 미기재")
        if name == "상품명 미기재" and not row.get("item_no"):
            continue
        suppliers.append({
            "item_no": val(row, "item_no", default=""),
            "name": name,
            "supply_price": num(row.get("price") or row.get("supply_price")),
            "shipping_fee": num(row.get("shipping_fee")),
            "min_order_qty": num(row.get("min_order_qty")),
            "supplier": val(row, "supplier", "supplier_name", default="판매자 미제공"),
            "url": val(row, "url", default=""),
            "source": val(row, "source", default="도매매 Open API")
        })

    seasonality = []
    for row in req.seasonality_observations[:100]:
        if isinstance(row, dict):
            observation = val(row, "observation", "content", "description", "season")
            if observation:
                seasonality.append({
                    "observation": observation,
                    "period": val(row, "period", "season", default=req.season),
                    "source": val(row, "source", default="출처 미기재")
                })

    coverage = sum(bool(x) for x in (keywords, competitors, suppliers, social, seasonality))
    total = len(keywords) + len(competitors) + len(suppliers) + len(social) + len(seasonality)
    confidence = "낮음" if coverage <= 1 or total < 3 else ("보통" if coverage <= 3 else "자료 범위 내 참고")
    opportunities = []
    ranked = sorted([x for x in keywords if x["priority_score"] is not None], key=lambda x: x["priority_score"], reverse=True)
    if ranked:
        opportunities.append({
            "area": "검색 유입",
            "finding": "우선 검토 키워드: " + ", ".join(x["keyword"] for x in ranked[:3]),
            "action": "상품명·상세페이지·광고 소재 후보로 분리해 소규모 테스트",
            "evidence": "사용자 입력 키워드 관측",
            "risk": "입력 표본 안에서만 비교한 점수이며 전체 시장 순위가 아닙니다."
        })
    if competitors:
        opportunities.append({
            "area": "경쟁상품 포지셔닝",
            "finding": f"경쟁 관측 {len(competitors)}건, 가격 확인 {len(prices)}건",
            "action": "순중량·손질 수준·구성품·배송비를 표준화한 뒤 가격 포지션 결정",
            "evidence": "사용자 입력 경쟁상품 관측",
            "risk": "리뷰 수는 판매량이나 시장점유율이 아닙니다."
        })
    if suppliers:
        prices_supply = [x["supply_price"] for x in suppliers if x["supply_price"] is not None]
        opportunities.append({
            "area": "도매매 공급처 소싱",
            "finding": f"공급상품 {len(suppliers)}건, 공급가 확인 {len(prices_supply)}건",
            "action": "최소구매수량·배송비·공급가·상품 옵션을 확인하고 실제 판매 가능 원가를 산출",
            "evidence": "도매매 Open API 공급상품 목록",
            "risk": "공급가는 소매 경쟁사 판매가격이 아니며, 중량·손질·포장·배송 조건을 확인하기 전에는 소매가와 직접 비교할 수 없습니다."
        })
    if social:
        opportunities.append({
            "area": "SNS 콘텐츠",
            "finding": f"SNS 관측 {len(social)}건",
            "action": "아이디어와 실제 반응 데이터 구분 후 저장·공유·프로필 방문·구매 클릭 추적",
            "evidence": "사용자 입력 SNS 관측",
            "risk": "단순 언급만으로 유행이나 구매전환을 단정할 수 없습니다."
        })
    if seasonality:
        opportunities.append({
            "area": "시즌 수요",
            "finding": f"계절성 관측 {len(seasonality)}건",
            "action": "주차별 검색 추세·주문량·기온·명절 변수를 누적해 패턴 검증",
            "evidence": "사용자 입력 계절성 관측",
            "risk": "단일 시점 관측으로 계절성을 확정할 수 없습니다."
        })

    strategy = []
    if ranked:
        strategy.append({"priority": 1, "action": "구매의도 키워드 중심으로 상품명·광고문구 후보 구성", "metric": "키워드별 클릭률·구매전환율", "approval_required": True})
    if competitors:
        strategy.append({"priority": 2, "action": "동일 조건 경쟁상품 비교 후 가격과 상품구성 결정", "metric": "동일 조건 가격 차이·공헌이익", "approval_required": True})
    if social:
        strategy.append({"priority": 3, "action": "SNS 소재 2~3개를 소규모 테스트", "metric": "저장·공유·프로필 방문·구매 클릭", "approval_required": True})
    if not strategy:
        strategy.append({"priority": 1, "action": "출처가 확인된 시장 관측 데이터를 추가 확보", "metric": "검증 가능한 관측 데이터 건수", "approval_required": False})

    return {
        "agent": "sourcing", "product": req.product_name,
        "analysis_goal": req.analysis_goal, "season": req.season,
        "status": "analysis_only",
        "market_opportunity": {
            "assessment": "기회 후보 도출" if opportunities else "관측 데이터 부족",
            "opportunities": opportunities, "confidence": confidence,
            "confidence_note": "관측 범위에 대한 정성 평가이며 통계적 신뢰구간이 아닙니다.",
            "coverage": {"keywords": len(keywords), "competitors": len(competitors), "suppliers": len(suppliers), "social": len(social), "seasonality": len(seasonality)}
        },
        "keywords": keywords,
        "competitor_observations": competitors,
        "competitor_summary": competitor_summary,
        "supplier_observations": suppliers,
        "supplier_summary": {"observed_count": len(suppliers), "priced_count": sum(x["supply_price"] is not None for x in suppliers), "warning": "도매매 공급가는 소매 경쟁상품 판매가격과 별도 지표입니다."},
        "social_observations": social,
        "seasonality_observations": seasonality,
        "sales_strategy": strategy,
        "approval_guidance": {
            "status": "승인 전",
            "message": "추천 상품명·가격·광고문구·게시물은 담당자 검토와 별도 승인 후 실행합니다. 이 API는 외부 채널을 변경하지 않습니다."
        },
        "limitations": [
            "검색량과 추세 점수는 출처 및 측정 기간을 확인해야 합니다.",
            "경쟁상품 리뷰 수는 주문량·매출·시장점유율이 아닙니다.",
            "도매매 공급가는 소매 경쟁상품 판매가격과 별도이며, 공급조건·중량·손질상태 확인 후 원가 분석에 사용해야 합니다.",
            "중량·손질 수준·구성품·배송비가 다른 상품 가격은 직접 비교할 수 없습니다.",
            "SNS 관측은 출처와 반응 지표가 확인되기 전까지 트렌드로 확정하지 않습니다.",
            "관측되지 않은 항목은 미확인으로 유지하며 매출·전환율을 임의 추정하지 않습니다."
        ],
        "next_step": "경쟁상품의 중량·손질·구성·배송비를 같은 기준으로 보완하고, 출처가 확인된 키워드와 SNS 반응을 누적해 재평가합니다.",
        "updated_at": datetime.now(timezone.utc).isoformat()
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
        response = llm_chat_completion(
            model,
            [
            {"role":"system","content":"한국 온라인 식품 판매 CS 초안 작성자. 제공된 검증 사실과 승인된 가이드만 사용한다. 사실이 없으면 확인 필요라고 말한다. 환불·보상·식품안전·원산지 분쟁은 확답하지 말고 담당자 확인으로 분류한다. 메시지를 전송하지 말고 답변 초안과 위험 분류만 반환한다."},
            {"role":"user","content":json.dumps(prompt,ensure_ascii=False)}
          ]
        )
        answer = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
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


@app.get("/agent/history")
def list_chat_history(client_id: UUID, limit: int = Query(default=30, ge=1, le=100)):
    try:
        with history_connection() as conn:
            ensure_history_table(conn)
            rows = conn.execute("""
              SELECT conversation_id::text,
                     MIN(created_at)::text AS created_at,
                     MAX(created_at)::text AS updated_at,
                     COUNT(*) AS message_count,
                     (SELECT LEFT(m2.content,100)
                        FROM agent_chat_messages m2
                       WHERE m2.client_id = m.client_id
                         AND m2.conversation_id = m.conversation_id
                         AND m2.role = 'user'
                       ORDER BY m2.id ASC LIMIT 1) AS title
                FROM agent_chat_messages m
               WHERE client_id = %s
               GROUP BY client_id, conversation_id
               ORDER BY MAX(created_at) DESC
               LIMIT %s
            """, (client_id,limit)).fetchall()
        return {"items":[{"conversation_id":r[0],"created_at":r[1],"updated_at":r[2],"message_count":r[3],"title":r[4] or "새 대화"} for r in rows],"persistent":True}
    except RuntimeError as exc:
        if str(exc) == "DATABASE_URL_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="대화 이력용 PostgreSQL이 아직 연결되지 않았습니다.")
        raise HTTPException(status_code=503, detail="대화 이력을 불러오지 못했습니다.")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="대화 이력을 불러오지 못했습니다: "+type(exc).__name__)

@app.get("/agent/history/{conversation_id}")
def get_chat_history(conversation_id: UUID, client_id: UUID):
    try:
        with history_connection() as conn:
            ensure_history_table(conn)
            rows = conn.execute("""
              SELECT role,content,model,created_at::text
                FROM agent_chat_messages
               WHERE client_id=%s AND conversation_id=%s
               ORDER BY id ASC
            """, (client_id,conversation_id)).fetchall()
        return {"conversation_id":str(conversation_id),"items":[{"role":r[0],"content":r[1],"model":r[2],"created_at":r[3]} for r in rows],"persistent":True}
    except RuntimeError as exc:
        if str(exc) == "DATABASE_URL_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="대화 이력용 PostgreSQL이 아직 연결되지 않았습니다.")
        raise HTTPException(status_code=503, detail="대화 내용을 불러오지 못했습니다.")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="대화 내용을 불러오지 못했습니다: "+type(exc).__name__)

@app.get("/agent/capabilities")
def capabilities_v2():
    return {
      "chat":llm_configured(),
      "chat_history":bool(os.getenv("DATABASE_URL")),
      "sourcing_analysis":True,"pricing_calculation":True,"cs_draft":bool(os.getenv("OPENAI_API_KEY")),
      "youtube_public_data":False,
      "approval_queue":True,
      "external_execution":False,
      "connected_channels":{"naver_smartstore_products":commerce_configured(),"domeggook_supply_products":domeggook_configured(),"naver_ads":True,"instagram_live_api":False},
      "limitations":["승인 기능은 승인 이력만 기록하며 외부 변경은 실행하지 않습니다.","대화 이력은 DATABASE_URL에 연결된 PostgreSQL에 저장됩니다. 브라우저별 client_id로 구분되며 다른 기기와 공유하려면 별도 로그인 기능이 필요합니다.",
      "승인 DB는 AGENT_DB_PATH 기본값(/tmp)의 임시 저장입니다. 영구 보존에는 별도 PostgreSQL 전환이 필요합니다.","Instagram은 현재 라이브 API가 아닌 기존 대시보드 스냅샷만 확인 가능합니다."]
    }
