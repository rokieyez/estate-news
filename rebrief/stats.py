"""정부 통계를 직접 받아온다 — 기사에 실린 숫자를 받아쓰지 않기 위해.

두 곳을 씁니다. 둘 다 무료 인증키가 필요하고, 키가 없으면 조용히 건너뜁니다.

* **아파트 매매 실거래가** — 공공데이터포털(apis.data.go.kr)의 국토교통부 자료.
  환경변수 `DATA_GO_KR_KEY`. 시군구 코드와 연월을 주면 그 달 거래를 전부 돌려줍니다.
* **한국부동산원 통계** — R-ONE 열린 자료(www.reb.or.kr). 환경변수 `REB_API_KEY`.
  통계표마다 번호(STATBL_ID)가 달라서, `python -m rebrief stats --tables 아파트` 로
  먼저 번호를 찾은 뒤 설정에 적어야 합니다.

돌려주는 값은 전부 '원문 그대로의 수치' 입니다. 해석은 붙이지 않습니다.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

DEAL_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
# 전월세는 자료가 따로라 포털에서 '아파트 전월세 실거래가' 를 한 번 더 활용신청해야 합니다.
RENT_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
REB_DATA_URL = "https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do"
REB_LIST_URL = "https://www.reb.or.kr/r-one/openapi/SttsApiTbl.do"

# 같은 자료인데 예전 판은 한글 태그, 새 판은 영문 태그를 씁니다. 둘 다 받습니다.
_FIELDS = {
    "name": ("아파트", "aptNm"),
    "amount": ("거래금액", "dealAmount"),
    "area": ("전용면적", "excluUseAr"),
    "year": ("년", "dealYear"),
    "month": ("월", "dealMonth"),
    "day": ("일", "dealDay"),
    "dong": ("법정동", "umdNm"),
    "floor": ("층", "floor"),
    "seq": ("일련번호", "aptSeq"),      # 단지 고유번호. 예전 판에는 없어 이름+동으로 대신한다
}
# 전월세 응답도 한글 판·영문 판이 섞여 있어 둘 다 읽는다.
_RENT_FIELDS = {
    "deposit": ("보증금액", "보증금", "deposit"),
    "monthly": ("월세금액", "월세", "monthlyRent"),
}


def _get(url: str, **kw):
    return requests.get(url, **kw)


def deal_key() -> str:
    """공공데이터포털 인증키.

    포털은 같은 키를 'Encoding' 과 'Decoding' 두 벌로 보여 줍니다. 우리는 요청을 보낼 때
    프로그램이 다시 인코딩하므로 **Decoding 쪽**이 맞습니다. 사용자가 Encoding 쪽을
    붙여넣어도 되도록, %2B 같은 이스케이프가 보이면 여기서 되돌립니다 — 두 번 인코딩되면
    포털이 '등록되지 않은 서비스키' 로 되돌려주는데, 원인을 찾기가 아주 어렵습니다.
    """
    raw = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if "%" in raw:
        from urllib.parse import unquote

        return unquote(raw)
    return raw


def reb_key() -> str:
    return os.environ.get("REB_API_KEY", "").strip()


def _text(item: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        found = item.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _won(amount: str) -> int:
    """'12,500' (만원) → 125000000 (원). 못 읽으면 0."""
    digits = re.sub(r"[^\d]", "", amount or "")
    return int(digits) * 10_000 if digits else 0


def parse_trades(xml_text: str) -> list[dict]:
    """실거래가 XML 을 거래 목록으로. 오류 응답이면 빈 목록."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if root.find(".//cmmMsgHeader") is not None:          # 키 오류 등
        return []
    rows = []
    for item in root.iter("item"):
        # 해제(계약 취소) 신고분은 뺀다. 넣으면 실제보다 거래가 많아 보인다.
        if (item.findtext("cdealType") or "").strip():
            continue
        amount = _won(_text(item, _FIELDS["amount"]))
        if not amount:
            continue
        try:
            area = float(_text(item, _FIELDS["area"]) or 0)
        except ValueError:
            area = 0.0
        y, m, d = (_text(item, _FIELDS[k]) for k in ("year", "month", "day"))
        name, dong = _text(item, _FIELDS["name"]), _text(item, _FIELDS["dong"])
        rows.append({
            "name": name,
            "dong": dong,
            "seq": _text(item, _FIELDS["seq"]) or f"{dong}|{name}",
            "amount": amount,
            "area": round(area, 2),
            "floor": _text(item, _FIELDS["floor"]),
            "date": f"{y}-{int(m):02d}-{int(d):02d}" if y and m and d else "",
        })
    return rows


