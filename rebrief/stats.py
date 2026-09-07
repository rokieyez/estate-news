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
        rows.append({
            "name": _text(item, _FIELDS["name"]),
            "dong": _text(item, _FIELDS["dong"]),
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


def month_label(ym: str) -> str:
    """'202608' → '2026년 8월'. 사람이 읽는 자리에만 씁니다."""
    return f"{ym[:4]}년 {int(ym[4:6])}월" if len(ym) == 6 and ym.isdigit() else ym


def collect(cfg, run_date: str) -> dict:
    """설정된 구들의 지난달·전달 거래를 모아 비교표로 만든다."""
    settings = cfg.get("stats", {}) or {}
    districts = list(settings.get("districts", []) or [])
    if not deal_key() or not districts:
        return {}
    ym = month_of(run_date)
    before = prev_month(ym)
    rows = []
    for item in districts[: int(settings.get("max_districts", 8))]:
        code, name = str(item.get("code", "")), str(item.get("name", ""))
        if not code:
            continue
        now = summarize(apt_trades(cfg, code, ym))
        was = summarize(apt_trades(cfg, code, before))
        if not now["count"] and not was["count"]:
            continue
        rows.append({"name": name, "code": code, "now": now, "was": was,
                     "change": now["count"] - was["count"]})
    if not rows:
        return {}
    rows.sort(key=lambda r: r["now"]["count"], reverse=True)
    return {"month": ym, "month_label": month_label(ym),
            "before": before, "before_label": month_label(before), "districts": rows,
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


def reb_series(cfg, statbl_id: str, cycle: str = "WK", count: int = 12) -> list[dict]:
    """통계표 하나의 최근 값들. (시점, 값) 만 남깁니다."""
    key = reb_key()
    if not key or not statbl_id:
        return []
    try:
        resp = _get(REB_DATA_URL, params={
            "KEY": key, "STATBL_ID": statbl_id, "DTACYCLE_CD": cycle,
            "Type": "json", "pIndex": "1", "pSize": str(max(count, 1)),
        }, timeout=float(cfg.get("collect.timeout_seconds", 15)))
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("부동산원 통계 실패: %s", type(exc).__name__)
        return []
    out = []
    for row in _reb_rows(data):
        when = str(row.get("WRTTIME_IDTFR_ID", "") or "")
        raw = row.get("DTA_VAL", "")
        try:
            value = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        out.append({"time": when, "value": value,
                    "region": str(row.get("CLS_NM", "") or "")})
    out.sort(key=lambda r: r["time"])
    return out
