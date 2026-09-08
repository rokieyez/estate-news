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
ORANGE = "#a9560a"      # 두 번째 계열색. 파랑↔주황은 색각 이상에서도 구분된다
# 크기를 나타내는 단계색. 한 가지 색의 옅음→짙음이라 순서가 저절로 읽힌다(무지개는 안 된다).
# 앞 넷은 검정 글씨(대비 6.7:1 이상), 뒤 둘은 흰 글씨(5.4:1 이상)를 얹는다.
BLUE_RAMP = ("#eaf2fe", "#cde2fb", "#9dc6f5", "#5a9ae4", "#256abf", "#173f77")
RAMP_INK = (INK, INK, INK, INK, "#ffffff", "#ffffff")
CRITICAL = "#d03b3b"
GOOD = "#006300"
# 맥(Apple SD Gothic Neo) → 리눅스 러너(Noto Sans CJK KR, 워크플로에서 설치) → 그 외 순서.
# 러너에 한글 글꼴이 없으면 PNG 의 한글이 전부 네모로 깨진다 (2026-09-07 실제로 그랬음).
FONT = "'Apple SD Gothic Neo','Noto Sans CJK KR','Noto Sans KR','NanumGothic',system-ui,-apple-system,sans-serif"

# 서울 25개 자치구의 상대 위치 도식. 칸의 크기·모양은 실제 면적과 무관하지만, **줄과 칸의
# 순서는 실제 위경도 순서를 지킵니다** — 같은 세로줄은 동서로 비슷한 자리, 같은 가로줄은
# 남북으로 비슷한 자리입니다.
#
# 아래 중심 좌표(자치구청 기준)로 남북 8줄·동서 7칸에 나눠 배치했습니다. 예전 도식은
# 여섯 곳이 크게 어긋나 있었습니다 (2026-09-07 측정): 관악이 동작 옆이 아니라 오른쪽으로
# 아홉 칸 밀려 있었고, 광진·송파가 실제보다 다섯 자리 북쪽에, 강북은 다섯 칸 서쪽에
# 있었습니다. 자리를 옮길 일이 생기면 이 좌표부터 보세요.
#
#   도봉 37.668,127.032 · 노원 37.654,127.075 · 강북 37.640,127.011 · 은평 37.618,126.928
#   성북 37.605,127.018 · 중랑 37.598,127.093 · 종로 37.595,126.978 · 서대문 37.577,126.937
#   동대문 37.575,127.045 · 마포 37.560,126.909 · 중구 37.560,126.996 · 강서 37.556,126.824
#   성동 37.550,127.041 · 강동 37.549,127.147 · 광진 37.538,127.083 · 용산 37.532,126.981
#   양천 37.524,126.861 · 영등포 37.522,126.910 · 동작 37.505,126.943 · 송파 37.505,127.115
#   강남 37.497,127.063 · 구로 37.494,126.858 · 서초 37.475,127.032 · 관악 37.470,126.947
#   금천 37.460,126.898
SEOUL_LAYOUT: list[dict[int, str]] = [
    {4: "도봉", 5: "노원"},
    {2: "은평", 4: "강북"},
    {2: "서대문", 3: "종로", 4: "성북", 5: "동대문", 6: "중랑"},
    {0: "강서", 1: "마포", 3: "중구", 5: "성동", 6: "강동"},
    {0: "양천", 1: "영등포", 3: "용산", 6: "광진"},
    {0: "구로", 2: "동작", 5: "강남", 6: "송파"},
    {1: "금천", 2: "관악", 4: "서초"},
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

    cols = max(c for row in SEOUL_LAYOUT for c in row) + 1
    w = (M + P) * 2 + cols * tw + (cols - 1) * gap
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



def _quantile_bins(values: list[float], groups: int = 5) -> list[float]:
    """값을 같은 수씩 나누는 경계. 다섯 칸에 구가 고르게 들어가게.

    값 범위를 그냥 5등분하면 거래가 몰린 한두 구 때문에 나머지 스물이 전부 맨 아래 칸에
    들어가 지도가 한 가지 색이 됩니다. 대신 순위로 나누고, 각 칸의 실제 값 범위를 범례에
    적어 무엇을 나눈 것인지 보이게 합니다.
    """
    xs = sorted(values)
    if not xs:
        return []
    return [xs[min(int(len(xs) * i / groups), len(xs) - 1)] for i in range(1, groups)]


# 지도로 그릴 수 있는 값들. 칸에 적는 글자 모양이 달라서 여기에 모아 둔다.
MAP_METRICS = {
    "count": {
        "key": "map", "slug": "stats-map",
        "title": "{label} 서울 자치구별 아파트 매매 거래",
        "sub": "신고분 기준 · 해제분 제외",
        "fmt": lambda v: f"{v:,}",
        "legend": lambda a, b: (f"{a:,.0f}~{b:,.0f}건" if b > a else f"{a:,.0f}건"),
        "note": "※ 색은 다섯 칸에 구가 고르게 들어가도록 순위로 나눴습니다. 칸마다 실제 건수를 적었습니다.",
    },
    "jeonse": {
        "key": "map_jeonse", "slug": "stats-map-jeonse",
        "title": "{label} 서울 자치구별 전세가율",
        "sub": "같은 단지·같은 면적의 전세 보증금 ÷ 매매가 (가운뎃값)",
        "fmt": lambda v: f"{v:.1f}%",
        "legend": lambda a, b: (f"{a:.0f}~{b:.0f}%" if b - a >= 1 else f"{a:.1f}%"),
        "note": "※ 월세가 붙은 계약과 갱신 계약은 뺐습니다. 짝지을 단지가 2곳 미만인 구는 비워 두었습니다.",
    },
}


def district_choropleth(data: dict, date: str, extra: dict | None = None, *,
                        metric: str = "count") -> "Image | None":
    """서울 자치구 도식에 값의 크기를 색 농담으로 칠한다.

    스물다섯 칸이 다 차야 지도로 읽히므로, 절반만 있으면 그리지 않습니다.
    색만으로 구분하지 않도록 칸마다 숫자를 함께 적습니다.
    """
    spec = MAP_METRICS.get(metric)
    if not spec:
        return None
    counts = {k: v for k, v in (data.get(spec["key"]) or {}).items() if v}
    if len(counts) < 18:
        return None
    label = data.get("month_label", "")
    title = spec["title"].format(label=label)
    sub = spec["sub"]
    notes = [
        "※ 실제 지형이 아닌 위치 도식입니다. 칸의 크기는 면적·인구와 무관합니다.",
        spec["note"],
        f"출처: 국토교통부 실거래가 공개시스템 · {date} 집계",
    ]

    tw, th, gap = 128, 92, 10
    ramp = BLUE_RAMP[1:]                    # 맨 옅은 단계는 '자료 없음' 과 헷갈려 뺀다
    inks = RAMP_INK[1:]
    edges = _quantile_bins(list(counts.values()), len(ramp))

    def step(value: float) -> int:
        for i, edge in enumerate(edges):
            if value <= edge:
                return i
        return len(ramp) - 1

    cols = max(c for row in SEOUL_LAYOUT for c in row) + 1
    w = (M + P) * 2 + cols * tw + (cols - 1) * gap
    rows = len(SEOUL_LAYOUT)
    content = 62 + rows * (th + gap) - gap
    h = card_height(w, title, sub, content, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y = g["x"], g["top"]

    # 범례 — 단계마다 그 칸에 실제로 들어간 값의 범위를 적는다
    lo = min(counts.values())
    bounds = [lo] + edges + [max(counts.values())]
    lw = g["inner"] / len(ramp)
    for i, color in enumerate(ramp):
        lx = x + i * lw
        p.append(f'<rect x="{lx:g}" y="{y - 16}" width="26" height="18" rx="4" fill="{color}"/>')
        left, right = bounds[i], bounds[i + 1]
        span = spec["legend"](left, right)
        p.append(f'<text x="{lx + 33:g}" y="{y - 1}" font-size="17" fill="{INK_2}">{esc(span)}</text>')

    map_top = y + 62
    for r, row in enumerate(SEOUL_LAYOUT):
        for c, gu in row.items():
            name = gu if gu.endswith("구") else f"{gu}구"
            gx, gy = x + c * (tw + gap), map_top + r * (th + gap)
            value = counts.get(name)
            if value is None:
                p.append(f'<rect x="{gx}" y="{gy}" width="{tw}" height="{th}" rx="10" '
                         f'fill="{SURFACE}" stroke="{BASELINE}" stroke-width="2" stroke-dasharray="6 4"/>')
                p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 - 2:g}" font-size="22" '
                         f'text-anchor="middle" fill="{INK_2}">{gu}</text>')
                p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 + 24:g}" font-size="17" '
                         f'text-anchor="middle" fill="{MUTED}">자료 없음</text>')
                continue
            i = step(value)
            p.append(f'<rect x="{gx}" y="{gy}" width="{tw}" height="{th}" rx="10" fill="{ramp[i]}"/>')
            p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 - 4:g}" font-size="22" '
                     f'text-anchor="middle" fill="{inks[i]}">{gu}</text>')
            p.append(f'<text x="{gx + tw / 2:g}" y="{gy + th / 2 + 26:g}" font-size="25" '
                     f'font-weight="700" text-anchor="middle" fill="{inks[i]}">'
                     f'{esc(spec["fmt"](value))}</text>')
    frame_close(p, notes, g)
    return Image(spec["slug"], "\n".join(p), title)


