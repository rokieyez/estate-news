"""수치를 인포그래픽 SVG 로 만든다.

이슈 주제는 매일 바뀌므로 특정 주제에 고정하지 않는다. datapoint 의 생김새를
보고 맞는 그림 형태를 고르며, 맞는 게 없으면 아무것도 만들지 않는다.

  · 서울 자치구 도식 — '자치구 수' + 제외 목록이 있는 날만
  · 지수 비교 막대   — 증감률(%)이 있을 때
  · 수치 카드        — 그 외 모든 수치의 기본형

색은 dataviz 기준 팔레트를 따른다. 흰 글씨/파랑 대비 5.39:1.
PNG 변환은 헤드리스 브라우저가 있을 때만 덤으로 한다(필수 의존성 아님).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# ── 팔레트 ───────────────────────────────────────────────────
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
BASELINE = "#c3c2b7"
GRID = "#e1e0d9"        # 눈금선 (가는 선)
BLUE = "#256abf"        # 파랑 step500 — 흰 글씨 대비 5.39:1
BLUE_SOFT = "#cde2fb"   # 파랑 step100
CRITICAL = "#d03b3b"
GOOD = "#006300"
# 맥(Apple SD Gothic Neo) → 리눅스 러너(Noto Sans CJK KR, 워크플로에서 설치) → 그 외 순서.
# 러너에 한글 글꼴이 없으면 PNG 의 한글이 전부 네모로 깨진다 (2026-09-07 실제로 그랬음).
FONT = "'Apple SD Gothic Neo','Noto Sans CJK KR','Noto Sans KR','NanumGothic',system-ui,-apple-system,sans-serif"

# 서울 25개 자치구의 상대 위치 도식. 실제 지형·면적과는 무관하다.
SEOUL_LAYOUT: list[dict[int, str]] = [
    {3: "도봉", 4: "노원"},
    {1: "은평", 2: "강북", 3: "성북", 4: "중랑"},
    {1: "서대문", 2: "종로", 3: "동대문", 4: "광진"},
    {0: "강서", 1: "마포", 2: "중구", 3: "성동", 4: "송파", 5: "강동"},
    {0: "양천", 1: "영등포", 2: "용산", 3: "서초", 4: "강남"},
    {0: "구로", 1: "금천", 2: "동작", 3: "관악"},
]
SEOUL_GU = {gu for row in SEOUL_LAYOUT for gu in row.values()}


@dataclass
class Image:
    slug: str
    svg: str
    title: str


# ── 공통 헬퍼 ────────────────────────────────────────────────


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_width(s: str, size: float) -> float:
    """한글은 1글자폭, 그 외는 대략 절반으로 어림한다."""
    wide = sum(1 for ch in s if ord(ch) > 0x1100)
    return (wide + (len(s) - wide) * 0.55) * size


def wrap(s: str, size: float, max_width: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and text_width(trial, size) > max_width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """오른쪽 끝만 둥근 가로 막대. 기준선 쪽은 각지게 둔다."""
    r = min(r, max(w, 0), h / 2)
    if r <= 0:
        return f"M{x},{y} h{w} v{h} h{-w} Z"
    return (f"M{x},{y} H{x + w - r} A{r},{r} 0 0 1 {x + w},{y + r} "
            f"V{y + h - r} A{r},{r} 0 0 1 {x + w - r},{y + h} H{x} Z")


def svg_open(w: float, h: float) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}" height="{h:g}" '
            f'viewBox="0 0 {w:g} {h:g}" font-family="{FONT}">',
            f'<rect width="{w:g}" height="{h:g}" fill="{SURFACE}"/>']


def footnotes(parts: list[str], x: float, y: float, size: float = 16) -> list[str]:
    return [f'<text x="{x:g}" y="{y + i * 24:g}" font-size="{size:g}" fill="{MUTED}">{esc(t)}</text>'
            for i, t in enumerate(parts)]


CARD = "#ffffff"
CARD_EDGE = "#e6e5df"
CHIP_BG = "#eef4fd"

# 카드 여백 — 바깥 테두리(M)와 카드 안쪽 여백(P)
M, P = 40, 44


def chip(x: float, y: float, text: str, *, fill: str = CHIP_BG, color: str = BLUE,
         size: float = 20) -> str:
    """작은 알약 라벨. 기준 시점·구분 표시에 쓴다."""
    w = text_width(text, size) + size * 1.6
    h = size * 1.9
    return (f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{h / 2:g}" fill="{fill}"/>'
            f'<text x="{x + w / 2:g}" y="{y + h * 0.68:g}" font-size="{size:g}" font-weight="600" '
            f'text-anchor="middle" fill="{color}">{esc(text)}</text>')


def head_height(w: float, title: str, subtitle: str = "", header: bool = True) -> float:
    """머리말(채널·날짜·제목·구분선)이 차지하는 높이 = 본문이 시작되는 y."""
    inner = w - (M + P) * 2
    y = M + P + 22 + (34 if header else 0)
    y += 44 * len(wrap(title, 34, inner)[:2])
    if subtitle:
        y += 34
    return y + 26 + 34


def card_height(w: float, title: str, subtitle: str, content_h: float, notes: list[str],
                header: bool = True) -> float:
    """머리말 + 본문 + 각주를 더한 카드 전체 높이."""
    return head_height(w, title, subtitle, header) + content_h + notes_height(notes)


def frame_open(w: float, h: float, *, title: str, subtitle: str = "",
               channel: str = "", date: str = "") -> tuple[list[str], dict]:
    """모든 그림이 공유하는 카드 틀.

    흰 카드 + 머리말(채널·날짜) + 제목 + 가는 구분선. 낱장으로 보나 여러 장을 나란히 보나
    같은 서식이라 자료처럼 읽힌다. 본문을 그리기 시작할 y 를 함께 돌려준다.
    """
    x = M + P
    inner = w - (M + P) * 2
    p = svg_open(w, h)
    p.append(f'<rect x="{M}" y="{M}" width="{w - M * 2:g}" height="{h - M * 2:g}" rx="22" '
             f'fill="{CARD}" stroke="{CARD_EDGE}" stroke-width="1.5"/>')

    head_y = M + P + 22
    if channel:
        p.append(f'<rect x="{x}" y="{head_y - 15}" width="5" height="20" rx="2.5" fill="{BLUE}"/>')
        p.append(f'<text x="{x + 14}" y="{head_y}" font-size="20" font-weight="700" '
                 f'fill="{BLUE}">{esc(channel)}</text>')
    if date:
        p.append(f'<text x="{x + inner}" y="{head_y}" font-size="19" text-anchor="end" '
                 f'fill="{MUTED}">{esc(date)}</text>')

    y = head_y + (34 if (channel or date) else 0)
    lines = wrap(title, 34, inner)[:2]
    for i, line in enumerate(lines):
        y += 44
        p.append(f'<text x="{x}" y="{y}" font-size="34" font-weight="700" fill="{INK}">{esc(line)}</text>')
    if subtitle:
        y += 34
        p.append(f'<text x="{x}" y="{y}" font-size="21" fill="{INK_2}">{esc(subtitle)}</text>')
    y += 26
    p.append(f'<line x1="{x}" y1="{y}" x2="{x + inner}" y2="{y}" stroke="{GRID}" stroke-width="1"/>')
    return p, {"x": x, "inner": inner, "top": y + 34, "w": w, "h": h}


def frame_close(p: list[str], notes: list[str], geom: dict) -> None:
    """아래쪽 구분선과 각주. 각주는 카드 바닥에 붙인다."""
    notes = [n for n in notes if n]
    x, inner, h = geom["x"], geom["inner"], geom["h"]
    base = h - M - P - max(len(notes) - 1, 0) * 24 - 4
    p.append(f'<line x1="{x}" y1="{base - 30}" x2="{x + inner}" y2="{base - 30}" '
             f'stroke="{GRID}" stroke-width="1"/>')
    p += footnotes(notes, x, base, size=17)
    p.append("</svg>")


def notes_height(notes: list[str]) -> float:
    """각주 줄 수에 맞춰 카드 아래에 확보할 높이.

    frame_close 가 각주를 카드 바닥에서 거꾸로 배치하므로, 이 값이 모자라면 본문과 겹친다.
    (본문 끝 + 40) 자리에 첫 각주가 오도록 여백까지 포함해 계산한다.
    """
    return max(len([n for n in notes if n]), 1) * 24 + 104


def _channel(extra: dict | None) -> str:
    return str((extra or {}).get("channel", "") or "")


def _num(dp: dict) -> float | None:
    """'-0.03', '1~2', '4' 같은 값에서 대표 숫자를 뽑는다. 못 뽑으면 None."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(dp.get("value", "")))
    return float(m.group()) if m else None


