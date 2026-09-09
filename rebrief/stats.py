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
    "contract": ("계약구분", "contractType"),
}


# 같은 서버가 연달아 안 열리면 더 두드리지 않습니다 (회로 차단기).
#
# 2026-09-09 아침 공공데이터포털이 통째로 안 열렸는데, 요청 57번이 저마다 15초씩 기다려
# 14분을 썼습니다. 그 사이 07:25 안전망이 출발해 같은 날을 한 번 더 만들었습니다. 연결
# 실패(연결 거부·시간 초과)가 세 번 이어지면 그 서버는 그날 포기하고 경고 한 줄만 남깁니다.
# HTTP 오류(4xx·5xx)는 세지 않습니다 — 서버는 살아 있고 요청이 틀린 것이라서요.
_DOWN_AFTER = 3
_failures: dict[str, int] = {}


class ServerDown(requests.ConnectionError):
    """연달아 안 열려 오늘은 포기한 서버. 부르는 쪽은 조용히 그만둡니다."""


def _get(url: str, **kw):
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc
    if _failures.get(host, 0) >= _DOWN_AFTER:
        raise ServerDown(host)
    try:
        resp = requests.get(url, **kw)
    except (requests.ConnectionError, requests.Timeout):
        _failures[host] = _failures.get(host, 0) + 1
        if _failures[host] == _DOWN_AFTER:
            log.warning("%s 가 %d번 연속 열리지 않아 오늘은 더 부르지 않습니다.", host, _DOWN_AFTER)
        raise
    _failures[host] = 0
    return resp


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


def _fetch_pages(cfg, url: str, code: str, ym: str, parse, label: str,
                 rows: int = 1000, max_pages: int = 6) -> list[dict]:
    """전체 건수를 채울 때까지 쪽을 넘긴다.

    한 쪽에 1000건이 상한이라 거래가 많은 구는 그냥 부르면 잘린다 (강남구 7월 전월세는
    1,359건이라 359건이 빠졌다). totalCount 를 보고 필요한 만큼만 더 받는다.
    """
    key = deal_key()
    if not key:
        return []
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            resp = _get(url, params={
                "serviceKey": key, "LAWD_CD": code, "DEAL_YMD": ym,
                "pageNo": str(page), "numOfRows": str(rows),
            }, timeout=float(cfg.get("collect.timeout_seconds", 15)))
            resp.raise_for_status()
        except requests.RequestException as exc:
            if not isinstance(exc, ServerDown):
                log.warning("%s를 가져오지 못했습니다(%s %s): %s", label, code, ym, type(exc).__name__)
            break
        got = parse(resp.text)
        out += got
        try:
            total = int(ET.fromstring(resp.text).findtext(".//totalCount") or 0)
        except ET.ParseError:
            break
        # 걸러 낸 건(해제 신고 등)이 있어 len(out) 과 total 이 정확히 같지는 않다.
        if not got or page * rows >= total:
            break
    return out


def apt_trades(cfg, code: str, ym: str, *, rows: int = 1000) -> list[dict]:
    """한 시군구(5자리 코드)의 한 달치 아파트 매매 실거래. 키가 없으면 빈 목록."""
    return _fetch_pages(cfg, DEAL_URL, code, ym, parse_trades, "실거래가", rows)


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
            "contract": _text(item, _RENT_FIELDS["contract"]),
            "area": round(area, 2),
            "date": f"{y}-{int(m):02d}-{int(d):02d}" if y and m and d else "",
        })
    return rows


def apt_rents(cfg, code: str, ym: str, *, rows: int = 1000) -> list[dict]:
    """한 시군구의 한 달치 아파트 전월세 실거래. 키가 없거나 신청 전이면 빈 목록."""
    return _fetch_pages(cfg, RENT_URL, code, ym, parse_rents, "전월세 실거래", rows)


