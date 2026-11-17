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


def txt_of(r):
    """한글이 깨지지 않도록 디코딩."""
    if r.encoding and r.encoding.lower() not in ("iso-8859-1", "ascii"):
        return r.text
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return r.content.decode(enc)
        except Exception:
            continue
    return r.text


def tag(el):
    return el.tag.split("}")[-1].lower()


def find1(node, names):
    for ch in node:
        if tag(ch) in names:
            return ch
    return None


def to_dt(val):
    """RSS(RFC822)·ISO·'YYYY-MM-DD HH:MM:SS' 등 날짜 표기를 KST 로 변환."""
    val = (val or "").strip()
    if not val:
        return None
    try:
        return parsedate_to_datetime(val).astimezone(KST)
    except Exception:
        pass
    v = val.replace("Z", "+00:00").replace("/", "-")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            d = datetime.fromisoformat(v) if fmt is None else datetime.strptime(v, fmt)
            return (d if d.tzinfo else d.replace(tzinfo=KST)).astimezone(KST)
        except Exception:
            continue
    return None


def fetch_feed(url, session):
    """신문사 RSS/Atom(또는 JSON) 피드에서 기사 목록을 읽는다."""
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
         "Accept": "application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
         "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://www.donga.com/"
         if "donga" in url else "/".join(url.split("/")[:3]) + "/"}
    last = None
    for attempt in range(3):                        # 일부 신문사 서버가 첫 요청을 끊는 경우가 있어 재시도
        try:
            r = session.get(url, headers=h, timeout=30)
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last
    body = txt_of(r).lstrip("\ufeff \r\n\t")
    out = []
    if body[:1] in ("{", "["):                      # JSON 형태 피드
        data = json.loads(body)
        rows = data if isinstance(data, list) else next(
            (v for v in (data or {}).values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
        for d in rows:
            ti = unescape(str(d.get("title") or d.get("subject") or "")).strip()
            ln = str(d.get("link") or d.get("url") or d.get("articleUrl") or "")
            dt = to_dt(str(d.get("pubDate") or d.get("date") or d.get("publishedAt") or d.get("service_day") or ""))
            if ti and ln and dt:
                out.append({"title": ti, "link": ln, "pubDate": dt,
                            "summary": re.sub(r"<[^>]+>", " ", unescape(str(d.get("description") or d.get("summary") or "")))[:300]})
        return out
    root = ET.fromstring(body.encode("utf-8"))
    for node in root.iter():
        if tag(node) not in ("item", "entry"):
            continue
        t_el, l_el = find1(node, {"title"}), find1(node, {"link"})
        ti = unescape((t_el.text or "").strip()) if t_el is not None else ""
        ln = ""
        if l_el is not None:
            ln = (l_el.get("href") or l_el.text or "").strip()
        if not ln:
            g = find1(node, {"guid", "id"})
            ln = (g.text or "").strip() if g is not None and (g.text or "").startswith("http") else ""
        d_el = find1(node, {"pubdate", "published", "updated", "date", "dc:date"})
        dt = to_dt(d_el.text if d_el is not None else "")
        s_el = find1(node, {"description", "summary", "content"})
        sm = re.sub(r"<[^>]+>", " ", unescape((s_el.text or ""))) if s_el is not None and s_el.text else ""
        if ti and ln and dt:
            out.append({"title": ti, "link": ln, "pubDate": dt, "summary": re.sub(r"\s+", " ", sm).strip()[:300]})
    return out


def fetch(query, window, session):
    url = (f"https://news.google.com/rss/search?q={quote(query + f' when:{window}')}"
           f"&hl=ko&gl=KR&ceid=KR:ko")
    for attempt in range(4):
        r = session.get(url, headers=UA, timeout=30)
        if r.status_code >= 500 or r.status_code == 429:   # 구글 뉴스 과다요청 제한: 점점 길게 쉬고 재시도
            time.sleep(4 * (attempt + 1))
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


def kw_hits(title, strong, weak):
    """제목에서 키워드를 찾는다. AI·인공지능(weak)만 걸린 기사는 의료·정부 키워드(strong)가 함께 있어야 수집."""
    s_hit = [k for k in strong if k in title]
    if not s_hit:
        return []
    return s_hit + [k for k in weak if k in title]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="press_sources.json")
    ap.add_argument("--out", default="data/press_news.json")
    ap.add_argument("--hours", type=int, default=0)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    hours = args.hours or int(cfg.get("hours", 48))
    # 제외어는 정규식이 아니라 그대로의 낱말로 본다([인사] 같은 표기가 문자열 집합으로 해석되지 않도록)
    window, excl = cfg.get("window", "2d"), [re.compile(re.escape(w), re.I) for w in cfg.get("exclude", [])]
    strong = cfg.get("keywords_strong") or cfg.get("keywords", [])
    weak = cfg.get("keywords_weak", [])
    cutoff = datetime.now(KST) - timedelta(hours=hours)

    results, errors, dropped = [], [], 0
    with requests.Session() as s:
        for paper in cfg["papers"]:
            name, sites = paper["name"], paper.get("sites", [])
            got, feed_ok = [], False
            for url in paper.get("feeds", []):          # ① 신문사 RSS
                try:
                    items = fetch_feed(url, s)
                    feed_ok = True
                    for a in items:
                        if a["pubDate"] < cutoff:
                            continue
                        hits = kw_hits(a["title"], strong, weak)   # 제목 기준, AI 단독 기사는 제외
                        if not hits:
                            dropped += 1
                            continue
                        if any(p.search(a["title"]) for p in excl):
                            dropped += 1
                            continue
                        if any(r["link"] == a["link"] for r in got) or \
                           next((r for r in got if similar(r["title"], a["title"]) >= 0.65), None):
                            continue
                        got.append({**a, "source": name, "source_url": a["link"], "queries": hits[:6]})
                except Exception as e:
                    errors.append({"query": f"{name} RSS {url.split('/')[-1]}",
                                   "error": str(e)[:120] + " → 구글 뉴스 검색으로 대체"})
                    print(f"[WARN] {name} {url}: {e}", file=sys.stderr)
                time.sleep(0.4)
            if feed_ok and got:
                got.sort(key=lambda a: a["pubDate"], reverse=True)
                for a in got[: int(cfg.get("max_per_paper", 40))]:
                    results.append({"title": a["title"], "link": a["link"], "originallink": a["link"],
                                    "summary": a.get("summary", ""), "pubDate": a["pubDate"].isoformat(),
                                    "category": name, "press": name, "source": name,
                                    "queries": a["queries"], "source_type": "major",
                                    "score": min(len(a["queries"]), 5) + 3})
                print(f"{name}: {len(got)}건 (RSS)")
                continue
            for group in cfg["keyword_groups"]:         # ② RSS 가 없거나 실패하면 Google News site: 검색
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
                        hits = kw_hits(a["title"], strong, weak)
                        if not hits:
                            dropped += 1
                            continue
                        a["queries"] = hits[:6]
                        got.append(a)
                except Exception as e:
                    errors.append({"query": f"{name} ({'/'.join(group[:2])}…)", "error": str(e)[:150]})
                    print(f"[WARN] {name} {group[:2]}: {e}", file=sys.stderr)
                time.sleep(1.5)
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
