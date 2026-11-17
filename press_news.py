#!/usr/bin/env python3
"""주요 일간지(조선·중앙·동아·한겨레·경향·한국일보)의 의료·병원·AI·정부 관련 기사를
Google News RSS(site: 검색)로 모아 data/press_news.json 에 저장한다. API 키 불필요.
사용: python press_news.py --config press_sources.json --out data/press_news.json
"""
import argparse, json, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; medical-ai-dashboard/1.0)"}


def rx(patterns):
    return [re.compile(p, re.I) for p in patterns]


def norm(t):
    return re.sub(r"[^0-9a-z가-힣]+", "", (t or "").lower())


def bigrams(t):
    n = norm(t)
    return {n[i:i + 2] for i in range(len(n) - 1)} or {n}


def similar(a, b):
    x, y = bigrams(a), bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def split_source(title, src):
    """Google News 제목은 '기사 제목 - 매체명' 형식."""
    title = unescape(title or "").strip()
    if src and title.endswith(" - " + src):
        title = title[: -len(src) - 3].rstrip()
    return title


def host_of(url):
    return (urlparse(url or "").hostname or "").lower().replace("www.", "")


def from_site(item, sites):
    """기사 출처가 해당 신문사 도메인인지 확인(소스 URL·링크 모두 확인)."""
    h = host_of(item.get("source_url")) or ""
    g = host_of(item.get("link")) or ""
    for s in sites:
        if h == s or h.endswith("." + s) or g == s or g.endswith("." + s):
            return True
    return False


def fetch(query, window, session):
    url = (f"https://news.google.com/rss/search?q={quote(query + f' when:{window}')}"
           f"&hl=ko&gl=KR&ceid=KR:ko")
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
        yield {"title": split_source(it.findtext("title") or "", src),
               "link": (it.findtext("link") or "").strip(),
               "source": src,
               "source_url": src_el.get("url", "") if src_el is not None else "",
               "pubDate": pub.astimezone(KST)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="press_sources.json")
    ap.add_argument("--out", default="data/press_news.json")
    ap.add_argument("--hours", type=int, default=0)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    hours = args.hours or int(cfg.get("hours", 48))
    window, excl = cfg.get("window", "2d"), rx(cfg.get("exclude", []))
    kws = cfg.get("keywords", [])
    cutoff = datetime.now(KST) - timedelta(hours=hours)

    results, errors, dropped = [], [], 0
    with requests.Session() as s:
        for paper in cfg["papers"]:
            name, sites = paper["name"], paper["sites"]
            got = []
            for group in cfg["keyword_groups"]:
                q = "(" + " OR ".join(f'"{k}"' if " " in k else k for k in group) + ") " + \
                    " OR ".join(f"site:{d}" for d in sites)
                try:
                    for a in fetch(q, window, s):
                        if a["pubDate"] < cutoff or not a["title"]:
                            continue
                        if not from_site(a, sites):
                            dropped += 1
                            continue
                        if any(p.search(a["title"]) for p in excl):
                            dropped += 1
                            continue
                        hit = next((r for r in got if similar(r["title"], a["title"]) >= 0.65), None)
                        if hit:
                            for k in group:
                                if k in a["title"] and k not in hit["queries"]:
                                    hit["queries"].append(k)
                            if a["source"] == name and hit["source"] != name:   # 같은 기사면 자매지보다 본지 우선
                                hit.update({k: a[k] for k in ("title", "link", "source", "source_url", "pubDate")})
                            continue
                        a["queries"] = [k for k in kws if k in a["title"]] or [group[0]]
                        got.append(a)
                except Exception as e:
                    errors.append({"query": f"{name} ({'/'.join(group[:2])}…)", "error": str(e)[:150]})
                    print(f"[WARN] {name} {group[:2]}: {e}", file=sys.stderr)
                time.sleep(0.6)
            got.sort(key=lambda a: a["pubDate"], reverse=True)
            for a in got[: int(cfg.get("max_per_paper", 40))]:
                results.append({"title": a["title"], "link": a["link"], "originallink": a["link"],
                                "summary": "", "pubDate": a["pubDate"].isoformat(),
                                "category": name, "press": name, "source": a["source"] or name,
                                "queries": a["queries"], "source_type": "major",
                                "score": min(len(a["queries"]), 5) + 3})
            print(f"{name}: {len(got)}건")

    results.sort(key=lambda a: (a["score"], a["pubDate"]), reverse=True)
    out = {"generated_at": datetime.now(KST).isoformat(), "window_hours": hours,
           "count": len(results), "filtered_out": dropped,
           "category_order": [p["name"] for p in cfg["papers"]],
           "errors": errors, "items": results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(results)}건 저장 → {args.out} (제외 {dropped}건, 오류 {len(errors)}건)")


if __name__ == "__main__":
    main()
