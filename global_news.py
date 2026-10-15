#!/usr/bin/env python3
"""Google News RSS(영문, 미국·영국 에디션)로 해외 의료 AI 정책·규제·연구·도입 사례 수집 → data/global_news.json
API 키 불필요. 사용: python global_news.py --config global_keywords.json --out data/global_news.json
"""
import argparse, json, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests

KST = timezone(timedelta(hours=9))
EDITIONS = [("en-US", "US", "US:en"), ("en-GB", "GB", "GB:en")]
UA = {"User-Agent": "Mozilla/5.0 (compatible; medical-ai-dashboard/1.0)"}


def rx(patterns):
    return [re.compile(p, re.I) for p in patterns]


def split_source(title, src):
    """Google News 제목은 '기사 제목 - 매체명' 형식. 매체명 분리."""
    title = unescape(title).strip()
    if src and title.endswith(" - " + src):
        title = title[: -len(src) - 3].rstrip()
    return title


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def jaccard(a, b):
    a, b = set(a.split()), set(b.split())
    return len(a & b) / len(a | b) if a and b else 0.0


def fetch(query, window, hl, gl, ceid, session):
    url = (f"https://news.google.com/rss/search?q={quote(query + f' when:{window}')}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    for attempt in range(3):
        r = session.get(url, headers=UA, timeout=30)
        if r.status_code >= 500 or r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    else:
        raise RuntimeError(f"HTTP {r.status_code}")
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            continue
        yield {
            "title": split_source(it.findtext("title") or "", src),
            "link": (it.findtext("link") or "").strip(),
            "source": src,
            "source_url": src_el.get("url", "") if src_el is not None else "",
            "pubDate": pub.astimezone(KST).isoformat(),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="global_keywords.json")
    ap.add_argument("--out", default="data/global_news.json")
    ap.add_argument("--hours", type=int, default=48)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ai, health, excl = rx(cfg["require_ai"]), rx(cfg["require_health"]), rx(cfg["exclude"])
    pref = [s.lower() for s in cfg.get("preferred_sources", [])]
    cutoff = datetime.now(KST) - timedelta(hours=args.hours)
    window = cfg.get("window", "2d")

    seen, results, errors, dropped = {}, [], [], 0
    with requests.Session() as s:
        for cat, queries in cfg["categories"].items():
            for q in queries:
                for hl, gl, ceid in EDITIONS:
                    try:
                        for a in fetch(q, window, hl, gl, ceid, s):
                            if a["pubDate"] < cutoff.isoformat():
                                continue
                            t = a["title"]
                            if not (any(p.search(t) for p in ai) and any(p.search(t) for p in health)) \
                                    or any(p.search(t) for p in excl):
                                dropped += 1
                                continue
                            key = norm(t)
                            dup = seen.get(key) or next(
                                (r for r in results if jaccard(key, norm(r["title"])) >= 0.6), None)
                            if dup:
                                if q not in dup["queries"]:
                                    dup["queries"].append(q)
                                if a["source"] and a["source"] not in dup["sources"]:
                                    dup["sources"].append(a["source"])
                                continue
                            a.update({"category": cat, "queries": [q], "sources": [a["source"]] if a["source"] else []})
                            seen[key] = a
                            results.append(a)
                    except Exception as e:
                        errors.append({"query": f"{q} [{gl}]", "error": str(e)})
                        print(f"[WARN] {q} [{gl}]: {e}", file=sys.stderr)
                    time.sleep(0.5)

    for a in results:
        pref_hit = any(p in (a["source"] + " " + a["source_url"]).lower() for p in pref)
        a["preferred"] = pref_hit
        a["score"] = min(len(a["queries"]), 4) + min(len(a["sources"]) - 1, 3) + (2 if pref_hit else 0)
    results.sort(key=lambda a: (a["score"], a["pubDate"]), reverse=True)

    out = {"generated_at": datetime.now(KST).isoformat(), "window_hours": args.hours,
           "count": len(results), "filtered_out": dropped, "category_order": list(cfg["categories"]),
           "errors": errors, "items": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(results)}건 저장 → {args.out} (필터 제외 {dropped}건, 오류 {len(errors)}건)")


if __name__ == "__main__":
    main()