def jeonse_ratio(trades: list[dict], rents: list[dict], *, min_pairs: int = 2) -> dict:
    """같은 단지·같은 면적 칸의 전세 보증금 ÷ 매매가.

    월세가 붙은 계약은 보증금이 낮아 섞으면 비율이 왜곡되므로 순수 전세만 씁니다.
    갱신 계약도 뺍니다 — 종전 보증금을 따라가 시세보다 낮습니다 (강남구 7월 실측에서
    갱신을 섞으면 34.7%, 신규만 보면 38.8% 로 4%p 넘게 차이가 났습니다).
    한쪽만 있는 칸은 셀 수 없으니 뺍니다.
    """
    sale = _by_unit(trades)
    pure = [r for r in rents if not r["monthly"] and r.get("contract") != "갱신"]
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
    median = round(_median([p["ratio"] for p in pairs]), 1)
    pairs.sort(key=lambda p: p["ratio"], reverse=True)
    return {"median": median, "pairs": pairs, "count": len(pairs)}


def _median(values: list[float]) -> float:
    """가운뎃값. 빈 목록이면 0."""
    if not values:
        return 0.0
    xs = sorted(values)
    mid = len(xs) // 2
    return float(xs[mid]) if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def rent_mix(rents: list[dict]) -> dict:
    """전월세 계약을 월세 낀 것과 순수 전세로 갈라 센다.

    전세가율은 순수 전세만 쓰므로 월세 계약은 계산에서 버려집니다. 그런데 '전세가 월세로
    바뀌는 중' 은 그 자체가 기사에 매일 나오는 이야기라, 버리는 대신 비중과 시세를 남깁니다.
    호출은 늘지 않습니다 — 전세가율을 내려고 이미 받아 둔 응답을 한 번 더 볼 뿐입니다.

    비중은 갱신까지 **전부** 셉니다(실제로 맺어진 계약 구성이 궁금한 것이므로). 반대로
    보증금·월세의 가운뎃값은 **신규만** 봅니다 — 갱신은 종전 조건을 따라가 시세가 아닙니다.
    """
    if not rents:
        return {}
    fresh = [r for r in rents if r.get("contract") != "갱신"]
    monthly = [r for r in rents if r["monthly"]]
    monthly_fresh = [r for r in fresh if r["monthly"]]
    jeonse_fresh = [r for r in fresh if not r["monthly"]]
    return {
        "total": len(rents),
        "monthly_count": len(monthly),
        "monthly_share": round(len(monthly) / len(rents) * 100, 1),
        "renew_count": sum(1 for r in rents if r.get("contract") == "갱신"),
        "jeonse_deposit": round(_median([r["deposit"] for r in jeonse_fresh])),
        "jeonse_count": len(jeonse_fresh),
        "rent_deposit": round(_median([r["deposit"] for r in monthly_fresh])),
        "rent_monthly": round(_median([r["monthly"] for r in monthly_fresh])),
        "rent_count": len(monthly_fresh),
    }


# ── 면적대별 ────────────────────────────────────────────────
#
# 같은 구라도 소형만 팔리는 달과 대형만 팔리는 달은 평균가가 딴판입니다. 평균가 하나만
# 보면 "값이 올랐다" 와 "비싼 것만 팔렸다" 를 구별할 수 없어 면적대를 나눠 함께 냅니다.
# 경계 60·85·135㎡ 는 전용면적 기준이고, 85㎡ 는 국민주택규모입니다.

SIZE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("소형", 0.0, 60.0),
    ("중형", 60.0, 85.0),
    ("중대형", 85.0, 135.0),
    ("대형", 135.0, float("inf")),
)


def size_change(now: list[dict], was: list[dict]) -> list[dict]:
    """이번 달 면적대별 구성에 전달 비중을 나란히 붙인다. 전달 자료가 없으면 그대로 둔다."""
    before = {r["band"]: r for r in was}
    out = []
    for row in now:
        prior = before.get(row["band"])
        out.append({**row,
                    "was_share": prior["share"] if prior else None,
                    "share_change": round(row["share"] - prior["share"], 1) if prior else None})
    return out


def size_band(area: float) -> str:
    """전용면적 → 면적대 이름. 60㎡ 정확히면 '소형'(이하 기준)."""
    try:
        value = float(area)
    except (TypeError, ValueError):
        return ""
    for name, lo, hi in SIZE_BANDS:
        if lo < value <= hi or (lo == 0.0 and value <= hi):
            return name
    return ""


