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


def jloads(txt):
    """게시판 본문에 줄바꿈·탭이 그대로 들어 있는 경우가 많아 strict=False 로 읽는다."""
    return json.loads(txt, strict=False)


def pick_list(obj, key):
    """JSON 문서에서 게시글 목록(사전들의 배열)을 찾는다."""
    if isinstance(obj, list):
        return obj if any(isinstance(x, dict) for x in obj) else None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get(key), list):
        return obj[key]
    for v in obj.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    for v in obj.values():                      # 한 단계 더 들어가서 찾기
        if isinstance(v, dict):
            got = pick_list(v, key)
            if got:
                return got
    return None


def json_after(text, key):
    """HTML 안에 박혀 있는 "key": [ ... ] 형태의 JSON 배열을 괄호 짝을 세며 뽑아낸다."""
    for m in re.finditer(r'[\'"]?' + re.escape(key) + r'[\'"]?\s*[:=]\s*\[', text):
        i = text.index("[", m.end() - 1)
        depth, instr, esc = 0, False, False
        for j in range(i, min(len(text), i + 4_000_000)):
            c = text[j]
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
                        return jloads(text[i:j + 1])
                    except Exception:
                        break
    return None


def row_from_dict(d, f):
    k_id, k_ti = f.get("id", "num"), f.get("title", "title")
    k_dt, k_bd = f.get("date", "write_dt"), f.get("body", "cont")
    ti = strip(str(d.get(k_ti) or ""))
    if not ti:
        for alt in ("title", "subject", "sj", "nttSj", "bbsTtl"):
            ti = strip(str(d.get(alt) or ""))
            if ti:
                break
    if not ti:
        return None
    dt = str(d.get(k_dt) or "")
    if not dt:
        for alt in ("write_dt", "reg_dt", "regDate", "frstRegistPnttm", "ntcDt", "date"):
            if d.get(alt):
                dt = str(d[alt])
                break
    rid = d.get(k_id)
    if rid in (None, ""):
        for alt in ("num", "seq", "nttId", "bbsId", "id", "idx"):
            if d.get(alt) not in (None, ""):
                rid = d[alt]
                break
    link = d.get("link") or d.get("url") or ""
    return {"id": str(rid or ""), "title": ti, "date": dt[:10].replace(".", "-").replace("/", "-"),
            "body": strip(str(d.get(k_bd) or ""))[:300], "href": link if isinstance(link, str) else ""}


def board_rows(text, board):
    """게시판 목록 파싱: ① JSON 응답 ② 페이지에 박힌 JSON 배열 ③ 링크·날짜 패턴 ④ 표/목록 행 단위."""
    f, key = board.get("fields", {}), board.get("json_key", "brdList")
    head = text.lstrip()[:1]
    if head in ("{", "["):                      # ① 응답 전체가 JSON
        try:
            got = pick_list(jloads(text), key)
            rows = [r for r in (row_from_dict(d, f) for d in got or [] if isinstance(d, dict)) if r]
            if rows:
                return rows
        except Exception:
            pass
    got = json_after(text, key)                 # ② HTML 안에 박힌 JSON
    if isinstance(got, list):
        rows = [r for r in (row_from_dict(d, f) for d in got if isinstance(d, dict)) if r]
        if rows:
            return rows
    rows = []                                   # ③ 글번호가 들어 있는 링크
    for m in re.finditer(r'<a[^>]+href="([^"]*?(?:num|seq|bltnNo|idx|nttId)=(\d+)[^"]*)"[^>]*>(.{0,300}?)</a>', text, re.S | re.I):
        ti = strip(m.group(3))
        if len(ti) < 6:
            continue
        near = text[m.end():m.end() + 500]
        dm = re.search(r"(20\d\d)[-./\s]+(\d{1,2})[-./\s]+(\d{1,2})", near) or re.search(r"(20\d\d)[-./\s]+(\d{1,2})[-./\s]+(\d{1,2})", ti)
        rows.append({"id": m.group(2), "href": unescape(m.group(1)), "title": ti,
                     "date": f"{dm[1]}-{int(dm[2]):02d}-{int(dm[3]):02d}" if dm else "", "body": ""})
    if rows:
        return rows
    for blk in re.findall(r"<(?:tr|li)\b.*?</(?:tr|li)>", text, re.S | re.I):   # ④ 표·목록 행 (javascript 링크 포함)
        dm = re.search(r"(20\d\d)[-./]\s?(\d{1,2})[-./]\s?(\d{1,2})", blk)
        if not dm:
            continue
        am = re.search(r"<a\b[^>]*>(.*?)</a>", blk, re.S | re.I)
        ti = strip(am.group(1)) if am else ""
        if len(ti) < 6:
            cells = [strip(c) for c in re.findall(r"<t[dh]\b.*?</t[dh]>", blk, re.S | re.I)]
            ti = max(cells, key=len) if cells else strip(blk)
        if len(ti) < 6:
            continue
        idm = re.search(r"(?:num|seq|nttId|idx|bltnNo)[=:'\"\s(]+(\d+)", blk) or re.search(r"\((\d{2,})[,)]", blk)
        hm = re.search(r'href="(?!#|javascript)([^"]+)"', blk, re.I)
        rows.append({"id": idm.group(1) if idm else "", "href": unescape(hm.group(1)) if hm else "",
                     "title": ti, "date": f"{dm[1]}-{int(dm[2]):02d}-{int(dm[3]):02d}", "body": ""})
    return rows


