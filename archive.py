#!/usr/bin/env python3
"""매일 수집 결과(news/global_news/gov_news.json)를 주 단위 파일(data/weeks/YYYY-Www.json)에 누적하고
data/weeks/index.json(주별 요약 목록)을 갱신한다. 주는 월요일 시작(ISO 주차, KST 기준).
사용: python archive.py            (수집 스크립트 실행 후)
"""
import json, re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tracked
    TRACKED = tracked.load("keywords.json")
except Exception:  # keywords.json 이 없으면 키워드 집계 생략
    TRACKED = []

KST = timezone(timedelta(hours=9))
DATA = Path("data")
WEEKS = DATA / "weeks"


def load(name):
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def week_info(d):
    y, w, _ = d.isocalendar()
    start = d - timedelta(days=d.weekday())
    return f"{y}-W{w:02d}", start, start + timedelta(days=6)


def norm(t):
    return re.sub(r"[^0-9a-z가-힣]+", "", (t or "").lower())


def keys(it):
    return {f"{it['region']}|{it.get('originallink') or it.get('link')}", f"{it['region']}|t|{norm(it['title'])}"}


def main():
    now = datetime.now(KST)
    today = now.date()
    wk, start, end = week_info(today)
    prev_wk = week_info(today - timedelta(days=7))[0]
    today_s = today.isoformat()

    incoming, order, runinfo = [], [], {}
    kr, gl, gv = load("news.json"), load("global_news.json"), load("gov_news.json")
    if kr:
        for it in kr.get("items", []):
            incoming.append({**it, "region": "국내"})
        order += kr.get("category_order", [])
        runinfo["국내"] = {"generated_at": kr.get("generated_at"), "errors": [e["query"] for e in kr.get("errors", [])]}
    if gl:
        for it in gl.get("items", []):
            incoming.append({**it, "region": "해외", "summary": "", "originallink": it.get("link"),
                             "source_type": "global_pref" if it.get("preferred") else ""})
        order += gl.get("category_order", [])
        runinfo["해외"] = {"generated_at": gl.get("generated_at"), "errors": [e["query"] for e in gl.get("errors", [])]}
    if gv:
        for it in gv.get("items", []):
            incoming.append({**it, "region": "정부", "category": it.get("agency"), "originallink": it.get("link"),
                             "score": 0, "queries": [it.get("kind")], "source_type": ""})
        order += gv.get("category_order", [])
        runinfo["정부"] = {"generated_at": gv.get("generated_at"), "errors": [e["query"].split(" (")[0] + " - " + e.get("error", "") for e in gv.get("errors", [])]}

    WEEKS.mkdir(parents=True, exist_ok=True)
    path = WEEKS / f"{wk}.json"
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"week": wk, "items": [], "category_order": []}
    prev_path = WEEKS / f"{prev_wk}.json"
    prev_keys = set()
    if prev_path.exists():
        for it in json.loads(prev_path.read_text(encoding="utf-8")).get("items", []):
            prev_keys |= keys(it)

    index = {}
    for it in cur["items"]:
        for k in keys(it):
            index[k] = it
    added = updated = 0
    for it in incoming:
        ks = keys(it)
        hit = next((index[k] for k in ks if k in index), None)
        if hit:  # 이미 있는 기사: 점수·검색어만 갱신, 최초 수집일 유지
            hit["score"] = max(hit.get("score", 0), it.get("score", 0))
            hit["queries"] = list(dict.fromkeys((hit.get("queries") or []) + (it.get("queries") or [])))
            hit["similar_count"] = max(hit.get("similar_count", 0), it.get("similar_count", 0))
            updated += 1
        elif ks & prev_keys:  # 지난주 파일에 이미 있는 기사(주 경계 겹침)
            continue
        else:
            it["first_seen"] = today_s
            cur["items"].append(it)
            for k in ks:
                index[k] = it
            added += 1

    if TRACKED:  # 설정 키워드(keywords.json terms) 일치 목록을 기사마다 저장 → 키워드 집계용
        for it in cur["items"]:
            it["kw"] = tracked.hits(it["title"], it.get("summary", ""), TRACKED)

    cur["week"], cur["start"], cur["end"] = wk, start.isoformat(), end.isoformat()
    cur["updated_at"] = now.isoformat()
    cur["category_order"] = list(dict.fromkeys(cur.get("category_order", []) + order))
    cur["count"] = len(cur["items"])
    cur["items"].sort(key=lambda a: (a.get("score", 0), a.get("pubDate", "")), reverse=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")

    weeks = []
    for p in sorted(WEEKS.glob("[0-9][0-9][0-9][0-9]-W[0-9][0-9].json")):
        w = json.loads(p.read_text(encoding="utf-8"))
        its = w.get("items", [])
        weeks.append({"week": w["week"], "start": w.get("start"), "end": w.get("end"), "count": len(its),
                      "by_region": dict(Counter(i["region"] for i in its)),
                      "by_category": dict(Counter(i["category"] for i in its))})
    (WEEKS / "index.json").write_text(json.dumps(
        {"generated_at": now.isoformat(), "weeks": weeks, "last_run": runinfo}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{wk}: 신규 {added}건, 갱신 {updated}건, 주 누계 {len(cur['items'])}건 / 보관 주 {len(weeks)}개")


if __name__ == "__main__":
    main()