def _source_line(dp: dict, date: str) -> str:
    bits = [b for b in [dp.get("source", ""), f"{date} 보도" if date else ""] if b]
    return "출처 " + " · ".join(bits) if bits else ""


# ── 1) 서울 자치구 도식 ──────────────────────────────────────

_EXCLUDE_RE = re.compile(r"([가-힣]{2,3}(?:[·,、]\s*[가-힣]{2,3})*)\s*(?:\d+개구)?\s*만?\s*제외")


def seoul_district_map(dp: dict, date: str, extra: dict | None = None) -> Image | None:
    """'자치구 N개' + '가·나·다 제외' 형태일 때만 그린다."""
    if "자치구" not in dp.get("label", ""):
        return None
    value = _num(dp)
    if value is None:
        return None
    m = _EXCLUDE_RE.search(dp.get("context", ""))
    if not m:
        return None
    excluded = {g for g in re.split(r"[·,、]\s*", m.group(1)) if g in SEOUL_GU}
    if not excluded:
        return None
    taxed = len(SEOUL_GU) - len(excluded)
    if taxed != int(value):
        # 도식과 보도값이 어긋나면 그리지 않는다. 틀린 그림보다 없는 편이 낫다.
        log.warning("자치구 도식 건너뜀: 도식 %d개 ≠ 보도 %s개", taxed, dp.get("value"))
        return None

    tw, th, gap = 128, 84, 10
    notes = ["※ 실제 지형이 아닌 위치 도식입니다. 칸의 모양·크기는 면적과 무관합니다."]
    if dp.get("context"):
        notes.append(f'※ {dp["context"]}')
    notes.append(_source_line(dp, date))

    w = (M + P) * 2 + 6 * tw + 5 * gap
    rows = len(SEOUL_LAYOUT)
    sub = (extra or {}).get("subtitle", "")
    h = card_height(w, dp["label"].replace(" 수", ""), sub, 34 + rows * (th + gap) - gap + 24, notes)
    p, g = frame_open(w, h, title=dp["label"].replace(" 수", ""), subtitle=sub,
                      channel=_channel(extra), date=date)
    x, y = g["x"], g["top"]

    # 범례. 칸마다 이름도 적으므로 색만으로 구분되지는 않는다.
    p.append(f'<rect x="{x}" y="{y - 14}" width="18" height="18" rx="5" fill="{BLUE}"/>')
    p.append(f'<text x="{x + 27}" y="{y}" font-size="19" fill="{INK_2}">대상 {taxed}개구</text>')
    lx = x + 165
    p.append(f'<rect x="{lx}" y="{y - 14}" width="18" height="18" rx="5" fill="{CARD}" '
             f'stroke="{BASELINE}" stroke-width="2" stroke-dasharray="4 3"/>')
    p.append(f'<text x="{lx + 27}" y="{y}" font-size="19" fill="{INK_2}">'
             f'제외 {len(excluded)}개구 ({"·".join(sorted(excluded))})</text>')

    map_top = y + 34
    for r, row in enumerate(SEOUL_LAYOUT):
        for c, gu in row.items():
            gx, gy = x + c * (tw + gap), map_top + r * (th + gap)
            if gu in excluded:
                p.append(f'<rect x="{gx}" y="{gy}" width="{tw}" height="{th}" rx="10" '
                         f'fill="{SURFACE}" stroke="{BASELINE}" stroke-width="2" stroke-dasharray="6 4"/>')
                p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 + 2:g}" font-size="23" '
                         f'text-anchor="middle" fill="{INK_2}">{gu}</text>')
                p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 + 26:g}" font-size="15" '
                         f'text-anchor="middle" fill="{MUTED}">제외</text>')
            else:
                p.append(f'<rect x="{gx}" y="{gy}" width="{tw}" height="{th}" rx="10" fill="{BLUE}"/>')
                p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 + 9:g}" font-size="24" '
                         f'font-weight="600" text-anchor="middle" fill="#ffffff">{gu}</text>')
    frame_close(p, notes, g)
    return Image("district-map", "\n".join(p), dp["label"])


