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
from urllib.parse import quote, urlparse

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
               "summary": strip(it.findtext("description"))[:300], "pubDate": pub,
               "source_url": (it.find("source").get("url") if it.find("source") is not None else "") or ""}


def is_official(url: str) -> bool:
    """공식 출처 도메인(정부·공공기관 .go.kr, 정책브리핑 korea.kr)인지."""
    h = (urlparse(url).hostname or "").lower()
    return h == "korea.kr" or h.endswith(".korea.kr") or h == "go.kr" or h.endswith(".go.kr")


def json_after(html, key):
    """HTML 안에 박혀 있는 "key": [ ... ] 형태의 JSON 배열을 괄호 짝을 세며 뽑아낸다."""
    for m in re.finditer(r'[\'"]?' + re.escape(key) + r'[\'"]?\s*[:=]\s*\[', html):
        i = html.index("[", m.end() - 1)
        depth, instr, esc = 0, False, False
        for j in range(i, min(len(html), i + 4_000_000)):
            c = html[j]
            if esc:
                esc = False
                continue
            if instr:
                if c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
                continue
            if c == '"':
                instr = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[i:j + 1])
                    except Exception:
                        break
    return None


def board_rows(html, board):
    """게시판 목록 파싱: ① 페이지에 들어 있는 JSON 배열 ② 안 되면 링크·날짜 패턴으로 직접 추출."""
    f = board.get("fields", {})
    k_id, k_ti, k_dt, k_bd = f.get("id", "num"), f.get("title", "title"), f.get("date", "write_dt"), f.get("body", "cont")
    rows = []
    data = json_after(html, board.get("json_key", "brdList"))
    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            ti = strip(str(d.get(k_ti) or ""))
            if not ti:
                continue
            rows.append({"id": str(d.get(k_id) or ""), "title": ti,
                         "date": str(d.get(k_dt) or "")[:10], "body": strip(str(d.get(k_bd) or ""))[:300]})
        if rows:
            return rows
    for m in re.finditer(r'<a[^>]+href="([^"]*?(?:num|seq|bltnNo|idx|nttId)=(\d+)[^"]*)"[^>]*>(.{0,300}?)</a>', html, re.S | re.I):
        ti = strip(m.group(3))
        if len(ti) < 6:
            continue
        near = html[m.end():m.end() + 500]
        dm = re.search(r"(20\d\d)[-./\s]+(\d{1,2})[-./\s]+(\d{1,2})", near) or re.search(r"(20\d\d)[-./\s]+(\d{1,2})[-./\s]+(\d{1,2})", ti)
        rows.append({"id": m.group(2), "href": unescape(m.group(1)), "title": ti,
                     "date": f"{dm[1]}-{int(dm[2]):02d}-{int(dm[3]):02d}" if dm else "", "body": ""})
    return rows


def board_link(board, row, session, state):
    """상세 페이지 주소 확인(없거나 열리지 않으면 목록 주소 사용)."""
    if row.get("href"):
        return requests.compat.urljoin(board["url"], row["href"])
    tpls = board.get("detail_url") or []
    if isinstance(tpls, str):
        tpls = [tpls]
    if state.get("tpl"):
        return state["tpl"].format(id=row["id"])
    if not row.get("id") or state.get("checks", 0) >= 5:
        return board["url"]
    for t in tpls:
        u = t.format(id=row["id"])
        try:
            state["checks"] = state.get("checks", 0) + 1
            r = session.get(u, headers=UA, timeout=20)
            if r.ok and (row["title"][:10] in r.text or row["title"][:10] in unescape(r.text)):
                state["tpl"] = t
                return u
        except Exception:
            pass
    return board["url"]


def fetch_board(board, cutoff, session):
    r = session.get(board["url"], headers={**UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}, timeout=30)
    r.raise_for_status()
    rows = board_rows(r.text, board)
    if not rows:
        raise RuntimeError("게시판 목록을 읽지 못했습니다 - 주소·구조 확인 필요")
    out, state = [], {}
    for row in rows[: int(board.get("max_items", 40))]:
        d = parse_date(row["date"]) if row.get("date") else None
        if d is None or d < cutoff:
            continue
        out.append({"title": row["title"], "link": board_link(board, row, session, state),
                    "summary": row.get("body", ""), "pubDate": d})
    return out


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
        if mode == "gnews":
            if not is_official(a.get("source_url", "")):  # 언론사 기사(검색 결과)는 제외 — 정부·기관이 직접 낸 자료만
                dropped += 1
                return
            a["title"] = re.sub(r"\s-\s[^-]+$", "", a["title"])  # 제목 끝 '- 매체명' 제거
        if not any(p.search(text) for p in ai) or not (hc or any(p.search(text) for p in health) or any(p.search(text) for p in policy)):
            dropped += 1
            return
        key = re.sub(r"\W+", "", a["title"])
        if key in seen:
            return
        seen.add(key)
        items.append({"title": a["title"], "link": a["link"], "summary": a["summary"],
                      "pubDate": a["pubDate"].isoformat(), "agency": agency,
                      "kind": kind_of(text, cfg["kinds"]), "via": mode, "official": True})

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

        # 2) 게시판(HTML) 직접 수집 — RSS 가 없는 기관
        for bd in cfg.get("boards", []):
            label = bd["agency"] + (" " + bd["board"] if bd.get("board") else "")
            try:
                got = fetch_board(bd, cutoff, s)
                for a in got:
                    add(a, bd["agency"], bd.get("health_context", False), "board")
            except Exception as e:
                errors.append({"query": f"{label} ({bd['url']})", "error": clean_err(e)[:120] + (" → 사이트 검색으로 대체" if bd.get("fallback_site") else "")})
                print(f"[WARN] {label}: {clean_err(e)}", file=sys.stderr)
                if bd.get("fallback_site"):
                    for q in (f"site:{bd['fallback_site']} 인공지능", f"site:{bd['fallback_site']} AI"):
                        try:
                            for a in parse_items(get_xml(gnews_url(q), s)):
                                add(a, bd["agency"], bd.get("health_context", False), "gnews")
                        except Exception:
                            pass
            time.sleep(0.5)

        # 3) RSS + 4) Google News 검색
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
