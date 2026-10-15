#!/usr/bin/env python3
"""정부 부처 발표(보도자료) 중 의료 AI 관련 건 수집 → data/gov_news.json
사용: python gov_news.py --config gov_sources.json --out data/gov_news.json
"""
import argparse, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
      "Accept-Language": "ko-KR,ko;q=0.9"}


def rx(ps):
    return [re.compile(p, re.I) for p in ps]


def strip(t):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()


def parse_date(s):
    s = (s or "").strip()
    s = re.sub(r"^(\d{4})[./](\d{1,2})[./](\d{1,2})", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", s)
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d.astimezone(KST)


def get_xml(url, session):
    for attempt in range(3):
        r = session.get(url, headers=UA, timeout=30)
        if r.status_code >= 500 or r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        head = r.content[:200].lstrip().lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<rss") or head.startswith(b"<feed")):
            raise RuntimeError("RSS가 아닌 응답(HTML 등) - 주소 확인 필요")
        return ET.fromstring(r.content)
    raise RuntimeError(f"HTTP {r.status_code}")


def parse_items(root):
    for it in root.iter("item"):
        pub = parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not pub:  # 날짜가 없거나 형식을 모르는 RSS는 버리지 않고 수집일(오늘)로 처리
            pub = datetime.now(KST)
        yield {"title": strip(it.findtext("title")), "link": (it.findtext("link") or "").strip(),
               "summary": strip(it.findtext("description"))[:300], "pubDate": pub}


def kind_of(text, kinds):
    for name, words in kinds.items():
        if any(w in text for w in words):
            return name
    return "기타 발표"


def fetch_msit_api(api, key, cutoff, session):
    """공공데이터포털 과기정통부 보도자료 API (pressReleaseList). 제목·상세URL·게시일만 제공."""
    out, rows = [], int(api.get("rows", 10))
    for page in range(1, int(api.get("max_pages", 8)) + 1):
        r = session.get(api["url"], params={"serviceKey": key, "numOfRows": rows, "pageNo": page, "returnType": "xml"},
                        headers=UA, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        code = (root.findtext(".//resultCode") or "").strip()
        if code != "00":
            raise RuntimeError(f"API 오류 {code}: {(root.findtext('.//resultMsg') or '')[:60]}")
        got = list(root.iter("item"))
        if not got:
            break
        oldest_ok = False
        for it in got:
            d = parse_date(it.findtext("pressDt"))
            if not d:
                continue
            if d >= cutoff - timedelta(days=1):  # 날짜만 있어 하루 여유
                oldest_ok = True
            if d < cutoff - timedelta(days=1):
                continue
            out.append({"title": strip(it.findtext("subject")), "link": (it.findtext("viewUrl") or "").strip(),
                        "summary": ("담당부서: " + strip(it.findtext("deptName"))) if it.findtext("deptName") else "",
                        "pubDate": d})
        if not oldest_ok:  # 이 페이지가 전부 기간 밖이면 중단
            break
        time.sleep(0.3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="gov_sources.json")
    ap.add_argument("--out", default="data/gov_news.json")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ai, health = rx(cfg["require_ai"]), rx(cfg["require_health"])
    policy = rx(cfg.get("policy_pass", []))
    cutoff = datetime.now(KST) - timedelta(hours=cfg.get("hours", 168))
    api_key = os.getenv("DATA_GO_KR_KEY", "").strip()

    def gnews_url(q):
        return f"https://news.google.com/rss/search?q={quote(q + ' when:7d')}&hl=ko&gl=KR&ceid=KR:ko"

    def clean_err(e):  # 인증키가 오류 문구(URL)에 섞여 저장소에 기록되지 않도록 제거
        msg = str(e)
        if api_key:
            msg = msg.replace(api_key, "***").replace(quote(api_key, safe=""), "***")
        return re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", msg)

    items, seen, errors = [], set(), []
    dropped = 0

    def add(a, agency, hc, mode):
        nonlocal dropped
        if a["pubDate"] < cutoff - (timedelta(days=1) if mode == "api" else timedelta(0)):
            return
        text = a["title"] + " " + a["summary"]
        if mode == "gnews":  # 제목 끝 '- 매체명' 제거
            a["title"] = re.sub(r"\s-\s[^-]+$", "", a["title"])
        if not any(p.search(text) for p in ai) or not (hc or any(p.search(text) for p in health) or any(p.search(text) for p in policy)):
            dropped += 1
            return
        key = re.sub(r"\W+", "", a["title"])
        if key in seen:
            return
        seen.add(key)
        items.append({"title": a["title"], "link": a["link"], "summary": a["summary"],
                      "pubDate": a["pubDate"].isoformat(), "agency": agency,
                      "kind": kind_of(text, cfg["kinds"]), "via": mode})

    with requests.Session() as s:
        # 1) 공식 API (공공데이터포털) — 먼저 처리해 같은 기사의 우선 출처가 되도록 함
        for api in cfg.get("apis", []):
            label = api["agency"] + (" " + api["board"] if api.get("board") else "")
            if not api_key:
                errors.append({"query": f"{label} (공공데이터포털 API)", "error": "DATA_GO_KR_KEY 비밀값이 없습니다"})
                print(f"[WARN] {label}: DATA_GO_KR_KEY 없음", file=sys.stderr)
                continue
            try:
                for a in fetch_msit_api(api, api_key, cutoff, s):
                    add(a, api["agency"], api.get("health_context", False), "api")
            except Exception as e:
                errors.append({"query": f"{label} (공공데이터포털 API)", "error": clean_err(e)[:120]})
                print(f"[WARN] {label}: {clean_err(e)}", file=sys.stderr)

        # 2) RSS + 3) Google News 검색
        jobs = [(f["agency"] + (" " + f["board"] if f.get("board") else ""), f["url"], f.get("health_context", False), "rss", f.get("fallback_site"), f["agency"]) for f in cfg["feeds"]]
        for g in cfg["gnews_queries"]:
            jobs.append((g["agency"], gnews_url(g["q"]), False, "gnews", None, g["agency"]))
        queue = list(jobs)
        while queue:
            label, url, hc, mode, fb, agency = queue.pop(0)
            try:
                for a in parse_items(get_xml(url, s)):
                    add(a, agency, hc, mode)
            except Exception as e:
                note = " → 사이트 검색으로 대체" if fb else ""
                errors.append({"query": f"{label} ({url})", "error": clean_err(e)[:120] + note})
                print(f"[WARN] {label} {url}: {clean_err(e)}{note}", file=sys.stderr)
                if fb:  # 부처 RSS 실패 시 해당 부처 사이트를 Google News 에서 검색
                    for q in (f"site:{fb} 인공지능", f"site:{fb} AI 의료"):
                        queue.append((agency, gnews_url(q), hc, "gnews", None, agency))
            time.sleep(0.5)

    items.sort(key=lambda a: a["pubDate"], reverse=True)
    agencies = []
    for a in items:
        if a["agency"] not in agencies:
            agencies.append(a["agency"])
    out = {"generated_at": datetime.now(KST).isoformat(), "window_hours": cfg.get("hours", 168),
           "count": len(items), "filtered_out": dropped, "category_order": agencies,
           "errors": errors, "items": items}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(items)}건 저장 → {args.out} (필터 제외 {dropped}건, 실패한 출처 {len(errors)}곳)")


if __name__ == "__main__":
    main()