# ── 2) 지수 비교 막대 ────────────────────────────────────────

_RATE_RE = re.compile(r"(감소|증가|상승|하락)율")


def index_comparison(dp: dict, date: str, extra: dict | None = None) -> Image | None:
    """증감률 하나뿐일 때, 연도별 값을 지어내지 않고 지수로만 비교한다."""
    m = _RATE_RE.search(dp.get("label", ""))
    if not m or "%" not in dp.get("unit", ""):
        return None
    pct = _num(dp)
    if pct is None or not 0 < abs(pct) <= 100:
        return None
    down = m.group(1) in ("감소", "하락")
    before, after = 100.0, round(100 - pct if down else 100 + pct)
    period = dp.get("period", "") or "이전"

    notes = [
        f'※ 보도된 {m.group(1)}율({dp["value"]}{dp["unit"]})로 지수화한 값입니다. '
        f'구간별 실제 수치는 보도에 제시되지 않았습니다.',
        (_source_line(dp, date) + (f' — {dp["context"]}' if dp.get("context") else "")).strip(),
    ]
    w = 1000
    title = dp["label"].replace(m.group(0), "").strip() or dp["label"]
    sub = f"{period} 전을 100으로 둔 지수 비교"
    h = card_height(w, f"{title}, {period} 사이", sub, 160 + 84 + 56 + 30, notes)
    p, g = frame_open(w, h, title=f"{title}, {period} 사이", subtitle=sub,
                      channel=_channel(extra), date=date)
    x, y = g["x"], g["top"]

    # 변화량을 먼저 크게. 색만으로 뜻을 전하지 않도록 화살표와 글자를 함께 둔다.
    p.append(f'<text x="{x}" y="{y + 62}" font-size="76" font-weight="800" '
             f'fill="{CRITICAL if down else GOOD}">{"▼" if down else "▲"} '
             f'{esc(dp["value"])}{esc(dp["unit"])}'
             f'<tspan font-size="30" font-weight="500" fill="{INK_2}"> {m.group(1)}</tspan></text>')
    p.append(f'<text x="{x}" y="{y + 100}" font-size="19" fill="{MUTED}">{esc(dp["label"])}</text>')

    bx = x + 150
    bw = g["inner"] - 150 - 70
    scale = bw / max(before, after)
    top = y + 160
    for i, (name, val) in enumerate([(f"{period} 전", before), ("현재", after)]):
        by = top + i * 84
        width = val * scale
        p.append(f'<text x="{bx - 20}" y="{by + 38}" font-size="21" text-anchor="end" '
                 f'fill="{INK_2}">{esc(name)}</text>')
        dark = i == 0
        p.append(f'<path d="{bar_path(bx, by, width, 56)}" fill="{BLUE if dark else BLUE_SOFT}"/>')
        inside = width > 96
        lx = bx + width - 18 if inside else bx + width + 16
        # 옅은 막대 위에 흰 글씨를 얹으면 읽히지 않는다. 막대 색에 따라 글자색을 고른다.
        color = ("#ffffff" if dark else INK) if inside else INK
        p.append(f'<text x="{lx:g}" y="{by + 38}" font-size="26" font-weight="700" '
                 f'text-anchor="{"end" if inside else "start"}" fill="{color}">{val:g}</text>')
    p.append(f'<line x1="{bx}" y1="{top - 8}" x2="{bx}" y2="{top + 84 + 56 + 8}" '
             f'stroke="{BASELINE}" stroke-width="2"/>')
    frame_close(p, notes, g)
    return Image("index-comparison", "\n".join(p), dp["label"])