def board_link(board, row, session, state):
    """상세 페이지 주소 확인(없거나 열리지 않으면 목록 주소 사용)."""
    if row.get("href") and not row["href"].startswith(("#", "javascript")):
        return requests.compat.urljoin(board["url"], row["href"])
    tpls = board.get("detail_url") or []
    if isinstance(tpls, str):
        tpls = [tpls]
    if state.get("tpl"):
        return state["tpl"].format(id=row["id"])
    if not row.get("id") or state.get("checks", 0) >= 5:
        return board["url"]
    core = re.sub(r"^\(?['\u2019]?\d{2}[.\s]*\d{1,2}[.\s]*\d{1,2}\)?\s*", "", row["title"])   # 제목 앞 ('26. 11. 13) 같은 날짜 표기 제거
    needle = re.sub(r"\W+", "", core)[:12]
    for t in tpls:
        u = t.format(id=row["id"])
        try:
            state["checks"] = state.get("checks", 0) + 1
            r = session.get(u, headers=UA, timeout=20)
            if r.ok and needle and needle in re.sub(r"\W+", "", unescape(board_text(r))):
                state["tpl"] = t
                return u
        except Exception:
            pass
    return board["url"]


def board_text(r):
    """한글이 깨지지 않도록 본문을 디코딩한다."""
    txt = r.text
    if "\ufffd" in txt[:3000] or (r.encoding or "").lower() in ("iso-8859-1", "ascii"):
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return r.content.decode(enc)
            except Exception:
                continue
    return txt


def fetch_board(board, cutoff, session):
    base = "{0.scheme}://{0.netloc}/".format(requests.compat.urlparse(board["url"]))
    tries = [{**UA, "Accept": "application/json, text/javascript, text/html;q=0.9, */*;q=0.8",
              "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8", "Referer": base, "X-Requested-With": "XMLHttpRequest"},
             {**UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "ko-KR,ko;q=0.9", "Referer": base}]
    last = None
    for h in tries:
        try:
            r = session.get(board["url"], headers=h, timeout=30)
        except Exception as e:
            last = ("요청 실패", clean_err(e), "", 0)
            continue
        txt = board_text(r) if r.ok else ""
        rows = board_rows(txt, board) if txt else []
        if rows:
            out, state = [], {}
            for row in rows[: int(board.get("max_items", 40))]:
                d = parse_date(row["date"]) if row.get("date") else None
                if d is None or d < cutoff:
                    continue
                out.append({"title": row["title"], "link": board_link(board, row, session, state),
                            "summary": row.get("body", ""), "pubDate": d})
            return out
        last = (f"HTTP {r.status_code}", r.headers.get("Content-Type", "?"), re.sub(r"\s+", " ", txt[:100]), len(txt))
    raise RuntimeError("게시판 목록을 읽지 못했습니다 [{0} / {1} / {3}자 / 시작: {2}]".format(*(last or ("?", "?", "", 0))))


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