def size_mix(rows: list[dict]) -> list[dict]:
    """거래를 면적대로 갈라 건수·비중·평균가. 거래가 없으면 빈 목록."""
    if not rows:
        return []
    buckets: dict[str, list[dict]] = {name: [] for name, _, _ in SIZE_BANDS}
    for r in rows:
        band = size_band(r.get("area", 0))
        if band:
            buckets[band].append(r)
    total = sum(len(v) for v in buckets.values())
    if not total:
        return []
    return [{"band": name, "count": len(v),
             "share": round(len(v) / total * 100, 1),
             "avg": sum(d["amount"] for d in v) // len(v) if v else 0}
            for name, _, _ in SIZE_BANDS
            for v in [buckets[name]]]


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
    all_now: list[dict] = []      # 면적대 합계용. 구별로 나눈 것과 별개로 전체도 낸다.
    all_was: list[dict] = []
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
            # 응답을 한 번만 받아 전세가율과 월세 구성에 함께 쓴다 (호출을 늘리지 않으려고).
            rents = apt_rents(cfg, code, ym)
            ratio = jeonse_ratio(deals, rents)
            if ratio:
                ratio["pairs"] = ratio["pairs"][:5]
                row["jeonse"] = ratio
            mix = rent_mix(rents)
            if mix:
                row["rent"] = mix
        all_now += deals
        all_was += by_month.get(before, [])
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
            # 단지 목록은 상위 몇 곳만 남긴다 — 전부 실으면 하루 200KB 가 매일 커밋된다
            "jeonse": [{"name": r["name"], **r["jeonse"],
                        "pairs": r["jeonse"]["pairs"][:5]}
                       for r in rows if r.get("jeonse")],
            "rent": [{"name": r["name"], **r["rent"]} for r in rows if r.get("rent")],
            "sizes": size_change(size_mix(all_now), size_mix(all_was)),
            **_city_block(cfg, ym, before, settings),
            "warnings": suspect_drops(rows),
            "history_months": months_back,
            "total": sum(r["now"]["count"] for r in rows),
            "total_before": sum(r["was"]["count"] for r in rows)}


def _city_block(cfg, ym: str, before: str, settings: dict) -> dict:
    """서울 한 바퀴 결과를 collect 가 내놓는 모양으로. 꺼져 있으면 빈 칸만 돌려준다."""
    if not settings.get("map", True):
        return {"map": {}, "map_jeonse": {}, "swings": []}
    city = city_wide(cfg, ym, before, jeonse=bool(settings.get("jeonse", True)))
    return {"map": city.get("counts", {}),
            "map_jeonse": city.get("jeonse", {}),
            "swings": district_swings(city.get("counts", {}), city.get("before", {}),
                                      city.get("hotspots", {}))}


def city_wide(cfg, ym: str, before: str = "", *, jeonse: bool = True) -> dict:
    """서울 25개 구를 한 바퀴 돈다. 지도 두 장과 구 단위 급변을 여기서 얻는다.

    표에 올리는 8개 구는 지난 달들까지 받지만, 여기서는 **그 달(과 전달) 한 번씩**만
    부릅니다. 빠진 칸이 열일곱이나 되는 지도는 읽을 값이 없어서, 지도를 그릴 거면
    스물다섯을 다 채워야 합니다.
    """
    if not deal_key():
        return {}
    counts: dict[str, int] = {}
    was: dict[str, int] = {}
    ratios: dict[str, float] = {}
    hotspots: dict[str, dict] = {}
    for name, code in SEOUL_CODES.items():
        deals = apt_trades(cfg, code, ym)
        if deals:
            counts[name] = len(deals)
            hotspots[name] = _busiest_dong(deals)
        if before:
            prior = apt_trades(cfg, code, before)
            if prior:
                was[name] = len(prior)
        if jeonse and deals:
            ratio = jeonse_ratio(deals, apt_rents(cfg, code, ym))
            if ratio:
                ratios[name] = ratio["median"]
    return {"counts": counts, "before": was, "jeonse": ratios, "hotspots": hotspots}


