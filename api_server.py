import os
from datetime import datetime, timedelta, timezone

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


@app.get("/api/naver/adgroup-stats")
def adgroup_stats(days: int = 7):
    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days는 1~30 범위여야 합니다.")
    try:
        client = get_ad_client()
        groups = client.get_adgroups()
        kst = timezone(timedelta(hours=9))
        until = datetime.now(kst).date()
        since = until - timedelta(days=days - 1)
        stats_by_id = {}
        daily_by_date = {}
        stats_record_count = 0
        for group in groups:
            group_id = group.get("nccAdgroupId")
            if not group_id:
                continue
            raw = client.get_stats([group_id], since.isoformat(), until.isoformat())
            records = raw.get("data", []) if isinstance(raw, dict) else raw
            if not isinstance(records, list):
                records = []
            stats_record_count += len(records)
            aggregate = {"impCnt": 0, "clkCnt": 0, "ccnt": 0, "_imp": False, "_clk": False, "_conv": False}
            for row in records:
                if not isinstance(row, dict):
                    continue
                for field, flag in (("impCnt", "_imp"), ("clkCnt", "_clk"), ("ccnt", "_conv")):
                    value = row.get(field)
                    if value is not None:
                        aggregate[field] += float(value)
                        aggregate[flag] = True
                row_date = str(row.get("dateStart") or row.get("date") or row.get("dt") or row.get("dateTime") or "")[:10]
                if row_date:
                    day = daily_by_date.setdefault(row_date, {"date": row_date, "impCnt": 0, "clkCnt": 0, "ccnt": 0, "_hasConv": False})
                    for field in ("impCnt", "clkCnt"):
                        if row.get(field) is not None:
                            day[field] += float(row[field])
                    if row.get("ccnt") is not None:
                        day["ccnt"] += float(row["ccnt"])
                        day["_hasConv"] = True
            stats_by_id[group_id] = aggregate
        items = []
        matched_count = 0
        for group in groups:
            group_id = group.get("nccAdgroupId")
            stat = stats_by_id.get(group_id)
            if stat and any(stat[k] for k in ("_imp", "_clk", "_conv")):
                matched_count += 1
            items.append({
                "id": group_id,
                "name": group.get("name"),
                "status": group.get("status"),
                "impCnt": stat["impCnt"] if stat and stat["_imp"] else None,
                "clkCnt": stat["clkCnt"] if stat and stat["_clk"] else None,
                "convCnt": stat["ccnt"] if stat and stat["_conv"] else None,
                "hasStats": bool(stat and any(stat[k] for k in ("_imp", "_clk", "_conv"))),
            })
        daily = []
        for key in sorted(daily_by_date):
            row = daily_by_date[key]
            daily.append({"date": row["date"], "impCnt": row["impCnt"], "clkCnt": row["clkCnt"], "convCnt": row["ccnt"] if row["_hasConv"] else None})
        return {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": days,
            "count": len(items),
            "statsRecordCount": stats_record_count,
            "matchedAdgroupCount": matched_count,
            "dataStatus": "matched" if matched_count else "no_rows",
            "daily": daily,
            "items": items,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
