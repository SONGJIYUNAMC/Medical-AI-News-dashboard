#!/usr/bin/env python3
"""기사 링크 1건을 받아 다른 뉴스와 같은 형식으로 주간 파일(data/weeks/YYYY-Www.json)에 추가한다.
사용: python add_news.py --url https://… [--region 국내|해외] [--category 이름] [--title 제목]
(GitHub Actions 'add-news' 워크플로가 호출. 제목·요약·매체·발행일은 기사 페이지의 메타 태그에서 추출)
"""
import argparse, html, json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

import archive
import naver_news as nn

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; NewsMonitorBot/1.0; +https://github.com)"


def meta(doc, *names):
    for n in names:
        for pat in (
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(n)}["\']',
        ):
            m = re.search(pat, doc, re.I)
            if m and m.group(1).strip():
                return html.unescape(m.group(1)).strip()
    return ""


def parse_date(doc):
    cands = [meta(doc, "article:published_time", "og:regDate", "pubdate", "publishdate", "date", "article:published", "datePublished")]
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', doc)
    if m:
        cands.append(m.group(1))
    for c in cands:
        if not c:
            continue
        c = c.strip().replace("Z", "+00:00")
        if re.fullmatch(r"\d{14}", c):
            c = f"{c[:4]}-{c[4:6]}-{c[6:8]}T{c[8:10]}:{c[10:12]}:{c[12:14]}"
        try:
            d = datetime.fromisoformat(c.replace(" ", "T"))
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=KST)
        return d.astimezone(KST)
    return None


def parse_page(doc, final_url):
    title = meta(doc, "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    desc = meta(doc, "og:description", "description", "twitter:description")
    site = meta(doc, "og:site_name")
    canon = meta(doc, "og:url")
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', doc, re.I)
    canon = canon or (m.group(1) if m else "")
    return {"title": nn.clean(title), "summary": nn.clean(desc)[:300], "site": site,
            "date": parse_date(doc), "url": canon if canon.startswith("http") else final_url}


def fetch_page(url):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}, timeout=25, allow_redirects=True)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text, r.url


def build_item(url, region, category, title_in, doc=None, final_url=None, now=None):
    now = now or datetime.now(KST)
    info = {"title": "", "summary": "", "site": "", "date": None, "url": final_url or url}
    if doc is None:
        try:
            doc, final_url = fetch_page(url)
        except Exception as e:  # 접속 차단·유료 기사 등: 입력한 제목으로 계속 진행
            print(f"페이지를 가져오지 못함: {e}")
            doc = ""
    if doc:
        info = parse_page(doc, final_url or url)
    title = title_in or info["title"]
    if not title:
        raise SystemExit("제목을 알 수 없습니다. 제목을 함께 입력해 다시 요청하세요.")
    link = info["url"] if urlparse(info["url"]).hostname else url
    pub = (info["date"] or now).astimezone(KST).isoformat(timespec="seconds")
    item = {"title": title, "summary": info["summary"], "link": url, "originallink": link, "pubDate": pub,
            "region": region, "queries": ["직접 등록"], "manual": True, "first_seen": now.date().isoformat(), "score": 99}
    if region == "해외":
        item["sources"] = [info["site"] or nn.host_of(link)]
        item["category"] = category or "해외 직접 등록"
        item["source_type"] = ""
    else:
        cc = nn.compile_cfg(json.loads(Path("keywords.json").read_text(encoding="utf-8")))
        art = {"title": title, "summary": info["summary"], "queries": []}
        nn.classify_and_score(art, cc)
        item["category"] = category or art["category"]
        item["source_type"] = nn.source_type(link, cc)
        item["query"] = "직접 등록"
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--region", default="국내", choices=["국내", "해외"])
    ap.add_argument("--category", default="")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    if not re.match(r"^https?://", a.url, re.I):
        raise SystemExit("http(s) 주소가 아닙니다.")
    now = datetime.now(KST)
    item = build_item(a.url.strip(), a.region, a.category.strip(), a.title.strip(), now=now)
    wk, start, end = archive.week_info(now.date())
    archive.WEEKS.mkdir(parents=True, exist_ok=True)
    path = archive.WEEKS / f"{wk}.json"
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"week": wk, "items": [], "category_order": []}
    have = set()
    for it in cur["items"]:
        have |= archive.keys(it)
    if archive.keys(item) & have:
        print("이미 등록된 기사입니다.")
        return
    cur["items"].append(item)
    if item["category"] not in cur.get("category_order", []):
        cur.setdefault("category_order", []).append(item["category"])
    cur["week"], cur["start"], cur["end"] = wk, start.isoformat(), end.isoformat()
    cur["updated_at"] = now.isoformat()
    cur["count"] = len(cur["items"])
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    archive.write_index(now)
    print(f"등록 완료: [{item['region']} · {item['category']}] {item['title']}")


if __name__ == "__main__":
    main()