def price_index_line(series: dict, date: str, extra: dict | None = None) -> "Image | None":
    """한국부동산원 주간 지수 추이. 매매·전세를 한 판에 겹쳐 그린다."""
    lines = [(name, [s for s in rows if s.get("value") is not None])
             for name, rows in (series or {}).items()]
    lines = [(name, rows) for name, rows in lines if len(rows) >= 3]
    if not lines:
        return None
    first = lines[0][1]
    region = first[0].get("region", "") or "전국"
    title = f"주간 아파트 가격지수 · {region}"
    sub = f"{first[0].get('when') or first[0]['time']} ~ {first[-1].get('when') or first[-1]['time']}"
    notes = [
        "※ 값 자체가 가격이 아니라 기준 시점 대비 상대값입니다. 두 선의 높낮이가 아니라 기울기를 보세요.",
        f"출처: 한국부동산원 R-ONE · {date} 조회",
    ]
    w, plot_h = 1000, 300
    h = card_height(w, title, sub, plot_h + 110, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    values = [v["value"] for _, rows in lines for v in rows]
    ticks = nice_ticks(min(values), max(values))
    lo, hi = ticks[0], ticks[-1]
    axis_w = 74
    px, pw = x + axis_w, inner - axis_w
    py = y + 64

    def sy(v: float) -> float:
        return py + plot_h - (v - lo) / (hi - lo) * plot_h

    for t in ticks:
        ty = sy(t)
        p.append(f'<line x1="{px}" y1="{ty:g}" x2="{px + pw}" y2="{ty:g}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px - 14}" y="{ty + 6:g}" font-size="18" text-anchor="end" '
                 f'fill="{MUTED}">{t:g}</text>')

    colors = [BLUE, ORANGE]
    legend_x = x
    for i, (name, rows) in enumerate(lines[:2]):
        color = colors[i]
        step = pw / max(len(rows) - 1, 1)
        coords = [(px + j * step, sy(v["value"])) for j, v in enumerate(rows)]
        p.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" '
                 'stroke-linejoin="round" points="'
                 + " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords) + '"/>')
        for cx, cy in coords:
            p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" '
                     f'stroke="{CARD}" stroke-width="2"/>')
        # 선 끝에 이름을 적으면 두 선이 붙은 날 서로 겹친다. 범례 한 곳에 이름·값·변화를 모은다.
        change = rows[-1]["value"] - rows[0]["value"]
        legend = (f'{name} {rows[-1]["value"]:.2f} '
                  f'{"▲" if change > 0 else ("▼" if change < 0 else "―")}{abs(change):.2f}')
        p.append(f'<rect x="{legend_x}" y="{y + 12}" width="14" height="14" rx="3" fill="{color}"/>')
        p.append(f'<text x="{legend_x + 22}" y="{y + 24}" font-size="20" fill="{INK_2}">'
                 f'{esc(name)} <tspan font-weight="700" fill="{INK}">'
                 f'{rows[-1]["value"]:.2f}</tspan> '
                 f'<tspan fill="{GOOD if change > 0 else (CRITICAL if change < 0 else INK_2)}">'
                 f'{"▲" if change > 0 else ("▼" if change < 0 else "―")}{abs(change):.2f}</tspan></text>')
        legend_x += 46 + text_width(legend, 20)

    for idx, anchor in ((0, "start"), (len(first) - 1, "end")):
        cx = px + idx * (pw / max(len(first) - 1, 1))
        when = first[idx].get("when") or first[idx]["time"]
        p.append(f'<text x="{cx:.1f}" y="{py + plot_h + 30:g}" font-size="18" '
                 f'text-anchor="{anchor}" fill="{MUTED}">{esc(when)}</text>')
    frame_close(p, notes, g)
    return Image("stats-index", "\n".join(p), title)


