#!/usr/bin/env python3
"""
Nadlan.gov.il scraper — runs via GitHub Actions daily.
Sends deals to: https://madad-sheli-sale.lovable.app/api/public/hooks/ingest-nadlan
"""

import os
import sys
import json
import time
import hmac
import hashlib
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from twocaptcha import TwoCaptcha

# ── Config from GitHub secrets ─────────────────────────────
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
CAPTCHA_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")

# Nadlan API endpoint (inspect the real site for exact URL)
NADLAN_API = "https://www.nadlan.gov.il/Nadlan.REST/Main/GetAssestAndDeals"

CITIES = [
    {"name": "תל אביב - יפו", "code": "5000"},
    {"name": "ירושלים", "code": "3000"},
    {"name": "חיפה", "code": "4000"},
    {"name": "ראשון לציון", "code": "8300"},
    {"name": "פתח תקווה", "code": "7900"},
]

MONTHS_BACK = 3


def log(msg: str):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


def solve_captcha(site_key: str, page_url: str) -> str | None:
    """Solve reCAPTCHA v2 via 2Captcha."""
    if not CAPTCHA_API_KEY:
        log("ERROR: CAPTCHA_API_KEY not set")
        return None
    try:
        solver = TwoCaptcha(CAPTCHA_API_KEY)
        result = solver.recaptcha(sitekey=site_key, url=page_url)
        return result.get("code") if isinstance(result, dict) else str(result)
    except Exception as e:
        log(f"Captcha solve failed: {e}")
        return None


def fetch_city_deals(city: dict, start_date: str, end_date: str):
    """Fetch deals for a single city + date range."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.nadlan.gov.il",
        "Referer": "https://www.nadlan.gov.il/",
    }

    payload = {
        "CityCode": city["code"],
        "CityName": city["name"],
        "FromDate": start_date,
        "ToDate": end_date,
        "DealType": 1,  # 1 = sale deals
        "Page": 1,
        "PageSize": 100,
    }

    # Try without captcha first
    try:
        resp = requests.post(NADLAN_API, headers=headers, json=payload, timeout=60)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log(f"Initial request failed for {city['name']}: {e}")

    # If blocked, solve captcha
    log(f"Attempting captcha solve for {city['name']}...")
    captcha_token = solve_captcha(
        site_key="6LdXXXXXXXXXX",  # ← REPLACE with real sitekey from nadlan.gov.il
        page_url="https://www.nadlan.gov.il/",
    )
    if captcha_token:
        headers["X-Captcha-Token"] = captcha_token
        try:
            resp = requests.post(NADLAN_API, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log(f"Captcha retry failed for {city['name']}: {e}")

    log(f"FAILED to fetch deals for {city['name']}")
    return None


def normalize_deal(raw: dict, city_name: str) -> dict:
    """Convert Nadlan raw record to our app schema."""
    return {
        "deal_id": str(raw.get("DealID") or raw.get("id") or ""),
        "city": city_name,
        "address": raw.get("Address") or raw.get("StreetName") or None,
        "gush": str(raw.get("Gush")) if raw.get("Gush") else None,
        "helka": str(raw.get("Helka")) if raw.get("Helka") else None,
        "rooms": raw.get("Rooms") if isinstance(raw.get("Rooms"), (int, float)) else None,
        "built_size": raw.get("BuiltSize") if isinstance(raw.get("BuiltSize"), (int, float)) else None,
        "floor": raw.get("Floor") if isinstance(raw.get("Floor"), (int, float)) else None,
        "price": raw.get("DealValue") if isinstance(raw.get("DealValue"), (int, float)) else None,
        "deal_date": raw.get("DealDate") or None,
        "raw": raw,
    }


def sign_payload(body: str) -> str:
    """Create HMAC-SHA256 signature for webhook."""
    return hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def send_to_app(deals: list[dict]) -> bool:
    """POST deals to the app webhook."""
    if not WEBHOOK_URL:
        log("ERROR: WEBHOOK_URL not set")
        return False

    payload = json.dumps({"deals": deals}, ensure_ascii=False, default=str)
    signature = sign_payload(payload)

    try:
        resp = requests.post(
            WEBHOOK_URL,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            },
            timeout=120,
        )
        log(f"Webhook response: {resp.status_code} — {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        log(f"Webhook request failed: {e}")
        return False


def main():
    # Validate env
    missing = []
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")
    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")
    if not CAPTCHA_API_KEY:
        missing.append("CAPTCHA_API_KEY")

    if missing:
        log(f"FATAL: Missing secrets: {', '.join(missing)}")
        sys.exit(1)

    end = datetime.now()
    start = end - timedelta(days=MONTHS_BACK * 30)

    all_deals = []
    for city in CITIES:
        log(f"Fetching {city['name']} ({city['code']}) ...")
        raw = fetch_city_deals(
            city,
            start.strftime("%d/%m/%Y"),
            end.strftime("%d/%m/%Y"),
        )
        if raw and isinstance(raw, dict):
            deals_list = raw.get("Results") or raw.get("Data") or raw.get("deals") or []
            if not deals_list and "data" in raw:
                deals_list = raw["data"]
            log(f"  → Received {len(deals_list)} raw records")
            for d in deals_list:
                normalized = normalize_deal(d, city["name"])
                if normalized["deal_id"]:
                    all_deals.append(normalized)
        time.sleep(2)  # polite delay between cities

    log(f"Total normalized deals: {len(all_deals)}")

    if all_deals:
        success = send_to_app(all_deals)
        sys.exit(0 if success else 1)
    else:
        log("No deals to send.")
        sys.exit(0)


if __name__ == "__main__":
    main()
