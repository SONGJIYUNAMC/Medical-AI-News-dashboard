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
  - 제목에 AI 용어(AI·인공지능·딥러닝 등)가 있으면 +3
  - 유사 기사 가점: 비슷한 제목의 기사가 여러 매체에서 보도되면 +1씩 (최대 +3)
정렬은 score 내림차순, 같으면 최신순.

유사 기사 정리: 제목이 비슷한 기사는 하나로 묶고, 주요 일간지·의료 전문지 기사를 대표로 남김
  (해당 매체가 없으면 점수가 높고 먼저 나온 기사). 묶인 나머지는 similar_count 로만 표시.
제외 주제: exclude_topics (제약사 신약개발 등)는 제목에 있거나 요약에 2개 이상 있으면 제외.
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
from urllib.parse import urlparse

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
        # 게이트: 그룹마다 하나 이상 일치해야 통과 (예: AI 용어 그룹 AND 의료 용어 그룹)
        # (패턴, 제목에서만 찾을지) — title_required 에 적힌 그룹은 제목에 있어야 통과
        "gate": [(rx(g), name in cfg.get("title_required", []), cfg.get("summary_min_hits", 2))
                 for name, g in cfg.get("require_all", {}).items()]
                + ([(rx(cfg["require_any"]), False, 1)] if cfg.get("require_any") else []),
        # 종합계획·지자체장 기사: 제목에 의료 용어가 없으면 제외 (의료는 일부로만 언급되는 경우)
        "broad": rx(cfg.get("broad_plan", [])),
        "med_title": [p for name, g in cfg.get("require_all", {}).items()
                      if name in cfg.get("title_required", []) for p in rx(g)],
        "exclude_any": rx(cfg.get("exclude_any", [])),
        "ai_title": rx(next(iter(cfg.get("require_all", {}).values()), [])),  # 첫 그룹 = AI 용어
        "exclude": [w.lower() for w in cfg.get("exclude_words", [])],
        "off_topic": rx(cfg.get("exclude_topics", [])),
        "sources": {k: [d.lower() for d in v] for k, v in cfg.get("preferred_sources", {}).items()},
        "sim": float(cfg.get("dedupe_similarity", 0.4)),
        "assist": tuple(cfg.get("dedupe_assist", [0.25, 0.3])),
        "anchor_min": float(cfg.get("dedupe_anchor_min", 0.09)),
        "anchor_generic": set(cfg.get("dedupe_anchor_generic", ["AI", "IT", "LLM", "EMR", "CDSS", "FDA", "NHS", "CMS", "CT", "MRI", "ICU", "ER", "PC", "DB", "OS", "UI", "AR", "VR", "ICT", "ESG", "IPO", "CEO", "KPI", "API"])),
    }


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def source_type(url: str, cc: dict) -> str:
    """주요 일간지(major) / 의료 전문지(medical) / 기타(other)."""
    h = host_of(url)
    for kind, domains in cc["sources"].items():
        if any(h == d or h.endswith("." + d) for d in domains):
            return kind
    return "other"


def passes_gate(art: dict, cc: dict) -> bool:
    """AI 용어 그룹과 의료 용어 그룹을 모두 만족해야 통과 (의료 용어는 제목에 있어야 함)."""
    t, s = art["title"], art["summary"]
    for pats, title_only, min_hits in cc["gate"]:
        if any(p.search(t) for p in pats):
            continue
        if title_only:  # 제목에 없으면 요약에서 서로 다른 용어가 min_hits개 이상 있어야 통과
            if sum(1 for p in pats if p.search(s)) >= min_hits:
                continue
            return False
        if not any(p.search(s) for p in pats):
            return False
    if any(p.search(t) for p in cc["broad"]) and not any(p.search(t) for p in cc["med_title"]):
        return False
    return True


def is_off_topic(art: dict, cc: dict) -> bool:
    """제목에 제외 주제가 있거나, 요약에 서로 다른 제외 패턴이 2개 이상이면 True."""
    pats = cc["off_topic"]
    if not pats:
        return False
    if any(p.search(art["title"]) for p in pats):
        return True
    return sum(1 for p in pats if p.search(art["summary"])) >= 2


def _title_grams(title: str) -> set:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", "", title)
    t = re.sub(r"[^0-9a-zA-Z가-힣]", "", t).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else {t}


_ACR = re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])")
_EVW = re.compile(r"세미나|포럼|컨퍼런스|콘퍼런스|심포지엄|워크숍|웨비나|설명회|간담회|발표회|학술대회|토론회|개최")
_EVT = re.compile(r"([가-힣]{2,6})\s?(선언|협약|결의안?|헌장)")   # '서울선언', '서울 선언' 같은 행사·문서 이름


def anchors_of(title: str, generic: set) -> set:
    a = {x for x in _ACR.findall(title) if x not in generic}
    a |= {m.group(1) + m.group(2) for m in _EVT.finditer(title)}
    return a


def _source_rank(url: str, cc: dict) -> int:
    """대표 기사 선정용 순위: 설정 목록(preferred_sources)의 앞쪽 매체일수록 작은 값."""
    h = host_of(url)
    base = 0
    for kind in ("major", "medical"):
        doms = cc["sources"].get(kind, [])
        for k, d in enumerate(doms):
            if h == d or h.endswith("." + d):
                return base + k
        base += 100
    return 999


