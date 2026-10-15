#!/usr/bin/env python3
"""네이버 뉴스 검색 자동 수집 (NAVER API HUB).

사용:
  export NAVER_API_KEY_ID=...
  export NAVER_API_KEY=...
  python naver_news.py --hours 24 --out data/news.json

필요 패키지: requests (pip install requests)
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
KST = timezone(timedelta(hours=9))
TAG_RE = re.compile(r"<[^>]+>")
NORM_RE = re.compile(r"[^0-9a-z가-힣]")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def norm_title(title: str) -> str:
    return NORM_RE.sub("", title.lower())


def fetch(query: str, cutoff: datetime, session: requests.Session,
          headers: dict, per_page: int = 100, max_start: int = 1000):
    """sort=date 로 최신순 조회, cutoff 이전 기사가 나오면 중단."""
    start = 1
    while start <= max_start:
        params = {"query": query, "display": per_page, "start": start,
                  "sort": "date", "format": "json"}
        for attempt in range(3):
            r = session.get(API_URL, params=params, headers=headers, timeout=10)
            if r.status_code == 200:
                break
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            # 4xx: 에러코드(SE01~SE06 등) 확인 후 중단
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        else:
            raise RuntimeError(f"재시도 초과: {query}")

        items = r.json().get("items", [])
        if not items:
            return
        reached_old = False
        for it in items:
            pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            if pub < cutoff:
                reached_old = True
                continue
            yield {
                "title": clean(it["title"]),
                "summary": clean(it["description"]),
                "link": it["link"],
                "originallink": it.get("originallink") or it["link"],
                "pubDate": pub.isoformat(),
                "query": query,
            }
        if reached_old or len(items) < per_page:
            return
        start += per_page
        time.sleep(0.15)  # rate limit 완화


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="keywords.json")
    ap.add_argument("--hours", type=int, default=24, help="최근 N시간")
    ap.add_argument("--out", default="data/news.json")
    args = ap.parse_args()

    key_id, key = os.getenv("NAVER_API_KEY_ID"), os.getenv("NAVER_API_KEY")
    if not key_id or not key:
        sys.exit("환경변수 NAVER_API_KEY_ID / NAVER_API_KEY 필요")
    headers = {"X-NCP-APIGW-API-KEY-ID": key_id, "X-NCP-APIGW-API-KEY": key}

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    exclude = [w.lower() for w in cfg.get("exclude_words", [])]
    cutoff = datetime.now(KST) - timedelta(hours=args.hours)

    seen_links, seen_titles, results = set(), set(), []
    errors = []
    with requests.Session() as s:
        for category, queries in cfg["categories"].items():
            for q in queries:
                try:
                    for art in fetch(q, cutoff, s, headers):
                        text = (art["title"] + " " + art["summary"]).lower()
                        if any(w in text for w in exclude):
                            continue
                        nt = norm_title(art["title"])
                        if art["originallink"] in seen_links or nt in seen_titles:
                            continue
                        seen_links.add(art["originallink"])
                        seen_titles.add(nt)
                        art["category"] = category
                        results.append(art)
                except Exception as e:  # 한 키워드 실패가 전체를 막지 않도록
                    errors.append({"query": q, "error": str(e)})
                    print(f"[WARN] {q}: {e}", file=sys.stderr)

    results.sort(key=lambda x: x["pubDate"], reverse=True)
    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "window_hours": args.hours,
        "count": len(results),
        "errors": errors,
        "items": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"{len(results)}건 저장 → {args.out} (오류 {len(errors)}건)")


if __name__ == "__main__":
    main()
