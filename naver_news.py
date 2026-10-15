#!/usr/bin/env python3
"""네이버 뉴스 검색 자동 수집 + 카테고리 분류 + 관련도 점수 (NAVER API HUB).

사용:
  export NAVER_API_KEY_ID=...
  export NAVER_API_KEY=...
  python naver_news.py --hours 24 --out data/news.json

필요 패키지: requests (pip install requests)

관련도 점수(score) = 카테고리 용어 점수 + "의료 AI"/"병원 AI" 구문 가점 + 여러 검색어에 중복 검색된 가점
  - 용어 점수: 핵심 용어 제목 4점/요약 2점, 보조 용어 제목 2점/요약 1점 (용어당 1회)
  - 구문 가점(must_include): 제목 3점, 요약 1점
  - 중복 가점: 서로 다른 검색어에 추가로 걸릴 때마다 +1 (최대 +3)
정렬은 score 내림차순, 같으면 최신순.
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
            }
        if reached_old or len(items) < per_page:
            return
        start += per_page
        time.sleep(0.15)  # rate limit 완화


# ---------------------------------------------------------------- 설정·분류
def compile_cfg(cfg: dict) -> dict:
    rx = lambda pats: [re.compile(p, re.I) for p in pats]
    cats, query_cat = {}, {}
    for name, c in cfg["categories"].items():
        cats[name] = {"strong": rx(c.get("terms", [])), "weak": rx(c.get("weak_terms", []))}
        for q in c.get("queries", []):
            query_cat.setdefault(q, name)
    order = list(cfg["categories"].keys())
    return {
        "cats": cats,
        "query_cat": query_cat,
        "order": order,
        "fallback": cfg.get("fallback_category") or order[-1],
        "must": rx(cfg.get("must_include", [])),
        "gate": rx(cfg.get("require_any", [])),
        "exclude": [w.lower() for w in cfg.get("exclude_words", [])],
    }


def _score(patterns, title, summary, w_title, w_sum):
    s = 0
    for p in patterns:
        if p.search(title):
            s += w_title
        elif p.search(summary):
            s += w_sum
    return s


def classify_and_score(art: dict, cc: dict) -> None:
    t, s = art["title"], art["summary"]
    ts = {}
    for name, c in cc["cats"].items():
        if name == cc["fallback"]:
            continue
        ts[name] = _score(c["strong"], t, s, 4, 2) + _score(c["weak"], t, s, 2, 1)

    query_cats = [cc["query_cat"][q] for q in art["queries"] if q in cc["query_cat"]]
    # 용어 점수 2 이상인 카테고리 중, 해당 카테고리 검색어로 찾은 경우 +3 가점 후 최고점 선택
    cand = {n: v + (3 if n in query_cats else 0) for n, v in ts.items() if v >= 2}
    if cand:
        category = max(cand, key=lambda n: (cand[n], -cc["order"].index(n)))
    else:
        category = query_cats[0] if query_cats else cc["fallback"]

    phrase = _score(cc["must"], t, s, 3, 1)
    multi = min(len(art["queries"]) - 1, 3)
    art["category"] = category
    art["score"] = ts.get(category, 0) + phrase + multi


# ---------------------------------------------------------------------- main
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
    cc = compile_cfg(cfg)
    cutoff = datetime.now(KST) - timedelta(hours=args.hours)

    by_link, by_title, results = {}, {}, []
    errors, dropped = [], 0
    with requests.Session() as s:
        for q in cc["query_cat"]:  # 검색어는 중복 없이 1회씩
            try:
                for art in fetch(q, cutoff, s, headers):
                    full = art["title"] + " " + art["summary"]
                    if any(w in full.lower() for w in cc["exclude"]):
                        continue
                    # 네이버는 단어를 따로 매칭하므로, 의료 AI 관련 구문이 있어야 통과
                    if cc["gate"] and not any(r.search(full) for r in cc["gate"]):
                        dropped += 1
                        continue
                    nt = norm_title(art["title"])
                    prev = by_link.get(art["originallink"]) or by_title.get(nt)
                    if prev:
                        if q not in prev["queries"]:
                            prev["queries"].append(q)
                        continue
                    art["queries"] = [q]
                    by_link[art["originallink"]] = art
                    by_title[nt] = art
                    results.append(art)
            except Exception as e:  # 한 키워드 실패가 전체를 막지 않도록
                errors.append({"query": q, "error": str(e)})
                print(f"[WARN] {q}: {e}", file=sys.stderr)

    for art in results:
        classify_and_score(art, cc)
        art["query"] = art["queries"][0]
    # 관련도 내림차순, 같으면 최신순 (pubDate 는 모두 +09:00 ISO 문자열이라 문자열 비교 가능)
    results.sort(key=lambda a: (a["score"], a["pubDate"]), reverse=True)

    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "window_hours": args.hours,
        "count": len(results),
        "filtered_out": dropped,
        "category_order": cc["order"],
        "errors": errors,
        "items": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    per_cat = {c: sum(1 for a in results if a["category"] == c) for c in cc["order"]}
    print(f"{len(results)}건 저장 → {args.out} (필터 제외 {dropped}건, 오류 {len(errors)}건)")
    print(per_cat)


if __name__ == "__main__":
    main()