def group_similar(results: list, cc: dict) -> list:
    """제목이 비슷한 기사끼리 묶어 인덱스 그룹 목록을 돌려준다."""
    n = len(results)
    grams = [_title_grams(a["title"]) for a in results]
    sgrams = [_title_grams(a["summary"][:100]) for a in results]
    anchors = [anchors_of(a_["title"], cc["anchor_generic"]) for a_ in results]
    t_min, s_min = cc["assist"]  # 제목이 조금 달라도 요약이 거의 같으면 같은 기사로 본다
    def same(i, j):
        gi, gj = grams[i], grams[j]
        if not gi or not gj:
            return False
        inter = len(gi & gj)
        sim = inter / (len(gi) + len(gj) - inter)
        shared = anchors[i] & anchors[j]
        diff_org = bool(anchors[i] and anchors[j] and not shared)   # 서로 다른 기관(KT vs LG)
        if sim >= cc["sim"] and not (diff_org and sim < 0.6):
            return True
        if shared and _EVW.search(results[i]["title"]) and _EVW.search(results[j]["title"]):
            return True   # 같은 기관(KT 등)의 세미나·포럼 개최 기사는 한 사안
        if any(len(x) >= 3 for x in shared):
            evt = any(x[-2:] in ("선언", "협약", "헌장") or x.endswith(("결의", "결의안")) for x in shared if len(x) >= 4)
            if sim >= cc["anchor_min"] * (0.3 if evt else 1):
                return True   # 같은 기관(WHO 등) 약어·같은 선언문을 제목에 공유하고 제목도 조금 겹치면 같은 사안
        if sim >= t_min:
            si, sj = sgrams[i], sgrams[j]
            return bool(si and sj and len(si & sj) / len(si | sj) >= s_min)
        return False

    # 대표 기사(앞쪽 매체·높은 관련도)부터 차례로 군집을 만들고, 각 군집의 대표와만 비교한다(연쇄 병합 방지)
    order = sorted(range(n), key=lambda i: (_source_rank(results[i].get("originallink") or "", cc),
                                           -results[i].get("score", 0), results[i].get("pubDate", "")))
    clusters = []
    for i in order:
        for c in clusters:
            if same(c[0], i) or any(anchors[m] & anchors[i] and same(m, i) for m in c[1:]):
                c.append(i)
                break
        else:
            clusters.append([i])
    return clusters


def dedupe_similar(results: list, cc: dict) -> list:
    """제목이 비슷한 기사를 묶어 대표 1건만 남긴다 (바이그램 자카드 유사도)."""
    kept = []
    for idxs in group_similar(results, cc):
        members = [results[i] for i in idxs]
        for m in members:
            m["source_type"] = source_type(m["originallink"], cc)
        # 대표 선정: 설정 목록 순서상 앞선 주요 일간지 → 의료 전문지 → 기타, 같은 매체면 관련도 높은 순 → 먼저 나온 순
        members.sort(key=lambda m: (_source_rank(m["originallink"], cc), -m["score"], m["pubDate"]))
        rep, others = members[0], members[1:]
        rep["similar_count"] = len(others)
        rep["similar_sources"] = sorted({host_of(o["originallink"]) for o in others})[:5]
        rep["score"] += min(len(others), 3)
        kept.append(rep)
    return kept


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
    ai_in_title = 3 if any(p.search(t) for p in cc["ai_title"]) else 0  # 제목에 AI 용어가 있어야 핵심 기사
    art["category"] = category
    art["score"] = ts.get(category, 0) + phrase + multi + ai_in_title


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
    errors, dropped, dropped_topic = [], 0, 0
    with requests.Session() as s:
        for q in cc["query_cat"]:  # 검색어는 중복 없이 1회씩
            try:
                for art in fetch(q, cutoff, s, headers):
                    full = art["title"] + " " + art["summary"]
                    if any(w in full.lower() for w in cc["exclude"]):
                        continue
                    # 네이버는 단어를 따로 매칭하므로, AI 용어와 의료 용어가 모두 있어야 통과
                    if cc["gate"] and not passes_gate(art, cc):
                        dropped += 1
                        continue
                    # 건강관리·보험사·마이크로바이옴 등 업무와 무관한 헬스케어 기사
                    if any(r.search(full) for r in cc["exclude_any"]):
                        dropped_topic += 1
                        continue
                    if is_off_topic(art, cc):  # 제약사 신약개발 등 업무와 무관한 주제
                        dropped_topic += 1
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
    before = len(results)
    results = dedupe_similar(results, cc)  # 비슷한 제목 기사는 대표 1건만
    merged = before - len(results)
    # 관련도 내림차순, 같으면 최신순 (pubDate 는 모두 +09:00 ISO 문자열이라 문자열 비교 가능)
    results.sort(key=lambda a: (a["score"], a["pubDate"]), reverse=True)

    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "window_hours": args.hours,
        "count": len(results),
        "filtered_out": dropped,
        "filtered_off_topic": dropped_topic,
        "merged_similar": merged,
        "category_order": cc["order"],
        "errors": errors,
        "items": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    per_cat = {c: sum(1 for a in results if a["category"] == c) for c in cc["order"]}
    print(f"{len(results)}건 저장 → {args.out} (필터 제외 {dropped}건, 제외 주제 {dropped_topic}건, "
          f"유사 기사 정리 {merged}건, 오류 {len(errors)}건)")
    print(per_cat)


if __name__ == "__main__":
    main()