def jeonse_history_line(rows: list[dict], region: str, date: str,
                        extra: dict | None = None) -> "Image | None":
    """날마다 잰 전세가율 추이. 사흘 이상 쌓여야 그린다."""
    points = [r for r in rows if r.get("median") is not None]
    if len(points) < 3:
        return None
    title = f"{region} 전세가율 — 우리 집계 추이"
    sub = f"{points[0]['date']} ~ {points[-1]['date']} 집계 · 같은 단지·같은 면적 비교"
    notes = [
        "※ 전세 보증금 ÷ 매매가입니다. 월세가 붙은 계약과 갱신 계약은 뺐습니다. "
        "견준 단지 수가 적은 날은 값이 크게 흔들립니다.",
        "출처: 국토교통부 실거래가 공개시스템 · 날마다 직접 집계",
    ]
    w, plot_h = 1000, 260
    h = card_height(w, title, sub, plot_h + 96, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    values = [r["median"] for r in points]
    ticks = nice_ticks(min(values), max(values))
    lo, hi = ticks[0], ticks[-1]
    axis_w = 74
    px, pw = x + axis_w, inner - axis_w
    py = y + 40

    def sy(v: float) -> float:
        return py + plot_h - (v - lo) / (hi - lo) * plot_h

    for t in ticks:
        ty = sy(t)
        p.append(f'<line x1="{px}" y1="{ty:g}" x2="{px + pw}" y2="{ty:g}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px - 14}" y="{ty + 6:g}" font-size="18" text-anchor="end" '
                 f'fill="{MUTED}">{t:g}%</text>')

    step = pw / max(len(points) - 1, 1)
    coords = [(px + i * step, sy(v)) for i, v in enumerate(values)]
    p.append(f'<polyline fill="none" stroke="{ORANGE}" stroke-width="3" '
             'stroke-linejoin="round" points="'
             + " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords) + '"/>')
    for (cx, cy), row in zip(coords, points):
        # 표본이 적은 날은 점을 작게 — 같은 굵기로 그리면 똑같이 믿게 된다
        r = 5 if row.get("count", 0) >= 20 else 3
        p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{ORANGE}" '
                 f'stroke="{CARD}" stroke-width="2"/>')
    ex, ey = coords[-1]
    p.append(f'<text x="{ex:.1f}" y="{ey - 16:.1f}" font-size="24" font-weight="700" '
             f'text-anchor="end" fill="{INK}">{values[-1]:.1f}%</text>')
    for idx, anchor in ((0, "start"), (len(points) - 1, "end")):
        p.append(f'<text x="{coords[idx][0]:.1f}" y="{py + plot_h + 30:g}" font-size="18" '
                 f'text-anchor="{anchor}" fill="{MUTED}">{esc(points[idx]["date"])}</text>')
    frame_close(p, notes, g)
    return Image("stats-jeonse", "\n".join(p), title)


def trade_history_line(rows: list[dict], region: str, date: str,
                       extra: dict | None = None) -> "Image | None":
    """우리가 날마다 집계한 거래 건수 추이. 사흘 이상 쌓여야 그린다."""
    points = [r for r in rows if r.get("count") is not None]
    if len(points) < 3:
        return None
    title = f"{region} 아파트 매매 거래 건수 — 우리 집계 추이"
    sub = f"{points[0]['date']} ~ {points[-1]['date']} 집계"
    notes = [
        "※ 같은 달이라도 신고가 늦게 들어와 집계일마다 값이 조금씩 커집니다. "
        "가격 변화가 아니라 신고가 쌓이는 속도를 보는 그림입니다.",
        "출처: 국토교통부 실거래가 공개시스템 · 날마다 직접 집계",
    ]
    w, plot_h = 1000, 260
    h = card_height(w, title, sub, plot_h + 96, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    values = [r["count"] for r in points]
    ticks = nice_ticks(min(values), max(values))
    lo, hi = ticks[0], ticks[-1]
    axis_w = 74
    px, pw = x + axis_w, inner - axis_w
    py = y + 40

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
    p.append(f'<polyline fill="none" stroke="{BLUE}" stroke-width="3" '
             'stroke-linejoin="round" points="'
             + " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords) + '"/>')
    for cx, cy in coords:
        p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{BLUE}" '
                 f'stroke="{CARD}" stroke-width="2"/>')
    ex, ey = coords[-1]
    p.append(f'<text x="{ex:.1f}" y="{ey - 16:.1f}" font-size="24" font-weight="700" '
             f'text-anchor="end" fill="{INK}">{values[-1]}건</text>')
    for idx, anchor in ((0, "start"), (len(points) - 1, "end")):
        p.append(f'<text x="{coords[idx][0]:.1f}" y="{py + plot_h + 30:g}" font-size="18" '
                 f'text-anchor="{anchor}" fill="{MUTED}">{esc(points[idx]["date"])}</text>')
    frame_close(p, notes, g)
    return Image("stats-history", "\n".join(p), title)



def supply_line(item: dict, date: str, extra: dict | None = None) -> "Image | None":
    """공급 쪽 통계 한 가지의 월별 추이. 넉 달 이상 있어야 그린다.

    거래·가격과 달리 이 숫자는 **앞으로의 공급**을 말합니다. 미분양은 재고, 인허가·착공은
    1~2년 뒤 물량이라 각주에 성질을 밝힙니다 (인허가 원자료는 누계라 되돌린 값입니다).
    """
    rows = [r for r in (item or {}).get("rows", []) if r.get("value") is not None]
    if len(rows) < 4:
        return None
    unit = item.get("unit", "호")
    title = f"{item.get('name', '')} 추이 — {rows[-1]['label']}까지"
    sub = f"{rows[0]['label']} ~ {rows[-1]['label']} · 서울"
    kind = {"stock": "그 시점에 남아 있는 물량입니다(재고).",
            "cumulative": "원자료가 연초부터의 누계라 그 달치로 되돌린 값입니다.",
            }.get(item.get("mode", ""), "그 달 실적입니다.")
    notes = ["※ " + kind + (f" {item['note']}." if item.get("note") else ""),
             f"출처: 한국부동산원 R-ONE · {date} 조회"]

    w, plot_h = 1000, 250
    h = card_height(w, title, sub, plot_h + 96, notes)
    p, g = frame_open(w, h, title=title, subtitle=sub, channel=_channel(extra), date=date)
    x, y, inner = g["x"], g["top"], g["inner"]

    values = [r["value"] for r in rows]
    ticks = nice_ticks(min(min(values), 0), max(values))
    lo, hi = ticks[0], ticks[-1]
    axis_w = 92
    px, pw = x + axis_w, inner - axis_w
    py = y + 40

    def sy(v: float) -> float:
        return py + plot_h - (v - lo) / (hi - lo) * plot_h if hi > lo else py + plot_h

    for t in ticks:
        ty = sy(t)
        p.append(f'<line x1="{px}" y1="{ty:g}" x2="{px + pw}" y2="{ty:g}" '
                 f'stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px - 14}" y="{ty + 6:g}" font-size="18" text-anchor="end" '
                 f'fill="{MUTED}">{t:,.0f}</text>')

    step = pw / max(len(rows) - 1, 1)
    coords = [(px + i * step, sy(v)) for i, v in enumerate(values)]
    p.append(f'<polyline fill="none" stroke="{ORANGE}" stroke-width="3" '
             'stroke-linejoin="round" points="'
             + " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords) + '"/>')
    for cx, cy in coords:
        p.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{ORANGE}" '
                 f'stroke="{CARD}" stroke-width="2"/>')
    ex, ey = coords[-1]
    p.append(f'<text x="{ex:.1f}" y="{ey - 16:.1f}" font-size="24" font-weight="700" '
             f'text-anchor="end" fill="{INK}">{values[-1]:,.0f}{esc(unit)}</text>')
    for idx, anchor in ((0, "start"), (len(rows) - 1, "end")):
        p.append(f'<text x="{coords[idx][0]:.1f}" y="{py + plot_h + 30:g}" font-size="18" '
                 f'text-anchor="{anchor}" fill="{MUTED}">{esc(rows[idx]["label"])}</text>')
    frame_close(p, notes, g)
    return Image("stats-supply", "\n".join(p), title)


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


# ── 카드뉴스 (유튜브 커뮤니티 게시물용) ──────────────────────
#
# 시각 철학은 docs/카드뉴스-디자인-철학.md 에 있습니다 — 「관측된 도시」.
# 요약하면 이렇습니다.
#
#   * **어둠이 기본.** 흰 바탕에 파란 글씨는 서류이지 도구가 아닙니다. 어둠 위에서만
#     숫자가 발광체가 됩니다. 밝은 카드는 리듬을 끊는 용도로만 씁니다.
#   * **숫자가 형상.** 수치는 정보가 아니라 화면의 건축입니다. 압도적으로 크게, 좁고 높은
#     골격으로. 한글은 숫자를 위해 자리를 비켜 줍니다 — 작고 조용하게.
#   * **계측의 흔적.** 여백에는 눈금·실선·색인이 남습니다. 읽히려고가 아니라 이것이
#     측정된 것임을 증명하려고 있습니다. 작고 낮은 색으로, 그러나 정확한 간격으로.
#   * **색은 하나.** 어둠·흰빛·신호색 하나. 신호색은 오늘 가장 중요한 것 하나에만 닿습니다.
#
# 한글은 시스템 글꼴을 그대로 씁니다. 라틴 서체(BigShoulders·GeistMono)에는 한글이
# 없으므로 **숫자와 기호에만** 쓰고, SVG 안에 base64 로 심어 보냅니다 (assets/fonts/README.md).

CARD_SIZE = (1080, 1080)   # 정사각. 유튜브 게시물은 세로를 잘라 보여 주는 화면이 있다.

# 관측실의 조도. 순수한 검정(#000)은 화면에서 구멍처럼 보이므로 아주 옅은 푸른 기를 남긴다.
CARD_INK = "#0d1117"
CARD_INK_2 = "#161c26"      # 한 단계 밝은 면 — 구역을 나눌 때
CARD_PAPER = "#f4f2ed"      # 밝은 카드의 바탕. 흰색(#fff)보다 종이에 가깝다
CARD_LIGHT = "#ffffff"
CARD_DIM = "#7d8794"        # 어둠 위의 낮은 글씨
CARD_DIM_PAPER = "#6b6862"  # 밝은 바탕 위의 낮은 글씨
# 신호색 — 테라코타. 처음에는 형광에 가까운 주황(#ff5c2b)이었는데 "너무 세다" 는
# 지적을 받아 채도를 낮췄습니다 (2026-09-08, 사용자). 따뜻함은 남기고 소리만 줄인 색입니다.
#
# 한 색으로 어둠과 종이 양쪽을 만족시킬 수 없습니다 — 중간 밝기 색은 양쪽 어디에도
# 4.5 를 못 냅니다. 그래서 바탕별로 짝을 나눠 둡니다.
CARD_SIGNAL = "#c9714f"        # 어둠 위 (대비 5.37)
CARD_SIGNAL_DEEP = "#96482a"   # 종이 위 (대비 5.76)
CARD_CLAY = "#73402c"          # 면색 카드의 바탕. 흰 글씨 대비 8.40
CARD_CLAY_DIM = "#e8cfc2"      # 그 위의 낮은 글씨 (대비 5.66)
CARD_RULE = "#242c38"       # 어둠 위의 실선
CARD_RULE_PAPER = "#d8d4cb"

# 라틴 서체 — 숫자와 계측 표식 전용. 한글이 오면 시스템 글꼴로 자동으로 넘어간다.
DISPLAY = "'BigShoulders','Apple SD Gothic Neo','Noto Sans CJK KR',sans-serif"
MONO = "'GeistMono','SF Mono',ui-monospace,monospace"

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_FILES = {"BigShoulders": "BigShoulders-Bold.ttf", "GeistMono": "GeistMono-Regular.ttf"}
_font_css_cache: str | None = None


def _font_css() -> str:
    """라틴 서체를 SVG 안에 심는다.

    깃허브 러너에는 이 글꼴이 없고, 크롬은 다른 로컬 파일을 기본적으로 읽지 못합니다.
    심어 보내면 어디서 그리든 같은 그림이 나옵니다. 심은 SVG 는 PNG 로 바뀐 뒤 지워지므로
    저장소에 쌓이지 않습니다. 글꼴을 못 찾으면 조용히 시스템 글꼴로 넘어갑니다.
    """
    global _font_css_cache
    if _font_css_cache is not None:
        return _font_css_cache
    import base64

    faces = []
    for family, filename in _FONT_FILES.items():
        path = _FONT_DIR / filename
        try:
            blob = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            log.debug("%s 를 찾지 못해 시스템 글꼴로 그립니다", path)
            continue
        faces.append(f"@font-face{{font-family:'{family}';font-display:block;"
                     f"src:url(data:font/ttf;base64,{blob}) format('truetype');}}")
    _font_css_cache = "".join(faces)
    return _font_css_cache


def _card_open(ground: str) -> list[str]:
    w, h = CARD_SIZE
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" font-family="{FONT}">',
            f"<style>{_font_css()}</style>",
            f'<rect width="{w}" height="{h}" fill="{ground}"/>']


def _ticks(p: list[str], color: str) -> None:
    """왼쪽 가장자리의 눈금. 계기판의 흔적 — 읽히려고가 아니라 측정을 증명하려고 있다."""
    w, h = CARD_SIZE
    for i in range(21):
        y = 150 + i * (h - 300) / 20
        long = i % 5 == 0
        p.append(f'<rect x="40" y="{y:.1f}" width="{18 if long else 9}" height="2" '
                 f'fill="{color}" opacity="{0.85 if long else 0.4}"/>')


def _card_frame(n: int, total: int, *, date: str = "", channel: str = "",
                dark: bool = True) -> list[str]:
    """카드 한 장의 바탕 — 눈금, 위쪽 표식 줄, 아래쪽 색인."""
    w, h = CARD_SIZE
    ground = CARD_INK if dark else CARD_PAPER
    ink = CARD_LIGHT if dark else INK
    dim = CARD_DIM if dark else CARD_DIM_PAPER
    rule = CARD_RULE if dark else CARD_RULE_PAPER

    p = _card_open(ground)
    _ticks(p, rule)
    if channel:
        p.append(f'<text x="96" y="104" font-size="26" font-weight="700" letter-spacing="1.5" '
                 f'fill="{ink}">{esc(channel)}</text>')
    if date:
        p.append(f'<text x="{w - 96}" y="104" font-size="24" font-family="{MONO}" '
                 f'letter-spacing="1" text-anchor="end" fill="{dim}">{esc(date.replace("-", "."))}</text>')
    p.append(f'<line x1="96" y1="132" x2="{w - 96}" y2="132" stroke="{rule}" stroke-width="1.5"/>')
    p.append(f'<line x1="96" y1="{h - 108}" x2="{w - 96}" y2="{h - 108}" stroke="{rule}" stroke-width="1.5"/>')
    if total > 1:
        p.append(f'<text x="{w - 96}" y="{h - 62}" font-size="26" font-family="{MONO}" '
                 f'letter-spacing="2" text-anchor="end" fill="{dim}">'
                 f'{n:02d} / {total:02d}</text>')
    return p


def _fit(text: str, size: float, width: float, max_lines: int, floor: float = 30) -> tuple[list[str], float]:
    """줄 수 안에 들어갈 때까지 글씨를 줄인다. 넘치면 잘라 내는 대신 작게 만든다."""
    lines = wrap(text, size, width)
    while len(lines) > max_lines and size > floor:
        size -= 4
        lines = wrap(text, size, width)
    return lines[:max_lines], size


def _sentence_fit(text: str, size: float, width: float, max_lines: int) -> tuple[list[str], float]:
    """문장 중간에서 끊기지 않게 자른다.

    그냥 줄 수로 자르면 "…수도권 전반의" 처럼 말이 끊긴 채 카드에 박힙니다.
    문장을 하나씩 덧붙여 보고 넘치기 직전까지만 담습니다.
    """
    text = " ".join((text or "").split())
    if not text:
        return [], size
    buf = ""
    for chunk in re.split(r"(?<=[.!?다])\s+", text):
        if not chunk:
            continue
        trial = f"{buf} {chunk}".strip()
        if len(wrap(trial, size, width)) > max_lines and buf:
            break
        buf = trial
    lines = wrap(buf or text, size, width)
    while len(lines) > max_lines and size > 26:
        size -= 3
        lines = wrap(buf or text, size, width)
    return lines[:max_lines], size


def _numeral(p: list[str], text: str, x: float, y: float, size: float, fill: str,
             anchor: str = "start") -> None:
    """큰 수치 한 덩이. 좁고 높은 골격이라 큰 크기에서 형태가 산다."""
    p.append(f'<text x="{x:.0f}" y="{y:.0f}" font-family="{DISPLAY}" font-size="{size:.0f}" '
             f'font-weight="700" letter-spacing="-1" text-anchor="{anchor}" '
             f'fill="{fill}">{esc(text)}</text>')


def _cover_card(headline: str, sub: str, badge: str, total: int, date: str, channel: str) -> Image:
    """표지 — 가장 어둡고, 가장 조용하고, 제목 하나가 화면을 지배한다."""
    w, h = CARD_SIZE
    p = _card_frame(1, total, date=date, channel=channel, dark=True)
    inner = w - 192

    lines, size = _fit(headline, 92, inner, 4, floor=54)
    line_h = size * 1.26
    sub_lines, sub_size = _sentence_fit(sub, 32, inner - 40, 3)
    sub_h = (52 + len(sub_lines) * sub_size * 1.5) if sub_lines else 0
    badge_h = 96 if badge else 0
    block = line_h * len(lines) + sub_h + badge_h
    top = 132 + ((h - 240) - block) / 2

    y = top + size * 0.86
    for i, line in enumerate(lines):
        p.append(f'<text x="96" y="{y + i * line_h:.0f}" font-size="{size:.0f}" '
                 f'font-weight="800" letter-spacing="-1" fill="{CARD_LIGHT}">{esc(line)}</text>')
    y += line_h * len(lines)

    if sub_lines:
        y += 52
        for i, line in enumerate(sub_lines):
            p.append(f'<text x="96" y="{y + i * sub_size * 1.5:.0f}" font-size="{sub_size:.0f}" '
                     f'fill="{CARD_DIM}">{esc(line)}</text>')
        y += len(sub_lines) * sub_size * 1.5

    if badge:
        y += 44
        p.append(f'<rect x="96" y="{y:.0f}" width="7" height="52" fill="{CARD_SIGNAL}"/>')
        _numeral(p, badge, 126, y + 44, 54, CARD_SIGNAL)
    p.append("</svg>")
    return Image("card-1-cover", "\n".join(p), headline)


def _numbers_card(nums: list[dict], n: int, total: int, date: str, channel: str) -> Image:
    """오늘의 숫자 — 신호색으로 통째로 채운 한 장. 이 장이 나머지를 지탱한다."""
    w, h = CARD_SIZE
    p = _card_open(CARD_CLAY)
    ink = CARD_LIGHT                      # 짙은 흙빛 위에는 흰 글씨 (대비 8.40)
    dim = CARD_CLAY_DIM                   # 낮은 글씨 (대비 5.66)
    for i in range(21):                   # 눈금은 여기서도 같은 자리에
        y = 150 + i * (h - 300) / 20
        p.append(f'<rect x="40" y="{y:.1f}" width="{18 if i % 5 == 0 else 9}" height="2" '
                 f'fill="{ink}" opacity="{0.45 if i % 5 == 0 else 0.22}"/>')
    if channel:
        p.append(f'<text x="96" y="104" font-size="26" font-weight="700" letter-spacing="1.5" '
                 f'fill="{ink}">{esc(channel)}</text>')
    p.append(f'<text x="{w - 96}" y="104" font-size="24" font-family="{MONO}" letter-spacing="1" '
             f'text-anchor="end" fill="{dim}">{esc(date.replace("-", "."))}</text>')
    p.append(f'<line x1="96" y1="132" x2="{w - 96}" y2="132" stroke="{ink}" stroke-width="1.5" opacity="0.3"/>')
    p.append(f'<line x1="96" y1="{h - 108}" x2="{w - 96}" y2="{h - 108}" stroke="{ink}" stroke-width="1.5" opacity="0.3"/>')
    if total > 1:
        p.append(f'<text x="{w - 96}" y="{h - 62}" font-size="26" font-family="{MONO}" '
                 f'letter-spacing="2" text-anchor="end" fill="{dim}">{n:02d} / {total:02d}</text>')

    p.append(f'<text x="96" y="212" font-size="30" font-weight="700" letter-spacing="6" '
             f'fill="{ink}">오늘의 숫자</text>')

    rows = nums[:3]
    inner = w - 192
    measured = []
    for dp in rows:
        value = f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
        v_size = 128
        while text_width(value, v_size) > inner and v_size > 56:
            v_size -= 6
        label, l_size = _fit(dp.get("label", ""), 28, inner, 2)
        measured.append((value, v_size, label, l_size,
                         v_size * 0.82 + 16 + l_size * 1.35 * len(label) + 54))

    used = sum(m[4] for m in measured)
    y = 262 + max(0, (h - 420 - used) / 2)
    for i, (value, v_size, label, l_size, height) in enumerate(measured):
        _numeral(p, value, 96, y + v_size * 0.82, v_size, ink)
        for j, line in enumerate(label):
            p.append(f'<text x="96" y="{y + v_size * 0.82 + 16 + l_size + j * l_size * 1.35:.0f}" '
                     f'font-size="{l_size:.0f}" fill="{dim}">{esc(line)}</text>')
        y += height
        if i < len(measured) - 1:
            p.append(f'<line x1="96" y1="{y - 27:.0f}" x2="{w - 96}" y2="{y - 27:.0f}" '
                     f'stroke="{ink}" stroke-width="1.5" opacity="0.25"/>')
    p.append("</svg>")
    return Image(f"card-{n}-numbers", "\n".join(p), "오늘의 숫자")


def _issue_card(issue: dict, n: int, total: int, date: str, channel: str) -> Image:
    """이슈 한 건 — 밝은 바탕. 어둠 사이에서 숨을 쉬게 하고, 수치 하나가 신호색을 가진다.

    내용을 먼저 재고 세로 가운데에 놓습니다. 위에서부터 쌓기만 하면 자료가 적은 날 카드
    아래 절반이 텅 빈 채로 남습니다. `what_happened` 는 있으면 채우고 없으면 건너뜁니다.
    """
    w, h = CARD_SIZE
    top, bottom = 176, h - 150
    inner = w - 192

    blocks: list[tuple] = []
    cat = str(issue.get("category", "")).strip()
    if cat:
        blocks.append(("cat", cat, 56))

    title, t_size = _fit(str(issue.get("title", "")), 62, inner, 3, floor=40)
    blocks.append(("title", (title, t_size), t_size * 1.28 * len(title) + 40))

    nums = [dp for dp in (issue.get("numbers") or []) if dp.get("value")][:1]
    if nums:
        dp = nums[0]
        value = f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
        v_size = 108
        while text_width(value, v_size) > inner - 20 and v_size > 52:
            v_size -= 6
        lab, lab_size = _fit(str(dp.get("label", "")), 26, inner, 2)
        blocks.append(("number", (value, v_size, lab, lab_size),
                       v_size * 0.82 + 14 + lab_size * 1.35 * len(lab) + 46))

    body, b_size = _fit(str(issue.get("one_liner", "")), 34, inner, 4)
    if body:
        blocks.append(("body", (body, b_size), b_size * 1.55 * len(body) + 36))

    for fact in [f for f in (issue.get("what_happened") or []) if f][:3]:
        lines, size = _fit(fact, 28, inner - 46, 2)
        blocks.append(("fact", (lines, size), size * 1.5 * len(lines) + 24))

    while len(blocks) > 2 and sum(b[2] for b in blocks) > bottom - top:
        blocks.pop()
    y = top + max(0, (bottom - top - sum(b[2] for b in blocks)) / 2)

    p = _card_frame(n, total, date=date, channel=channel, dark=False)
    for kind, value, height in blocks:
        if kind == "cat":
            p.append(f'<rect x="96" y="{y + 6:.0f}" width="5" height="26" fill="{CARD_SIGNAL_DEEP}"/>')
            p.append(f'<text x="118" y="{y + 28:.0f}" font-size="24" font-weight="700" '
                     f'letter-spacing="3" fill="{CARD_DIM_PAPER}">{esc(value)}</text>')
        elif kind == "title":
            lines, size = value
            for i, line in enumerate(lines):
                p.append(f'<text x="96" y="{y + size * 0.86 + i * size * 1.28:.0f}" '
                         f'font-size="{size:.0f}" font-weight="800" letter-spacing="-0.5" '
                         f'fill="{INK}">{esc(line)}</text>')
        elif kind == "number":
            text, size, lab, lab_size = value
            _numeral(p, text, 96, y + size * 0.82, size, CARD_SIGNAL_DEEP)
            for i, line in enumerate(lab):
                p.append(f'<text x="96" y="{y + size * 0.82 + 14 + lab_size + i * lab_size * 1.35:.0f}" '
                         f'font-size="{lab_size:.0f}" fill="{CARD_DIM_PAPER}">{esc(line)}</text>')
        elif kind == "body":
            lines, size = value
            for i, line in enumerate(lines):
                p.append(f'<text x="96" y="{y + size + i * size * 1.55:.0f}" '
                         f'font-size="{size:.0f}" fill="{INK_2}">{esc(line)}</text>')
        elif kind == "fact":
            lines, size = value
            p.append(f'<rect x="96" y="{y + size * 0.45:.0f}" width="24" height="1.5" '
                     f'fill="{CARD_RULE_PAPER}"/>')
            for i, line in enumerate(lines):
                p.append(f'<text x="142" y="{y + size + i * size * 1.5:.0f}" '
                         f'font-size="{size:.0f}" fill="{CARD_DIM_PAPER}">{esc(line)}</text>')
        y += height
    p.append("</svg>")
    return Image(f"card-{n}-issue", "\n".join(p), str(issue.get("title", "")))


def _list_card(title: str, items: list[str], slug: str, n: int, total: int,
               date: str, channel: str) -> Image:
    """제목 하나에 항목 몇 줄. 어두운 바탕에 번호가 눈금처럼 늘어선다."""
    w, h = CARD_SIZE
    top, bottom = 176, h - 150
    inner = w - 260

    # 항목이 적은 날은 글씨를 키운다. 같은 크기로 두면 카드가 '덜 만든 것' 처럼 비어 보인다.
    picked = items[:6]
    base = {1: 46, 2: 42, 3: 38}.get(len(picked), 32)
    rows = []
    for item in picked:
        lines, size = _fit(item, base, inner, 2)
        rows.append((lines, size, size * 1.5 * len(lines) + 44))

    head_h = 116
    while rows and head_h + sum(r[2] for r in rows) > bottom - top:
        rows.pop()
    y = top + max(0, (bottom - top - head_h - sum(r[2] for r in rows)) / 2)

    p = _card_frame(n, total, date=date, channel=channel, dark=True)
    p.append(f'<text x="96" y="{y + 36:.0f}" font-size="30" font-weight="700" letter-spacing="6" '
             f'fill="{CARD_SIGNAL}">{esc(title)}</text>')
    y += head_h
    for i, (lines, size, height) in enumerate(rows, start=1):
        p.append(f'<text x="96" y="{y + size:.0f}" font-size="24" font-family="{MONO}" '
                 f'fill="{CARD_DIM}">{i:02d}</text>')
        for j, line in enumerate(lines):
            p.append(f'<text x="164" y="{y + size + j * size * 1.5:.0f}" font-size="{size:.0f}" '
                     f'fill="{CARD_LIGHT}">{esc(line)}</text>')
        y += height
        if i < len(rows):
            p.append(f'<line x1="164" y1="{y - 20:.0f}" x2="{w - 96}" y2="{y - 20:.0f}" '
                     f'stroke="{CARD_RULE}" stroke-width="1.5"/>')
    p.append("</svg>")
    return Image(f"card-{n}-{slug}", "\n".join(p), title)


def cards(brief: dict, *, date: str = "", channel: str = "", key_numbers: list[dict] | None = None,
          max_cards: int = 7) -> list[Image]:
    """하루치 브리핑을 유튜브 게시물용 카드 5~7장으로.

    **모델을 새로 부르지 않습니다.** 이미 만들어 둔 브리핑(headline·issues·numbers·
    tomorrow_watch)을 그대로 나눠 담습니다. 그래서 카드를 켜도 하루 비용이 늘지 않습니다.

    구성: 표지(어둠) → 오늘의 숫자(신호색) → 이슈 2~3장(밝음) → 그 밖의 소식(어둠) →
    내일 볼 것(어둠). 어둠과 밝음이 교차하며 묶음에 박자를 만듭니다.
    자료가 모자란 날은 장수가 줄어듭니다. 억지로 채우지 않습니다.
    """
    issues = [i for i in (brief.get("issues") or []) if i.get("title")]
    if not brief.get("headline") or not issues:
        return []

    nums = list(key_numbers or [])
    if not nums:                       # 핵심 수치를 안 넘겨주면 이슈에서 주워 온다
        for issue in issues:
            for dp in issue.get("numbers") or []:
                if dp.get("value") and len(nums) < 3:
                    nums.append(dp)

    headline = brief["headline"]
    badge = ""
    for dp in nums:
        # **단위까지 붙은 온전한 값**으로만 견준다. 값만 보면 '6' 이 '162만원' 안의 6 에
        # 걸려 엉뚱한 배지가 달린다 (2026-09-08 실제로 그랬다).
        text = f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
        if text and text in headline:
            badge = text
            break
    watch = [w for w in (brief.get("tomorrow_watch") or []) if w]

    # 장수를 먼저 정한다 — 쪽번호(02 / 07)를 찍어야 하므로.
    deep = min(3, max(1, len(issues) - 1))            # 깊게 다룰 이슈
    rest = issues[deep:]
    plan = ["cover"]
    if len(nums) >= 2:
        plan.append("numbers")
    plan += ["issue"] * deep
    if rest:
        plan.append("rest")
    if watch:
        plan.append("watch")
    plan = plan[:max_cards]
    total = len(plan)

    out: list[Image] = []
    issue_i = 0
    for n, kind in enumerate(plan, start=1):
        if kind == "cover":
            out.append(_cover_card(headline, brief.get("market_temperature", ""),
                                   badge, total, date, channel))
        elif kind == "numbers":
            out.append(_numbers_card(nums, n, total, date, channel))
        elif kind == "issue":
            out.append(_issue_card(issues[issue_i], n, total, date, channel))
            issue_i += 1
        elif kind == "rest":
            out.append(_list_card("그 밖의 오늘 소식", [i["title"] for i in rest],
                                  "rest", n, total, date, channel))
        elif kind == "watch":
            out.append(_list_card("내일 볼 것", watch, "watch", n, total, date, channel))
    return out