# ── 시계열 (이력이 쌓인 지표만) ─────────────────────────────

SERIES_MIN_POINTS = 3
SERIES_SIMILARITY = 0.6


def _same_metric(a: dict, b: dict) -> bool:
    """단위가 같고 라벨이 충분히 비슷하면 같은 지표로 본다. 라벨은 LLM 이 매일 조금씩 다르게 쓴다."""
    from .cluster import similarity
    if (a.get("unit") or "") != (b.get("unit") or ""):
        return False
    la, lb = a.get("label", ""), b.get("label", "")
    return la == lb or similarity(la, lb) >= SERIES_SIMILARITY


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """1·2·5 배수로 떨어지는 눈금값. 0.0344 같은 숫자를 축에 적지 않으려는 것."""
    import math

    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(count - 1, 1)
    power = math.floor(math.log10(raw)) if raw > 0 else 0
    base = 10 ** power
    step = next((m * base for m in (1, 2, 2.5, 5, 10) if m * base >= raw), 10 * base)
    # 데이터 범위를 반드시 덮도록 아래·위로 넉넉히 잡는다 (선이 눈금 밖으로 나가면 안 된다)
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    steps = max(int(round((end - start) / step)), 1)
    return [round(start + i * step, 10) for i in range(steps + 1)]


def series_for(dp: dict, history: list[dict]) -> list[tuple[str, float]]:
    """이력에서 같은 지표의 (날짜, 값) 을 날짜순으로 뽑는다. 하루에 여러 건이면 첫 건."""
    points: dict[str, float] = {}
    for row in history:
        if not _same_metric(dp, row):
            continue
        v = _num(row)
        if v is None or row.get("date") in points:
            continue
        points[row["date"]] = v
    return sorted(points.items())