def apt_trades(cfg, code: str, ym: str, *, rows: int = 1000) -> list[dict]:
    """한 시군구(5자리 코드)의 한 달치 아파트 매매 실거래. 키가 없으면 빈 목록."""
    key = deal_key()
    if not key:
        return []
    try:
        resp = _get(DEAL_URL, params={
            "serviceKey": key, "LAWD_CD": code, "DEAL_YMD": ym,
            "pageNo": "1", "numOfRows": str(rows),
        }, timeout=float(cfg.get("collect.timeout_seconds", 15)))
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("실거래가를 가져오지 못했습니다(%s %s): %s", code, ym, type(exc).__name__)
        return []
    return parse_trades(resp.text)


# ── 전월세 실거래와 전세가율 ─────────────────────────────────
#
# 지수로는 "전세가 매매보다 더 올랐다" 까지만 말할 수 있습니다. 실제 거래를 나란히 놓아야
# "이 단지는 매매가의 몇 %에 전세가 나간다" 를 말할 수 있습니다.


def parse_rents(xml_text: str) -> list[dict]:
    """전월세 XML 을 거래 목록으로. 월세가 0 인 것만 순수 전세로 본다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if root.find(".//cmmMsgHeader") is not None:
        return []
    rows = []
    for item in root.iter("item"):
        deposit = _won(_text(item, _RENT_FIELDS["deposit"]))
        if not deposit:
            continue
        try:
            area = float(_text(item, _FIELDS["area"]) or 0)
        except ValueError:
            area = 0.0
        name, dong = _text(item, _FIELDS["name"]), _text(item, _FIELDS["dong"])
        y, m, d = (_text(item, _FIELDS[k]) for k in ("year", "month", "day"))
        rows.append({
            "name": name, "dong": dong,
            "seq": _text(item, _FIELDS["seq"]) or f"{dong}|{name}",
            "deposit": deposit,
            "monthly": _won(_text(item, _RENT_FIELDS["monthly"])),
            "area": round(area, 2),
            "date": f"{y}-{int(m):02d}-{int(d):02d}" if y and m and d else "",
        })
    return rows


def apt_rents(cfg, code: str, ym: str, *, rows: int = 1000) -> list[dict]:
    """한 시군구의 한 달치 아파트 전월세 실거래. 키가 없거나 신청 전이면 빈 목록."""
    key = deal_key()
    if not key:
        return []
    try:
        resp = _get(RENT_URL, params={
            "serviceKey": key, "LAWD_CD": code, "DEAL_YMD": ym,
            "pageNo": "1", "numOfRows": str(rows),
        }, timeout=float(cfg.get("collect.timeout_seconds", 15)))
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("전월세 실거래를 가져오지 못했습니다(%s %s): %s", code, ym, type(exc).__name__)
        return []
    return parse_rents(resp.text)


def jeonse_ratio(trades: list[dict], rents: list[dict], *, min_pairs: int = 2) -> dict:
    """같은 단지·같은 면적 칸의 전세 보증금 ÷ 매매가.

    월세가 붙은 계약은 보증금이 낮아 섞으면 비율이 왜곡되므로 순수 전세만 씁니다.
    한쪽만 있는 칸은 셀 수 없으니 뺍니다.
    """
    sale = _by_unit(trades)
    pure = [r for r in rents if not r["monthly"]]
    lease: dict[tuple[str, int], list[dict]] = {}
    for r in pure:
        lease.setdefault((r.get("seq", ""), area_bucket(r.get("area", 0))), []).append(r)

    pairs = []
    for key, deals in sale.items():
        got = lease.get(key)
        if not got:
            continue
        sale_avg = sum(d["amount"] for d in deals) / len(deals)
        lease_avg = sum(d["deposit"] for d in got) / len(got)
        if sale_avg <= 0:
            continue
        pairs.append({
            "name": deals[0]["name"], "dong": deals[0]["dong"],
            "area": deals[0]["area"], "sale": round(sale_avg), "lease": round(lease_avg),
            "ratio": round(lease_avg / sale_avg * 100, 1),
        })
    if len(pairs) < min_pairs:
        return {}
    ratios = sorted(p["ratio"] for p in pairs)
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else round((ratios[mid - 1] + ratios[mid]) / 2, 1)
    pairs.sort(key=lambda p: p["ratio"], reverse=True)
    return {"median": median, "pairs": pairs, "count": len(pairs)}


def summarize(rows: list[dict]) -> dict:
    """거래 목록 → 건수·평균가·중간값·최고가. 비어 있으면 건수 0."""
    if not rows:
        return {"count": 0, "avg": 0, "median": 0, "top": None}
    amounts = sorted(r["amount"] for r in rows)
    mid = len(amounts) // 2
    median = amounts[mid] if len(amounts) % 2 else (amounts[mid - 1] + amounts[mid]) // 2
    top = max(rows, key=lambda r: r["amount"])
    return {"count": len(rows), "avg": sum(amounts) // len(amounts),
            "median": median, "top": top}


# ── 눈에 띄는 거래 고르기 ────────────────────────────────────
#
# 같은 단지라도 면적이 다르면 값이 딴판이라 함께 셀 수 없습니다. 전용면적을 5㎡ 칸으로
# 나눠 같은 칸끼리만 비교합니다. 지난 거래가 너무 적으면 '신고가' 라 부를 근거가 약해
# 최소 건수를 둡니다.

AREA_STEP = 5.0


def area_bucket(area: float) -> int:
    """전용면적을 5㎡ 칸으로. 84.43㎡ 과 84.99㎡ 는 같은 칸으로 본다."""
    try:
        return int(round(float(area) / AREA_STEP) * AREA_STEP)
    except (TypeError, ValueError):
        return 0


def _by_unit(rows: list[dict]) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        out.setdefault((r.get("seq", ""), area_bucket(r.get("area", 0))), []).append(r)
    return out


def highlights(current: list[dict], history: list[dict], *, district: str = "",
               limit: int = 5, jump: float = 8.0, min_prior: int = 2) -> list[dict]:
    """이번 달 거래 가운데 신고가와 큰 변동만 골라낸다.

    · 신고가 — 같은 단지·같은 면적 칸에서 지난 거래를 모두 넘어선 값
    · 급변  — 같은 칸의 이번 달 평균이 지난 평균과 크게 벌어진 경우
    지난 거래가 `min_prior` 건 미만이면 비교할 근거가 없다고 보고 뺍니다.
    """
    past = _by_unit(history)
    found: list[dict] = []
    for key, deals in _by_unit(current).items():
        prior = past.get(key, [])
        if len(prior) < min_prior:
            continue
        prior_max = max(d["amount"] for d in prior)
        prior_avg = sum(d["amount"] for d in prior) / len(prior)
        top = max(deals, key=lambda d: d["amount"])

        if top["amount"] > prior_max:
            found.append({
                "kind": "신고가", "district": district, "name": top["name"],
                "dong": top["dong"], "area": top["area"], "floor": top["floor"],
                "amount": top["amount"], "before": prior_max, "date": top["date"],
                "pct": round((top["amount"] / prior_max - 1) * 100, 1) if prior_max else 0.0,
                "prior_count": len(prior),
            })
            continue

        now_avg = sum(d["amount"] for d in deals) / len(deals)
        pct = (now_avg / prior_avg - 1) * 100 if prior_avg else 0.0
        if len(deals) >= 2 and abs(pct) >= jump:
            found.append({
                "kind": "급등" if pct > 0 else "급락", "district": district,
                "name": top["name"], "dong": top["dong"], "area": top["area"],
                "floor": "", "amount": round(now_avg), "before": round(prior_avg),
                "date": top["date"], "pct": round(pct, 1), "prior_count": len(prior),
            })
    # 신고가를 먼저, 그다음 변동 폭이 큰 순서로
    found.sort(key=lambda r: (r["kind"] != "신고가", -abs(r["pct"])))
    return found[:limit]


def prev_month(ym: str) -> str:
    """'202609' → '202608'."""
    year, month = int(ym[:4]), int(ym[4:6])
    return f"{year - 1}12" if month == 1 else f"{year}{month - 1:02d}"


def _month_end(ym: str) -> date:
    year, month = int(ym[:4]), int(ym[4:6])
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def month_of(run_date: str) -> str:
    """신고가 다 들어온 마지막 달.

    계약일로부터 30일 안에 신고하므로, 그 달 마지막 날에서 30일이 지나야 자료가 찹니다.
    9월 7일에 8월을 보면 아직 절반도 안 들어와서 '거래 급감' 으로 잘못 읽습니다.
    """
    try:
        d = date.fromisoformat(run_date)
    except ValueError:
        d = date.today()
    candidate = prev_month(f"{d.year}{d.month:02d}")
    for _ in range(12):
        if _month_end(candidate) + timedelta(days=30) <= d:
            return candidate
        candidate = prev_month(candidate)
    return candidate


# 서울 25개 자치구의 시군구 코드(법정동코드 앞 5자리). 기사에 나온 구를 바로 찾아보기 위한 표.
# 다른 지역을 보려면 settings.yaml 의 stats.districts 를 고치면 되고, 이 표는 그때도 그대로 쓴다.
SEOUL_CODES = {
    "종로구": "11110", "중구": "11140", "용산구": "11170", "성동구": "11200",
    "광진구": "11215", "동대문구": "11230", "중랑구": "11260", "성북구": "11290",
    "강북구": "11305", "도봉구": "11320", "노원구": "11350", "은평구": "11380",
    "서대문구": "11410", "마포구": "11440", "양천구": "11470", "강서구": "11500",
    "구로구": "11530", "금천구": "11545", "영등포구": "11560", "동작구": "11590",
    "관악구": "11620", "서초구": "11650", "강남구": "11680", "송파구": "11710",
    "강동구": "11740",
}


def districts_for(cfg, focus: list[str] | None = None) -> list[dict]:
    """오늘 볼 지역 목록. 기사에 나온 구를 앞에 놓고, 나머지는 설정 순서대로 채운다.

    기사가 노원구를 다루는 날 표에 노원구가 없으면 글과 표가 따로 논다.
    """
    settings = cfg.get("stats", {}) or {}
    default = [dict(d) for d in (settings.get("districts", []) or [])]
    limit = int(settings.get("max_districts", 8))

    picked: list[dict] = []
    seen: set[str] = set()
    for name in focus or []:
        code = SEOUL_CODES.get(name)
        if code and code not in seen:
            picked.append({"name": name, "code": code, "focus": True})
            seen.add(code)
    for item in default:
        code = str(item.get("code", ""))
        if code and code not in seen:
            picked.append({**item, "focus": False})
            seen.add(code)
    return picked[:limit]


def month_label(ym: str) -> str:
    """'202608' → '2026년 8월'. 사람이 읽는 자리에만 씁니다."""
    return f"{ym[:4]}년 {int(ym[4:6])}월" if len(ym) == 6 and ym.isdigit() else ym


def collect(cfg, run_date: str, focus: list[str] | None = None) -> dict:
    """설정된 구들의 지난달·전달 거래를 모아 비교표로 만든다."""
    settings = cfg.get("stats", {}) or {}
    districts = districts_for(cfg, focus)
    if not deal_key() or not districts:
        return {}
    ym = month_of(run_date)
    before = prev_month(ym)
    # 신고가를 가리려면 지난 거래가 있어야 한다. 몇 달치를 더 받아 비교 바탕으로 쓴다.
    months_back = max(int(settings.get("history_months", 6)), 1)
    past_months = []
    cursor = before
    for _ in range(months_back):
        past_months.append(cursor)
        cursor = prev_month(cursor)

    rows, picks = [], []
    for item in districts:
        code, name = str(item.get("code", "")), str(item.get("name", ""))
        if not code:
            continue
        deals = apt_trades(cfg, code, ym)
        # 달마다 따로 담아 둔다. 전달 비교는 그 달 응답을 그대로 쓰고,
        # 신고가 비교에는 지난 달들을 전부 합쳐 쓴다.
        by_month = {month: apt_trades(cfg, code, month) for month in past_months}
        history = [d for deals_of_month in by_month.values() for d in deals_of_month]
        now, was = summarize(deals), summarize(by_month.get(before, []))
        if not now["count"] and not was["count"]:
            continue
        row = {"name": name, "code": code, "now": now, "was": was,
               "focus": bool(item.get("focus")),
               "change": now["count"] - was["count"]}
        if settings.get("jeonse", True):
            ratio = jeonse_ratio(deals, apt_rents(cfg, code, ym))
            if ratio:
                row["jeonse"] = ratio
        rows.append(row)
        picks += highlights(deals, history, district=name)

    if not rows:
        return {}
    rows.sort(key=lambda r: (not r["focus"], -r["now"]["count"]))
    picks.sort(key=lambda r: (r["kind"] != "신고가", -abs(r["pct"])))
    return {"month": ym, "month_label": month_label(ym),
            "before": before, "before_label": month_label(before), "districts": rows,
            "highlights": picks[: int(settings.get("max_highlights", 5))],
            "focus": [r["name"] for r in rows if r["focus"]],
            "jeonse": [{"name": r["name"], **r["jeonse"]} for r in rows if r.get("jeonse")],
            "history_months": months_back,
            "total": sum(r["now"]["count"] for r in rows),
            "total_before": sum(r["was"]["count"] for r in rows)}


# ── 한국부동산원 ─────────────────────────────────────────────


def reb_tables(cfg, keyword: str = "") -> list[dict]:
    """통계표 목록. 이름에 keyword 가 든 것만 (번호를 설정에 적기 위해 씁니다)."""
    key = reb_key()
    if not key:
        return []
    found = []
    for page in range(1, 6):
        try:
            resp = _get(REB_LIST_URL, params={"KEY": key, "Type": "json",
                                              "pIndex": str(page), "pSize": "100"},
                        timeout=float(cfg.get("collect.timeout_seconds", 15)))
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("부동산원 통계표 목록 실패: %s", type(exc).__name__)
            break
        rows = _reb_rows(data)
        if not rows:
            break
        for row in rows:
            name = str(row.get("STATBL_NM", "") or "")
            if not keyword or keyword in name:
                found.append({"id": str(row.get("STATBL_ID", "") or ""), "name": name,
                              "cycle": str(row.get("DTACYCLE_CD", "") or "")})
    return found


def _reb_rows(data) -> list[dict]:
    """부동산원 응답에서 자료 줄만 꺼낸다. 오류면 빈 목록."""
    if isinstance(data, dict) and "RESULT" in data:
        log.warning("부동산원 응답 오류: %s", str(data["RESULT"].get("CODE", ""))[:20])
        return []
    if isinstance(data, list):                      # [{head...}, {row: [...]}]
        for part in data:
            if isinstance(part, dict) and isinstance(part.get("row"), list):
                return part["row"]
        return []
    if isinstance(data, dict):
        for value in data.values():
            rows = _reb_rows(value)
            if rows:
                return rows
    return []


def week_id(d: date) -> str:
    """'2026-08-31' → '202636'. 부동산원 주간 통계의 시점 표기와 같은 ISO 주차."""
    year, week, _ = d.isocalendar()
    return f"{year}{week:02d}"


def reb_period(run_date: str, cycle: str, weeks: int = 12) -> tuple[str, str]:
    """조회할 시작·끝 시점. 주간이면 주차, 월간이면 연월."""
    try:
        end = date.fromisoformat(run_date)
    except ValueError:
        end = date.today()
    if cycle.upper() == "WK":
        return week_id(end - timedelta(weeks=weeks)), week_id(end)
    ym = f"{end.year}{end.month:02d}"
    start = ym
    for _ in range(max(weeks // 4, 1)):
        start = prev_month(start)
    return start, ym


def reb_all_series(cfg, run_date: str = "") -> dict[str, list[dict]]:
    """설정에 적힌 지수들을 한 번에. 열쇠는 사람이 읽는 이름('매매'·'전세')."""
    settings = cfg.get("stats", {}) or {}
    cycle = str(settings.get("reb_cycle", "WK") or "WK")
    count = int(settings.get("reb_weeks", 12))
    wanted = (("매매", str(settings.get("reb_statbl_id", "") or "")),
              ("전세", str(settings.get("reb_jeonse_statbl_id", "") or "")))
    out: dict[str, list[dict]] = {}
    for name, statbl_id in wanted:
        if not statbl_id:
            continue
        rows = reb_series(cfg, statbl_id, cycle, count=count, run_date=run_date)
        if rows:
            out[name] = rows
    return out


def reb_series(cfg, statbl_id: str, cycle: str = "WK", count: int = 12,
               region_id: str = "", run_date: str = "") -> list[dict]:
    """통계표 하나의 최근 값들. (시점, 값) 만 남깁니다.

    인증키가 없어도 견본으로 한 장에 5건씩 받을 수 있어, 키 없이도 최근 추이는 나옵니다.
    키가 거부되면(승인 대기·오타) 견본으로 물러납니다 — 아무것도 안 나오는 것보다 낫습니다.
    지역(CLS_ID)을 주면 서버가 걸러 주므로 훨씬 적게 받습니다.
    """
    if not statbl_id:
        return []
    settings = cfg.get("stats", {}) or {}
    region_id = region_id or str(settings.get("reb_region_id", "") or "")
    when = run_date or date.today().isoformat()
    timeout = float(cfg.get("collect.timeout_seconds", 15))

    key = reb_key()
    if key:
        rows = _reb_fetch(statbl_id, cycle, count, region_id, when, key, timeout)
        if rows:
            return rows
        log.warning("부동산원 인증키가 받아들여지지 않아 견본 자료로 대신합니다.")
    return _reb_fetch(statbl_id, cycle, count, region_id, when, "", timeout)


def _reb_fetch(statbl_id: str, cycle: str, count: int, region_id: str,
               run_date: str, key: str, timeout: float) -> list[dict]:
    # 인증키가 없으면 한 번에 5건까지만 주는데, 그 5건은 요청 구간의 앞쪽이다.
    # 그래서 구간 자체를 최근 5주로 좁혀야 '최근' 자료가 들어온다.
    span = count if key else min(count, 5)
    start, end = reb_period(run_date, cycle, span)
    params = {"STATBL_ID": statbl_id, "DTACYCLE_CD": cycle.upper(), "Type": "json",
              "START_WRTTIME": start, "END_WRTTIME": end,
              "pSize": "100" if key else "5"}
    if key:
        params["KEY"] = key
    if region_id:
        params["CLS_ID"] = region_id

    # 시점 하나에 값 하나. 인증키가 없으면 서버가 쪽 넘김을 무시하고 같은 자료를 되돌려주므로,
    # 새 시점이 하나도 안 늘면 거기서 멈춘다 (안 그러면 같은 줄만 쌓인다).
    seen: dict[str, dict] = {}
    for page in range(1, 12):
        try:
            resp = _get(REB_DATA_URL, params={**params, "pIndex": str(page)}, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("부동산원 통계 실패: %s", type(exc).__name__)
            break
        rows = _reb_rows(data)
        if not rows:
            break
        before = len(seen)
        for row in rows:
            try:
                value = float(str(row.get("DTA_VAL", "")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            stamp = str(row.get("WRTTIME_IDTFR_ID", "") or "")
            seen.setdefault(stamp, {"time": stamp, "value": value,
                                    "region": str(row.get("CLS_NM", "") or ""),
                                    "when": str(row.get("WRTTIME_DESC", "") or "")})
        if len(seen) == before or len(seen) >= count:
            break
    out = sorted(seen.values(), key=lambda r: r["time"])
    return out[-count:]