def _busiest_dong(deals: list[dict]) -> dict:
    """거래가 가장 많은 법정동과 그 비중. 구 전체가 움직였는지 한 동네가 움직였는지 가른다."""
    tally: dict[str, int] = {}
    for d in deals:
        if d.get("dong"):
            tally[d["dong"]] = tally.get(d["dong"], 0) + 1
    if not tally:
        return {}
    dong, count = max(tally.items(), key=lambda x: x[1])
    return {"dong": dong, "count": count, "share": round(count / len(deals) * 100, 1)}


def district_swings(counts: dict, before: dict, hotspots: dict | None = None, *,
                    pct: float = 20.0, min_count: int = 40, limit: int = 4,
                    concentrated: float = 40.0) -> list[dict]:
    """구 단위로 거래가 크게 늘거나 준 곳. 단지 단위 신고가와는 다른 이야기다.

    표의 '전달 대비' 는 우리가 고른 여덟 곳만 보여 줍니다. 스물다섯 곳을 다 세고 나면
    "중랑구가 141% 늘었다" 같은 것을 프로그램이 집어낼 수 있습니다. 거래가 원래 적은 구는
    몇 건만 움직여도 비율이 크게 튀므로 `min_count` 미만이면 뺍니다.

    **한 동네에 몰린 경우를 밝힙니다.** 2026년 7월 중랑구가 208→501건으로 늘었는데
    501건 가운데 332건이 묵동이었습니다. 이런 달은 구 전체가 달아오른 게 아니라 큰 단지
    한 곳이 한꺼번에 신고된 것입니다. 비중이 `concentrated` 를 넘으면 그 동네를 함께 답니다.
    """
    found = []
    for name, now in counts.items():
        prior = before.get(name, 0)
        if prior < min_count or now < min_count:
            continue
        change = (now / prior - 1) * 100
        if abs(change) < pct:
            continue
        spot = (hotspots or {}).get(name) or {}
        # 템플릿이 StrictUndefined 라 항목은 늘 있어야 한다. 몰린 곳이 없으면 None.
        found.append({"name": name, "now": now, "before": prior, "pct": round(change, 1),
                      "hotspot": spot if spot.get("share", 0) >= concentrated else None})
    found.sort(key=lambda r: -abs(r["pct"]))
    return found[:limit]


def suspect_drops(rows: list[dict], *, floor: int = 30, ratio: float = 0.1) -> list[str]:
    """거래가 0에 가깝게 떨어진 구. **자료가 아니라 호출을 의심하라는 신호**다.

    응답이 비어 오면 그 구는 조용히 0건이 되어 표에서 빠집니다. 진짜 거래 급감과
    호출 실패가 겉으로 똑같이 보이는 것입니다. 전달에 넉넉히 있던 곳이 10분의 1 아래로
    내려가면 사람이 한 번 보게 합니다 — 실제로 이런 달은 거의 없습니다.
    """
    out = []
    for row in rows:
        was = row.get("was", {}).get("count", 0)
        now = row.get("now", {}).get("count", 0)
        if was >= floor and now <= was * ratio:
            out.append(f"{row['name']} 거래가 {was}건 → {now}건으로 떨어졌습니다. "
                       f"실제 급감일 수도 있지만 응답이 비어 왔을 가능성을 먼저 확인하세요.")
    return out


# ── 한국부동산원 ─────────────────────────────────────────────



# ── 공급 쪽 통계 (미분양·인허가·착공) ────────────────────────
#
# 지금까지 우리 통계는 전부 '팔린 것' 이었습니다. 미분양은 지금 안 팔리고 남은 물량이고,
# 인허가·착공은 1~2년 뒤 공급을 말해 줍니다. 세 숫자의 **성질이 서로 다릅니다.**