def time_series(dp: dict, date: str, extra: dict | None = None) -> Image | None:
    """같은 지표가 사흘 이상 쌓였을 때만 추이를 그린다. 쌓이지 않은 날은 그리지 않는다."""
    points = series_for(dp, (extra or {}).get("history") or [])
    if len(points) < SERIES_MIN_POINTS:
        return None
    if date and date not in dict(points):
        return None                       # 오늘 값이 없는 지표의 옛 추이는 오늘 그림이 아니다

    notes = [
        "※ 매일 기사에서 뽑힌 값을 그대로 이은 것입니다. 발표 기관·기준이 날마다 다를 수 있습니다.",
        _source_line(dp, date),
    ]
    w = 1000
    unit = dp.get("unit") or ""
    sub = f'{points[0][0]} ~ {points[-1][0]} · {len(points)}일치' + (f" · 단위 {unit}" if unit else "")
    h = card_height(w, dp["label"], sub, 30 + 330 + 60, notes)
    p, g = frame_open(w, h, title=dp["label"], subtitle=sub, channel=_channel(extra), date=date)

    px0, px1 = g["x"] + 56, g["x"] + g["inner"]
    py0, py1 = g["top"] + 30, g["top"] + 330
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.18
    ticks = nice_ticks(lo - pad, hi + pad)
    lo, hi = min(ticks), max(ticks)

    def sx(i: int) -> float:
        return px0 + (px1 - px0) * (i / max(len(points) - 1, 1))

    def sy(v: float) -> float:
        return py1 - (py1 - py0) * ((v - lo) / (hi - lo))

    for v in ticks:                                     # 눈금선은 뒤로 물러나게
        y = sy(v)
        p.append(f'<line x1="{px0}" y1="{y:.1f}" x2="{px1}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px0 - 14}" y="{y + 5:.1f}" font-size="15" text-anchor="end" '
                 f'fill="{MUTED}">{v:g}</text>')
    if lo < 0 < hi:
        p.append(f'<line x1="{px0}" y1="{sy(0):.1f}" x2="{px1}" y2="{sy(0):.1f}" '
                 f'stroke="{BASELINE}" stroke-width="1.5"/>')

    # 선 아래를 옅게 채워 추이 방향이 한눈에 들어오게
    area = (f'M{sx(0):.1f},{py1:.1f} '
            + " ".join(f'L{sx(i):.1f},{sy(v):.1f}' for i, (_, v) in enumerate(points))
            + f' L{sx(len(points) - 1):.1f},{py1:.1f} Z')
    p.append(f'<path d="{area}" fill="{BLUE_SOFT}" opacity="0.55"/>')
    line = " ".join(f'{"M" if i == 0 else "L"}{sx(i):.1f},{sy(v):.1f}' for i, (_, v) in enumerate(points))
    p.append(f'<path d="{line}" fill="none" stroke="{BLUE}" stroke-width="3" '
             f'stroke-linejoin="round" stroke-linecap="round"/>')
    p.append(f'<line x1="{px0}" y1="{py1:.1f}" x2="{px1}" y2="{py1:.1f}" stroke="{BASELINE}" stroke-width="1.5"/>')

    for i, (d, v) in enumerate(points):
        last = i == len(points) - 1
        p.append(f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="{7 if last else 5}" '
                 f'fill="{BLUE}" stroke="{CARD}" stroke-width="2.5"/>')
        # 날짜는 월-일만. 점이 많으면 처음·끝·중간만 적어 겹침을 막는다.
        if len(points) <= 8 or i in (0, len(points) - 1, len(points) // 2):
            p.append(f'<text x="{sx(i):.1f}" y="{py1 + 30}" font-size="15" text-anchor="middle" '
                     f'fill="{MUTED}">{esc(d[5:])}</text>')
    for i in (0, len(points) - 1):                      # 직접 라벨은 처음과 끝에만
        d, v = points[i]
        p.append(f'<text x="{sx(i):.1f}" y="{sy(v) - 18:.1f}" font-size="19" font-weight="700" '
                 f'text-anchor="{"start" if i == 0 else "end"}" fill="{INK}">{v:g}{esc(unit)}</text>')
    frame_close(p, notes, g)
    return Image("time-series", "\n".join(p), dp["label"])
# ── 3) 수치 카드 (기본형) ────────────────────────────────────


def stat_card(dp: dict, date: str, extra: dict | None = None) -> Image | None:
    """어떤 수치든 받아 큰 숫자 카드로 만든다. 마지막 수단."""
    if not dp.get("value"):
        return None
    w = 1000
    notes = []
    if dp.get("context"):
        notes += [f"※ {ln}" if i == 0 else f"　 {ln}"
                  for i, ln in enumerate(wrap(dp["context"], 17, w - (M + P) * 2))]
    if src := _source_line(dp, date):
        notes.append(src)

    # 제목 줄 수와 각주 줄 수에 맞춰 높이를 잡는다. 고정하면 아래가 비거나 넘친다.
    content_h = 190 if dp.get("period") else 150
    h = card_height(w, dp["label"], "", content_h, notes)

    p, g = frame_open(w, h, title=dp["label"], subtitle="", channel=_channel(extra), date=date)
    x, y = g["x"], g["top"]

    # 큰 숫자 — 단위는 한 단계 작게 붙여 숫자가 먼저 읽히게
    p.append(f'<text x="{x}" y="{y + 96}" font-size="112" font-weight="800" fill="{INK}">'
             f'{esc(dp["value"])}'
             f'<tspan font-size="46" font-weight="500" fill="{INK_2}">{esc(dp.get("unit", ""))}</tspan></text>')
    # 숫자 아래 짧은 강조선. 카드에 무게중심을 준다.
    p.append(f'<rect x="{x}" y="{y + 122}" width="96" height="6" rx="3" fill="{BLUE}"/>')
    if dp.get("period"):
        p.append(chip(x, y + 150, f'기준 {dp["period"]}'))
    frame_close(p, notes, g)
    return Image("stat-card", "\n".join(p), dp["label"])
SPECIFIC = (time_series, seoul_district_map, index_comparison)
FALLBACK = (stat_card,)
GENERATORS = SPECIFIC + FALLBACK


# ── 조립 ─────────────────────────────────────────────────────


# ── 실거래가 그림 (정부 통계에서 직접 받은 값) ──────────────
#
# 뉴스에서 뽑은 수치가 아니라 우리가 직접 센 값이라, 각주에 출처와 집계 방식을 반드시 적습니다.


def trade_volume_bar(data: dict, date: str, extra: dict | None = None) -> "Image | None":
    """지역별 아파트 매매 거래 건수. 전달과의 차이를 막대 옆에 함께 적는다."""
    rows = [r for r in (data.get("districts") or []) if r["now"]["count"]][:8]
    if len(rows) < 2:
        return None
    label = data.get("month_label", "")
    title = f"{label} 아파트 매매 거래 건수"
    sub = f"{data.get('before_label', '')} 대비 · 신고분 기준"
    notes = [
        "※ 국토교통부 실거래가 신고 자료를 직접 집계했습니다. 해제(계약 취소) 신고분은 뺐습니다.",
        f"출처: 국토교통부 실거래가 공개시스템 · {date} 집계",
    ]
    w = 1000
    row_h = 60
    content = len(rows) * row_h + 30
    h = card_height(w, title, sub, content, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    name_w = 96
    bx = x + name_w
    bw = inner - name_w - 150
    top = max(r["now"]["count"] for r in rows)
    scale = bw / top if top else 0

    for i, r in enumerate(rows):
        by = y + i * row_h
        width = r["now"]["count"] * scale
        p.append(f'<text x="{bx - 18}" y="{by + 32}" font-size="21" text-anchor="end" '
                 f'fill="{INK_2}">{esc(r["name"])}</text>')
        p.append(f'<path d="{bar_path(bx, by, max(width, 3), 44)}" fill="{BLUE}"/>')
        p.append(f'<text x="{bx + width + 16:g}" y="{by + 32}" font-size="24" '
                 f'font-weight="700" fill="{INK}">{r["now"]["count"]}건</text>')
        change = r["change"]
        if change:
            cx = bx + width + 16 + text_width(f'{r["now"]["count"]}건', 24) + 14
            p.append(f'<text x="{cx:g}" y="{by + 32}" font-size="20" '
                     f'fill="{GOOD if change > 0 else CRITICAL}">'
                     f'{"▲" if change > 0 else "▼"}{abs(change)}</text>')
    p.append(f'<line x1="{bx}" y1="{y - 8}" x2="{bx}" y2="{y + len(rows) * row_h - 8}" '
             f'stroke="{BASELINE}" stroke-width="2"/>')
    frame_close(p, notes, g)
    return Image("stats-volume", "\n".join(p), title)


def price_index_line(series: list[dict], date: str, extra: dict | None = None) -> "Image | None":
    """한국부동산원 주간 아파트 매매가격지수 추이."""
    points = [s for s in series if s.get("value") is not None]
    if len(points) < 3:
        return None
    region = points[0].get("region", "") or "전국"
    title = f"주간 아파트 매매가격지수 · {region}"
    sub = f"{points[0].get('when') or points[0]['time']} ~ {points[-1].get('when') or points[-1]['time']}"
    change = points[-1]["value"] - points[0]["value"]
    notes = [
        "※ 한국부동산원이 매주 발표하는 지수입니다. 값 자체가 가격이 아니라 기준 시점 대비 상대값입니다.",
        f"출처: 한국부동산원 R-ONE · {date} 조회",
    ]
    w, plot_h = 1000, 300
    h = card_height(w, title, sub, plot_h + 96, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    values = [s["value"] for s in points]
    ticks = nice_ticks(min(values), max(values))
    lo, hi = ticks[0], ticks[-1]
    axis_w = 74
    px, pw = x + axis_w, inner - axis_w
    py = y + 54

    def sy(v: float) -> float:
        return py + plot_h - (v - lo) / (hi - lo) * plot_h

    for t in ticks:
        ty = sy(t)
        p.append(f'<line x1="{px}" y1="{ty:g}" x2="{px + pw}" y2="{ty:g}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px - 14}" y="{ty + 6:g}" font-size="18" text-anchor="end" '
                 f'fill="{MUTED}">{t:g}</text>')

    step = pw / max(len(points) - 1, 1)
    coords = [(px + i * step, sy(v)) for i, v in enumerate(values)]
    p.append('<polyline fill="none" stroke="' + BLUE + '" stroke-width="3" '
             'stroke-linejoin="round" points="'
             + " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords) + '"/>')
    for cx, cy in coords:
        p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{BLUE}" '
                 f'stroke="{CARD}" stroke-width="2"/>')

    # 처음과 끝만 값을 적는다. 모든 점에 숫자를 붙이면 읽히지 않는다.
    for idx, anchor in ((0, "start"), (len(points) - 1, "end")):
        cx, cy = coords[idx]
        p.append(f'<text x="{cx:.1f}" y="{cy - 18:.1f}" font-size="22" font-weight="700" '
                 f'text-anchor="{anchor}" fill="{INK}">{values[idx]:.2f}</text>')
    for idx, anchor in ((0, "start"), (len(points) - 1, "end")):
        cx = coords[idx][0]
        when = points[idx].get("when") or points[idx]["time"]
        p.append(f'<text x="{cx:.1f}" y="{py + plot_h + 30:g}" font-size="18" '
                 f'text-anchor="{anchor}" fill="{MUTED}">{esc(when)}</text>')

    p.append(f'<text x="{x}" y="{y + 30}" font-size="26" font-weight="700" '
             f'fill="{GOOD if change > 0 else (CRITICAL if change < 0 else INK)}">'
             f'{"▲" if change > 0 else ("▼" if change < 0 else "―")} {abs(change):.2f}'
             f'<tspan font-size="20" font-weight="500" fill="{INK_2}"> '
             f'{len(points)}주 동안</tspan></text>')
    frame_close(p, notes, g)
    return Image("stats-index", "\n".join(p), title)


def build(datapoints: list[dict], date: str, headline: str = "", limit: int = 3,
          history: list[dict] | None = None) -> list[Image]:
    """수치 목록에서 그릴 수 있는 그림을 최대 limit 개 만든다.

    특수 형태(지도·지수)를 먼저 훑고, 자리가 남으면 기본 카드로 채운다.
    앞에서부터 순서대로 집으면 정보량 적은 카드가 자리를 차지해 버린다.
    """
    built: list[Image] = []
    used_rows: set[int] = set()
    slug_counts: dict[str, int] = {}

    for generators in (SPECIFIC, FALLBACK):
        for row, dp in enumerate(datapoints):
            if len(built) >= limit:
                return built
            if row in used_rows:
                continue
            for gen in generators:
                try:
                    img = gen(dp, date, {"subtitle": headline, "history": history or []})
                except Exception:        # 그림 하나가 파이프라인 전체를 죽이지 않게 한다
                    log.exception("그림 생성 실패: %s", dp.get("label"))
                    continue
                if img is None:
                    continue
                n = slug_counts.get(img.slug, 0) + 1
                slug_counts[img.slug] = n
                if n > 1:
                    img.slug = f"{img.slug}-{n}"
                built.append(img)
                used_rows.add(row)
                break
    return built


# ── 본문 자리에 맞춘 생성 ───────────────────────────────────

SLOT_MATCH_SIMILARITY = 0.6


def _first_image(dp: dict, date: str, extra: dict) -> Image | None:
    """한 수치에 가장 알맞은 형태 하나. 특수 형태 → 기본 카드 순."""
    for gen in GENERATORS:
        try:
            img = gen(dp, date, extra)
        except Exception:
            log.exception("그림 생성 실패: %s", dp.get("label"))
            continue
        if img is not None:
            return img
    return None


def match_datapoint(label: str, datapoints: list[dict]) -> int | None:
    """블로그가 적어 준 라벨과 가장 가까운 수치의 인덱스. 정확히 같으면 그것, 아니면 비슷한 것."""
    from .cluster import similarity
    if not label:
        return None
    for i, dp in enumerate(datapoints):
        if dp.get("label") == label:
            return i
    best, best_score = None, 0.0
    for i, dp in enumerate(datapoints):
        score = similarity(label, dp.get("label", ""))
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= SLOT_MATCH_SIMILARITY else None


def build_for_slots(datapoints: list[dict], date: str, slot_labels: list[str], *,
                    headline: str = "", history: list[dict] | None = None,
                    limit: int = 3) -> tuple[dict[int, Image], list[Image]]:
    """블로그의 이미지 자리(라벨 목록)에 맞춰 그림을 만든다.

    돌려주는 것: (자리 번호 → 그림, 자리 밖 여분 그림). 자리 그림은 파일명이
    `1-district-map` 처럼 자리 번호로 시작해 본문과 확정적으로 짝지어진다.
    라벨이 비었거나 맞는 수치가 없는 자리는 비워 두고(사진 자리), 남는 장수는
    아직 안 쓴 수치로 채운다.
    """
    extra = {"subtitle": headline, "history": history or []}
    by_slot: dict[int, Image] = {}
    used: set[int] = set()
    for slot_no, label in enumerate(slot_labels, start=1):
        idx = match_datapoint(label, datapoints)
        if idx is None or idx in used:
            continue
        img = _first_image(datapoints[idx], date, extra)
        if img is None:
            continue
        img.slug = f"{slot_no}-{img.slug}"
        by_slot[slot_no] = img
        used.add(idx)

    remaining = max(0, limit - len(by_slot))
    leftovers = [dp for i, dp in enumerate(datapoints) if i not in used]
    extras = build(leftovers, date, headline=headline, limit=remaining, history=history) if remaining else []
    return by_slot, extras


# ── 썸네일 ───────────────────────────────────────────────────


def thumbnail(text: str, *, sub: str = "", channel: str = "", date: str = "",
              size: tuple[int, int] = (1280, 720), badge: str = "") -> Image:
    """큰 글씨 한 줄(최대 3줄)짜리 표지. 롱폼 1280×720, 쇼츠 1080×1920.
    badge 는 오른쪽 위 파란 알약 — 오늘의 첫 번째 핵심 수치."""
    w, h = size
    portrait = h > w
    margin = 80 if portrait else 72
    big = 150 if portrait else 108
    lines = wrap(text, big, w - margin * 2)
    while len(lines) > 3 and big > 60:           # 세 줄에 못 들어가면 글씨를 줄인다
        big -= 12
        lines = wrap(text, big, w - margin * 2)
    lines = lines[:3]
    line_h = big * 1.18
    block_h = line_h * len(lines)
    # 부제까지 한 덩어리로 보고 가운데를 잡는다. 제목만 기준으로 잡으면 부제가 아래 띠를 침범한다.
    sub_size = 44 if portrait else 36
    sub_lines = wrap(sub, sub_size, w - margin * 2 - 44)[:2] if sub else []
    sub_h = (40 + len(sub_lines) * 58) if sub_lines else 0
    base_y = (h * (0.50 if portrait else 0.46)) - (block_h + sub_h) / 2 + big * 0.85

    p = svg_open(w, h)
    # 왼쪽 세로 강조 막대 + 상단 채널명, 하단 날짜. 사진 없이도 표지로 읽히게.
    p.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="{SURFACE}"/>')
    p.append(f'<rect x="{margin}" y="{base_y - big * 0.85:.0f}" width="14" height="{block_h:.0f}" rx="4" fill="{BLUE}"/>')
    for i, line in enumerate(lines):
        p.append(f'<text x="{margin + 44}" y="{base_y + i * line_h:.0f}" font-size="{big}" '
                 f'font-weight="800" fill="{INK}">{esc(line)}</text>')
    if sub_lines:
        sub_top = base_y + block_h + 40
        # 아래 띠를 침범하면 부제를 생략한다. 겹쳐 찍느니 없는 편이 낫다.
        # sub_top 은 첫 줄의 기준선이므로 마지막 줄의 아래끝만 보면 된다.
        last_bottom = sub_top + (len(sub_lines) - 1) * 58 + sub_size * 0.3
        if last_bottom < h - (96 if portrait else 76) - 12:
            for i, line in enumerate(sub_lines):
                p.append(f'<text x="{margin + 44}" y="{sub_top + i * 58:.0f}" '
                         f'font-size="{sub_size}" fill="{INK_2}">{esc(line)}</text>')
    # 아래 띠 — 채널명·날짜를 얹어 표지처럼 보이게 한다
    band = 96 if portrait else 76
    p.append(f'<rect x="0" y="{h - band}" width="{w}" height="{band}" fill="{INK}"/>')
    if channel:
        p.append(f'<text x="{margin}" y="{h - band / 2 + 12:g}" font-size="{34 if portrait else 28}" '
                 f'font-weight="700" fill="#ffffff">{esc(channel)}</text>')
    if date:
        p.append(f'<text x="{w - margin}" y="{h - band / 2 + 12:g}" font-size="{30 if portrait else 25}" '
                 f'text-anchor="end" fill="#c9cdd2">{esc(date)}</text>')
    if badge:
        bsize = 52 if portrait else 40
        bw = text_width(badge, bsize) + bsize * 1.2
        bh = bsize * 1.7
        bx, by = w - margin - bw, margin - bh * 0.45
        p.append(f'<rect x="{bx:.0f}" y="{by:.0f}" width="{bw:.0f}" height="{bh:.0f}" rx="{bh / 2:.0f}" fill="{BLUE}"/>')
        p.append(f'<text x="{bx + bw / 2:.0f}" y="{by + bh * 0.68:.0f}" font-size="{bsize}" font-weight="800" '
                 f'text-anchor="middle" fill="#ffffff">{esc(badge)}</text>')
    p.append("</svg>")
    return Image("thumb", "\n".join(p), text)


# ── PNG 변환 (있으면 덤) ─────────────────────────────────────

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    for path in _CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _platform_flags() -> list[str]:
    """리눅스 CI(깃허브 러너)에서 헤드리스 크롬이 필요로 하는 플래그."""
    import os
    import sys
    if sys.platform == "darwin":
        return []
    flags = ["--disable-dev-shm-usage"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        flags.append("--no-sandbox")       # 루트로 돌 때만. 러너는 보통 runner 계정이다
    return flags


def svg_to_png(svg_path: Path, png_path: Path, scale: int = 2) -> bool:
    """헤드리스 브라우저로 PNG 를 뽑는다. 브라우저가 없으면 조용히 건너뛴다."""
    browser = find_browser()
    if not browser:
        return False
    head = svg_path.read_text(encoding="utf-8")[:400]
    m = re.search(r'width="(\d+)"\s+height="(\d+)"', head)
    if not m:
        return False
    w, hgt = m.group(1), m.group(2)
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "wrap.html"
        shutil.copy(svg_path, Path(tmp) / svg_path.name)
        wrapper.write_text(
            "<style>html,body{margin:0;padding:0}img{display:block}</style>"
            f'<img src="{svg_path.name}" width="{w}" height="{hgt}">',
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [browser, "--headless", "--disable-gpu", "--hide-scrollbars",
                 *_platform_flags(),
                 f"--force-device-scale-factor={scale}", f"--window-size={w},{hgt}",
                 f"--screenshot={png_path}", wrapper.as_uri()],
                check=True, capture_output=True, timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("PNG 변환 실패 (%s). SVG 는 그대로 남습니다.", exc)
            return False
    return png_path.exists()
