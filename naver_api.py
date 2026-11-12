import base64
import hashlib
import hmac
import json
import time

import requests


class NaverSearchAdClient:
    BASE_URL = "https://api.searchad.naver.com"

    def __init__(self, api_key, secret_key, customer_id):
        self.api_key = api_key
        self.secret_key = secret_key
        self.customer_id = str(customer_id)
        self.session = requests.Session()

    def _signature(self, timestamp, method, uri):
        message = f"{timestamp}.{method}.{uri}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(self, method, uri, params=None):
        timestamp = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Timestamp": timestamp,
            "X-API-KEY": self.api_key,
            "X-Customer": self.customer_id,
            "X-Signature": self._signature(timestamp, method, uri),
        }
        response = self.session.request(
            method=method,
            url=self.BASE_URL + uri,
            headers=headers,
            params=params,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Naver Search Ads API HTTP {response.status_code}: {response.text}"
            )
        return response.json() if response.text else []

    def get_campaigns(self):
        return self._request("GET", "/ncc/campaigns")

    def get_adgroups(self, campaign_id=None):
        params = {"nccCampaignId": campaign_id} if campaign_id else None
        return self._request("GET", "/ncc/adgroups", params=params)

    def get_stats(self, entity_ids, since, until):
        # Naver Stat API accepts a homogeneous list of entity IDs per request.
        params = {
            "ids": entity_ids,
            "fields": json.dumps(["impCnt", "clkCnt"]),
            "timeRange": json.dumps({"since": since, "until": until}),
            "timeIncrement": "allDays",
        }
        return self._request("GET", "/stats", params=params)