def reb_supply_rows(cfg, statbl_id: str, cls_id: str, start: str, end: str,
                    max_pages: int = 20) -> dict[str, float]:
    """월간 통계표 한 장을 시점→값으로. 인증키가 없으면 빈 표."""
    key = reb_key()
    if not key or not statbl_id:
        return {}
    out: dict[str, float] = {}
    timeout = float(cfg.get("collect.timeout_seconds", 15))
    for page in range(1, max_pages + 1):
        params = {"KEY": key, "Type": "json", "STATBL_ID": statbl_id, "DTACYCLE_CD": "MM",
                  "START_WRTTIME": start, "END_WRTTIME": end,
                  "pIndex": str(page), "pSize": "100"}
        if cls_id:
            params["CLS_ID"] = str(cls_id)
        try:
            resp = _get(REB_DATA_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            rows = _reb_rows(resp.json())
        except (requests.RequestException, ValueError) as exc:
            if not isinstance(exc, ServerDown):
                log.warning("부동산원 공급 통계 실패(%s): %s", statbl_id, type(exc).__name__)
            break
        if not rows:
            break
        before = len(out)
        for row in rows:
            try:
                out[str(row.get("WRTTIME_IDTFR_ID", ""))] = float(
                    str(row.get("DTA_VAL", "")).replace(",", ""))
            except (TypeError, ValueError):
                continue
        if len(out) == before:
            break
    return dict(sorted(out.items()))


def de_cumulate(rows: dict[str, float]) -> dict[str, float]:
    """연초부터 쌓인 누계를 그 달치로 되돌린다.

    **인허가 실적은 누계입니다** (2026-09-08 실측: 서울 2025년 1월 2,801 → 12월 41,912 →
    2026년 1월 1,250 으로 초기화). 그대로 '6월 20,838호' 라고 쓰면 그 달에 그만큼 허가된
    것처럼 읽혀 글이 거짓말이 됩니다. 1월은 누계가 곧 그 달치라 그대로 둡니다.
    """
    out: dict[str, float] = {}
    prev_year, prev_value = "", 0.0
    for time, value in sorted(rows.items()):
        year, month = time[:4], time[4:6]
        out[time] = value if (month == "01" or year != prev_year) else value - prev_value
        prev_year, prev_value = year, value
    return out


def reb_supply(cfg, run_date: str = "") -> list[dict]:
    """미분양·인허가·착공을 한 번에. 설정에 적힌 통계표만 봅니다.

    발표가 두세 달 늦으므로 넉넉한 구간을 요청하고 받은 것 중 최근치만 씁니다.
    """
    settings = ((cfg.get("stats", {}) or {}).get("supply", {}) or {})
    if not settings.get("enabled", True) or not reb_key():
        return []
    try:
        end = date.fromisoformat(run_date or date.today().isoformat())
    except ValueError:
        end = date.today()
    months = max(int(settings.get("months", 13)), 2)
    start_ym = f"{end.year - 2}01"          # 발표 지연을 감안해 넉넉히
    end_ym = f"{end.year}12"

    out: list[dict] = []
    for item in settings.get("tables", []) or []:
        rows = reb_supply_rows(cfg, str(item.get("id", "")), str(item.get("cls", "")),
                               start_ym, end_ym)
        if not rows:
            continue
        if str(item.get("mode", "monthly")) == "cumulative":
            rows = de_cumulate(rows)
        picked = list(rows.items())[-months:]
        if not picked:
            continue
        out.append({
            "name": str(item.get("name", "")),
            "unit": str(item.get("unit", "호")),
            "mode": str(item.get("mode", "monthly")),
            "note": str(item.get("note", "")),
            "rows": [{"time": t, "label": month_label(t), "value": v} for t, v in picked],
            "latest": picked[-1][1],
            "latest_label": month_label(picked[-1][0]),
            "before": picked[-2][1] if len(picked) > 1 else None,
        })
    return out


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
            if not isinstance(exc, ServerDown):
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
        code = str(data["RESULT"].get("CODE", ""))[:20]
        # INFO-200 은 '해당 자료 없음' 입니다. 쪽을 넘기다 끝에 닿으면 늘 나오므로
        # 오류로 올리면 정상 실행에도 경고가 줄줄이 찍힙니다.
        log.debug("부동산원 자료 없음: %s", code) if code == "INFO-200" else \
            log.warning("부동산원 응답 오류: %s", code)
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
            if not isinstance(exc, ServerDown):
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
