"""오늘의 핵심 수치 — 브리핑의 datapoint 가운데 글에 실제로 쓰인 것을 고르고, 카드로 만든다.

수치 토큰 규칙(무엇을 '같은 수치'로 볼지)도 여기에 둔다. render.py 의 형광펜과
카드가 같은 규칙을 써야 "카드에 있는 숫자가 본문에서는 안 잡히는" 일이 없다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import DailyBrief, DataPoint

# 수치 토큰: 숫자(천 단위 쉼표·소수 허용) + 단위. 긴 단위를 앞에 둬야 '개월' 이 '개' 로 잘리지 않는다.
NUM_UNITS = (
    r"%p|%포인트|%|％|조\s?원|억\s?원|만\s?원|천\s?원|원|"
    r"만\s?가구|만\s?세대|만\s?호|만\s?명|만\s?건|천\s?가구|천\s?세대|조|억|만|"
    r"가구|세대|개구|개월|개소|개동|단지|개|건|호|채|곳|동|명|년|배|㎡|평|층|위|점|회|bp|p|포인트"
)
NUM_TOKEN = re.compile(
    r"(?<![\d,.\-A-Za-z%])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(" + NUM_UNITS + r")?"
)
# 글자가 아닌 조각: 태그, 엔티티(&#39; 처럼 숫자를 품는다), 아직 치환 전인 이미지 자리, 소제목 전체
OPAQUE = re.compile(r"(<h[1-6][^>]*>.*?</h[1-6]>|<[^>]+>|&#?\w+;|\[이미지\s*:[^\]]*\])", re.DOTALL)


def number_key(match: re.Match) -> str | None:
    """같은 수치로 볼 정규화 키. 데이터가 아닌 숫자(연도·날짜·단위 없는 정수)는 None."""
    whole, frac, unit = match.group(1), match.group(2) or "", match.group(3)
    digits = whole.replace(",", "")
    if unit is None:
        # 단위가 없으면 쉼표나 소수점이 있는 것만 수치로 본다. '3' 같은 정수는 셈에서 뺀다.
        if "," not in whole and not frac:
            return None
        following = match.string[match.end():match.end() + 1]
        if following in "월일시분초":          # 9월 6일, 10시 — 날짜·시각
            return None
        return digits + frac
    unit = unit.replace(" ", "").replace("％", "%")
    if unit == "년" and len(digits) == 4:     # 2026년 — 연도는 수치가 아니다
        return None
    return digits + frac + unit


def keys_in(text: str) -> dict[str, int]:
    """글자 조각에서 수치 키별 등장 횟수."""
    counts: dict[str, int] = {}
    for i, seg in enumerate(OPAQUE.split(text or "")):
        if i % 2:
            continue
        for m in NUM_TOKEN.finditer(seg):
            key = number_key(m)
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass
class KeyNumber:
    display: str      # 화면에 보일 그대로. 예: '-0.03%', '22개구'
    key: str          # 본문에서 같은 수치를 찾을 정규화 키
    label: str        # 무엇의 수치인지
    note: str = ""    # 기준 시점이나 비교 한 줄


def _parse(dp: DataPoint) -> tuple[str, str] | None:
    value = " ".join((dp.value or "").split())
    unit = " ".join((dp.unit or "").split())
    display = value if (not unit or value.endswith(unit)) else value + unit
    m = NUM_TOKEN.search(display.replace(" ", ""))
    if not m:
        return None
    key = number_key(m)
    return (display, key) if key else None


def pick(brief: DailyBrief | None, body_markdown: str, limit: int = 3) -> list[KeyNumber]:
    """이슈 순위대로, 글에 실제로 등장한 수치를 먼저, 이슈당 하나씩 고른다.

    본문에 있는 수치가 항상 먼저다(카드에 있는 숫자를 본문에서 못 찾으면 이상하니까). 그 안에서
    이슈당 하나씩 고루 뽑고, 모자라면 같은 이슈의 다른 수치, 그래도 모자라면 본문에 없는 수치 순.
    같은 수치가 여러 이슈에 있으면 한 번만.
    """
    if not brief or limit <= 0:
        return []
    in_body = keys_in(body_markdown)
    cands: list[tuple[int, bool, DataPoint, str, str]] = []
    for rank, issue in enumerate(brief.issues):
        for dp in issue.numbers:
            parsed = _parse(dp)
            if parsed:
                display, key = parsed
                cands.append((rank, key not in in_body, dp, display, key))
    cands.sort(key=lambda c: c[0])                   # 이슈 순위대로
    chosen: list[KeyNumber] = []
    seen_keys: set[str] = set()
    seen_issues: set[int] = set()
    for allow_missing, one_per_issue in ((False, True), (False, False), (True, True), (True, False)):
        for rank, missing, dp, display, key in cands:
            if key in seen_keys or (missing and not allow_missing) or (one_per_issue and rank in seen_issues):
                continue
            chosen.append(KeyNumber(display, key, dp.label.strip(), (dp.period or dp.context).strip()))
            seen_keys.add(key)
            seen_issues.add(rank)
            if len(chosen) >= limit:
                return chosen
    return chosen


_CELL = ("text-align:center;padding:14px 8px;border:1px solid #dddddd;"
         "background-color:#f7f8fa;vertical-align:top;width:{w}%")


def card_html(nums: list[KeyNumber]) -> str:
    """네이버 붙여넣기용 '오늘의 숫자' 카드. 표라야 에디터가 칸 구조를 살린다."""
    if not nums:
        return ""
    from html import escape

    w = 100 // len(nums)
    cells = []
    for n in nums:
        cell = (f'<span style="font-size:30px;font-weight:700;color:#256abf">{escape(n.display)}</span>'
                f'<br><span style="font-size:14px">{escape(n.label)}</span>')
        if n.note:
            cell += f'<br><span style="font-size:12px;color:#888888">{escape(n.note)}</span>'
        cells.append(f'<td style="{_CELL.format(w=w)}">{cell}</td>')
    return ('<p style="margin:0 0 6px;font-size:14px;color:#6b7178">오늘의 숫자</p>\n'
            '<table style="width:100%;border-collapse:collapse;margin:0 0 24px"><tr>'
            + "".join(cells) + "</tr></table>\n")


def card_markdown(nums: list[KeyNumber]) -> str:
    if not nums:
        return ""
    esc = lambda s: s.replace("|", "／")
    head = "| " + " | ".join(f"**{esc(n.display)}**" for n in nums) + " |"
    rule = "|" + ":---:|" * len(nums)
    labels = "| " + " | ".join(esc(n.label) + (f"<br><small>{esc(n.note)}</small>" if n.note else "") for n in nums) + " |"
    return "**오늘의 숫자**\n\n" + "\n".join([head, rule, labels]) + "\n"
