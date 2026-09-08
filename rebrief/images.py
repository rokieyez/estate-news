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
    return Image("district-map", _embed_fonts("\n".join(p)), dp["label"])


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
    return Image("index-comparison", _embed_fonts("\n".join(p)), dp["label"])
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
    return Image("time-series", _embed_fonts("\n".join(p)), dp["label"])
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
    return Image("stat-card", _embed_fonts("\n".join(p)), dp["label"])
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
    return Image("stats-volume", _embed_fonts("\n".join(p)), title)



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
    return Image(spec["slug"], _embed_fonts("\n".join(p)), title)


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
    return Image("stats-index", _embed_fonts("\n".join(p)), title)


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
    return Image("stats-jeonse", _embed_fonts("\n".join(p)), title)


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
    return Image("stats-history", _embed_fonts("\n".join(p)), title)



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
    return Image("stats-supply", _embed_fonts("\n".join(p)), title)


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
    return Image("thumb", _embed_fonts("\n".join(p)), text)


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
# 2026-09-08 에 시안 22개를 놓고 골라 **청사진** 판으로 갈아입혔습니다. 미학의 뼈대는
# 그대로입니다 — 집값을 관측된 현상으로 다루고, 카드를 관측 도구의 눈금판으로 봅니다.
#
#   * **제도 도면의 격자.** 60px 격자와 안쪽 테두리가 화면 전체에 깔립니다. 장식이
#     아니라 "이것은 재어서 그린 것"이라는 표시입니다.
#   * **한 색.** 청사진 남색과 청록 하나뿐입니다. 앞선 판의 주황은 "세다" 는 지적을
#     받았고, 청록은 같은 자리를 훨씬 조용히 지킵니다.
#   * **숫자가 형상.** 수치는 화면의 건축입니다. 고정폭(IBM Plex Mono)으로 판독값처럼.
#     고정폭은 큰 크기에서 헐거워 보이므로 음수 자간으로 조입니다.
#   * **세 가지 낯.** 남색(표지·목록) · 짙은 남색(숫자) · 밝은 청사진(이슈)이
#     번갈아 나오며 일곱 장에 박자를 만듭니다.
#
# 한글은 시스템 글꼴을 그대로 씁니다. 라틴 서체에는 한글이 없으므로 **숫자와 기호에만**
# 쓰고 SVG 안에 base64 로 심어 보냅니다 (assets/fonts/README.md 에 이유가 있습니다).

CARD_SIZE = (1080, 1080)   # 정사각. 유튜브 게시물은 세로를 잘라 보여 주는 화면이 있다.

# 세 가지 낯 — (바탕, 글씨, 낮은 글씨, 선, 신호색). 색은 눈이 아니라 대비로 골랐습니다.
# 작은 글씨는 모두 4.5 이상입니다 (시험이 지킵니다).
CARD_DARK = ("#0a2f4f", "#cfe8ff", "#6f9dc4", "#164a75", "#7fe3ff")
CARD_FLOOD = ("#062033", "#7fe3ff", "#6b93b4", "#103753", "#7fe3ff")
CARD_PAPER = ("#dfeaf4", "#0a2f4f", "#3a6588", "#b9d0e3", "#0b6a8f")

# 일곱 장의 박자. 표지·목록은 남색, 숫자는 짙은 남색, 이슈는 밝은 청사진.
CARD_FACES = {"cover": CARD_DARK, "numbers": CARD_FLOOD, "issue": CARD_PAPER,
              "rest": CARD_DARK, "watch": CARD_DARK}

CARD_LIGHT = "#ffffff"     # 소제목·사진 위 글씨. 어느 낯에서든 흰색이다.
CARD_GRID = 60             # 제도 격자 한 칸
# 한글은 프리텐다드, 숫자·기호는 IBM Plex Mono. 이 카드에만 쓰는 짝입니다
# (블로그 인포그래픽은 그대로 FONT 를 씁니다 — 러너에 프리텐다드가 없어 그쪽까지
# 바꾸면 맥에서 만든 그림과 러너에서 만든 그림이 달라집니다).
CARD_FONT = "'Pretendard','Apple SD Gothic Neo','Noto Sans CJK KR',sans-serif"
DISPLAY = "'IBMPlexMonoBold','PretendardBold','Apple SD Gothic Neo',sans-serif"
MONO = "'IBMPlexMono','SF Mono',ui-monospace,monospace"

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_FILES = {
    "IBMPlexMono": "IBMPlexMono-Regular.ttf",
    "IBMPlexMonoBold": "IBMPlexMono-Bold.ttf",
    "Pretendard": "Pretendard-Regular.otf",
    "PretendardBold": "Pretendard-Bold.otf",
}
# 굵기를 이름으로 갈라 두었으므로 @font-face 에 굵기를 함께 적습니다. 그래야
# font-weight:800 인 제목이 굵은 파일을 집습니다.
_FONT_WEIGHT = {"IBMPlexMonoBold": 700, "PretendardBold": 700}
_FONT_CSS_TOKEN = "<!--글꼴-->"     # 카드마다 쓴 글자만 잘라 넣을 자리
_font_cache: dict[str, str] = {}


def _font_css(chars: str = "") -> str:
    """카드에 실제로 쓴 글자만 잘라 SVG 안에 심는다.

    깃허브 러너에는 이 글꼴이 없고, 크롬은 다른 로컬 파일을 기본적으로 읽지 못합니다.
    심어 보내면 어디서 그리든 같은 그림이 나옵니다.

    **통째로 심으면 안 됩니다.** 프리텐다드 한 벌이 1.5MB 라 두 굵기를 그대로 넣으면
    카드 한 장의 SVG 가 4MB 를 넘습니다. 한 장에 쓰는 글자는 200자 안쪽이므로 그만
    잘라내면 수십 KB 로 줄어듭니다. fontTools 가 없으면 조용히 시스템 글꼴로 갑니다 —
    글씨가 조금 달라질 뿐 카드는 그대로 나옵니다.
    """
    key = "".join(sorted(set(chars)))
    if key in _font_cache:
        return _font_cache[key]
    import base64

    try:
        from fontTools import subset as ft_subset
        from fontTools.ttLib import TTFont

        # 'meta NOT subset' 안내가 글꼴마다 나온다. 우리가 할 일이 없는 표라 접어 둔다.
        logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
    except ImportError:
        log.debug("fontTools 가 없어 카드 글꼴을 시스템 것으로 그립니다")
        _font_cache[key] = ""
        return ""

    faces = []
    for family, filename in _FONT_FILES.items():
        path = _FONT_DIR / filename
        try:
            font = TTFont(path, lazy=True)
        except Exception:
            log.debug("%s 를 찾지 못해 시스템 글꼴로 그립니다", path)
            continue
        try:
            options = ft_subset.Options()
            options.desubroutinize = True          # CFF 를 풀어야 크롬이 확실히 읽는다
            options.drop_tables += ["GSUB", "GPOS"]
            options.notdef_outline = True
            subsetter = ft_subset.Subsetter(options=options)
            subsetter.populate(text=key + "0123456789.,%/ ")
            subsetter.subset(font)
            import io

            buf = io.BytesIO()
            font.save(buf)
            blob = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            log.debug("%s 부분집합을 뜨지 못했습니다: %s", filename, exc)
            continue
        finally:
            font.close()
        weight = _FONT_WEIGHT.get(family, 400)
        faces.append(f"@font-face{{font-family:'{family}';font-weight:{weight};"
                     f"font-display:block;"
                     f"src:url(data:font/otf;base64,{blob}) format('opentype');}}")
    _font_cache[key] = "".join(faces)
    return _font_cache[key]


def _embed_fonts(svg: str) -> str:
    """다 그린 카드에서 쓴 글자를 긁어 글꼴을 심는다.

    글꼴을 먼저 넣고 글을 나중에 그리므로, 그릴 때는 자리만 잡아 두고 마지막에 바꿉니다.
    """
    chars = "".join(re.findall(r">([^<>]*)<", svg))
    return svg.replace(_FONT_CSS_TOKEN, f"<style>{_font_css(chars)}</style>")


def _png_size(path: Path) -> tuple[int, int]:
    """PNG 머리말에서 가로·세로를 읽는다. 그림 라이브러리를 들이지 않으려고 직접 읽는다."""
    blob = path.read_bytes()[:24]
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 가 아닙니다")
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")


CARD_BAND = 470        # 카드 위쪽 그림 띠의 높이. 아래 판에 글 넉넉히 들어갈 만큼 남긴다.
_ART_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp"}


def _art_band(path: Path) -> dict | None:
    """카드 위쪽에 얹을 그림 한 장을 준비한다.

    두 갈래입니다.
    · `photo-*` — **사람이 직접 넣어 둔 사진**, 또는 무료 사진(Pexels)에서 받아 온 것.
    · `img-*` — 그날 우리가 그린 인포그래픽. 기사 사진은 저작권이 있어 쓰지 않고
      수집하지도 않으므로, 사진이 없는 날의 기본값이 이쪽입니다.

    띠는 **가득 채웁니다**(`slice`). 담아서 넣으면 양옆이나 위아래가 허옇게 비어
    '덜 만든 것' 처럼 보입니다. 대신 세로로 긴 인포그래픽은 잘리면 축이나 범례가
    날아가므로 아예 쓰지 않습니다 (사진은 잘려도 되니 이 검사에서 뺍니다).
    """
    mime = _ART_MIME.get(path.suffix.lower())
    if not mime:
        return None
    if not path.name.startswith("photo-"):
        try:
            w, h = _png_size(path)
        except (OSError, ValueError):
            return None
        if not w or not h or w / h < 1.35:
            return None
    import base64

    try:
        blob = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return {"height": CARD_BAND, "data": blob, "mime": mime, "credit": ""}


_LOGO_PATH = _FONT_DIR.parent / "logo.png"      # assets/logo.png (SVG 도 됩니다)
# 로고의 글자(부돌보)를 뺀 **그림 부분만** 씁니다. 옆에 '부돌보 브리핑' 이 붙어 있어
# 통째로 넣으면 같은 말이 두 번 되고, 44px 로 줄이면 그 글자가 8px 이 되어 뭉갭니다.
CARD_LOGO_MARK_ONLY = True
_logo_cache: dict[str, tuple] = {}


def _opaque_box(path: Path) -> tuple[int, int, int, int, list[tuple[int, int]]]:
    """PNG 에서 **비어 있지 않은 부분**의 사각형과, 세로로 끊긴 덩이 목록을 돌려준다.

    로고 파일은 가운데에만 그림이 있고 사방이 비어 있는 경우가 많습니다 — 이 파일도
    좌우 30%·아래 23% 가 빈칸이라 그대로 얹으면 그림이 자리의 절반도 못 채웁니다.
    그림 라이브러리(PIL)를 들이지 않으려고 PNG 를 직접 풉니다. 한 실행에 한 번만 합니다.
    """
    import struct
    import zlib

    raw = path.read_bytes()
    pos, idat, w, h, color = 8, b"", 0, 0, 6
    while pos < len(raw):
        ln = struct.unpack(">I", raw[pos:pos + 4])[0]
        tag = raw[pos + 4:pos + 8]
        if tag == b"IHDR":
            w, h, _depth, color = struct.unpack(">IIBB", raw[pos + 8:pos + 18])
        elif tag == b"IDAT":
            idat += raw[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    if color != 6:                       # 알파가 없으면 자를 것도 없다
        return 0, 0, w, h, [(0, h - 1)]

    data, ch = zlib.decompress(idat), 4
    stride, prev, i = w * ch, bytearray(w * ch), 0
    minx, miny, maxx, maxy = w, h, -1, -1
    rows: list[bool] = []
    for y in range(h):
        ft = data[i]
        i += 1
        line = bytearray(data[i:i + stride])
        i += stride
        if ft:                           # 0 이면 필터 없음 — 그대로 쓴다
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                if ft == 1:
                    line[x] = (line[x] + a) & 255
                elif ft == 2:
                    line[x] = (line[x] + b) & 255
                elif ft == 3:
                    line[x] = (line[x] + (a + b) // 2) & 255
                else:
                    pp = a + b - c
                    pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                    line[x] = (line[x] + (a if (pa <= pb and pa <= pc)
                                          else (b if pb <= pc else c))) & 255
        prev = line
        on = False
        for x in range(w):
            if line[x * ch + 3] > 16:
                on = True
                minx, maxx = min(minx, x), max(maxx, x)
        rows.append(on)
        if on:
            miny, maxy = min(miny, y), max(maxy, y)

    blocks, begin = [], None
    for y, on in enumerate(rows):
        if on and begin is None:
            begin = y
        if not on and begin is not None:
            blocks.append((begin, y - 1))
            begin = None
    if begin is not None:
        blocks.append((begin, h - 1))
    if maxx < 0:
        return 0, 0, w, h, [(0, h - 1)]
    return minx, miny, maxx - minx + 1, maxy - miny + 1, blocks


def _logo_tag(x: float, top: float, height: float, *, white: bool) -> tuple[str, float]:
    """머리글 왼쪽에 얹을 로고. (SVG 조각, 차지한 너비) 를 돌려준다.

    **어두운 낯에서는 통째로 흰색으로 만듭니다** (2026-09-08, 사용자 지시). 로고의 파랑이
    청사진 남색과 붙어 있어 그냥 얹으면 보이지 않습니다. `brightness(0) invert(1)` 은
    투명하지 않은 픽셀을 모두 흰색으로 바꿉니다 — 그래서 **바탕이 투명한 파일이어야
    합니다.** 흰 바탕 파일을 넣으면 흰 네모가 됩니다.

    빈 여백은 잘라 냅니다(`_opaque_box`). 자르지 않으면 44px 자리에 그림이 20px 만 찹니다.
    겹친 `<svg>` 는 **`overflow="hidden"` 이어야** 자르기가 먹습니다 — `visible` 이면
    `viewBox` 로 아무리 좁혀도 그림 전체가 그대로 나옵니다.
    파일이 없으면 빈 조각을 돌려주고 글자만 나갑니다 — 로고 하나 때문에 카드가 죽으면 안 됩니다.
    """
    path = _LOGO_PATH
    key = str(path)
    if key not in _logo_cache:
        import base64

        try:
            blob = base64.b64encode(path.read_bytes()).decode("ascii")
            if path.suffix.lower() == ".svg":
                head = path.read_text(encoding="utf-8", errors="ignore")[:600]
                box = re.search(r'viewBox="([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)"',
                                head)
                crop = tuple(float(g) for g in box.groups()) if box else (0, 0, 100, 100)
                _logo_cache[key] = (crop, (crop[2], crop[3]), "image/svg+xml", blob)
            else:
                bx, by, bw, bh, blocks = _opaque_box(path)
                if CARD_LOGO_MARK_ONLY and len(blocks) > 1:
                    by, bh = blocks[0][0], blocks[0][1] - blocks[0][0] + 1
                full = _png_size(path)
                _logo_cache[key] = ((bx, by, bw, bh), full, "image/png", blob)
        except (OSError, ValueError):
            log.debug("%s 가 없어 로고 없이 그립니다", path)
            _logo_cache[key] = ()
    packed = _logo_cache[key]
    if not packed:
        return "", 0.0
    (bx, by, bw, bh), (fw, fh), mime, blob = packed
    width = height * (bw / bh if bh else 1.0)
    style = ' style="filter:brightness(0) invert(1)"' if white else ""
    tag = (f'<svg x="{x:.0f}" y="{top:.0f}" width="{width:.0f}" height="{height:.0f}" '
           f'viewBox="{bx:.0f} {by:.0f} {bw:.0f} {bh:.0f}" overflow="hidden">'
           f'<image x="0" y="0" width="{fw:.0f}" height="{fh:.0f}"{style} '
           f'xlink:href="data:{mime};base64,{blob}"/></svg>')
    return tag, width


def _card_frame(face: tuple, n: int, total: int, *, date: str = "", channel: str = "",
                art: dict | None = None,
                banner: tuple[str, str] | None = None) -> tuple[list[str], float, float]:
    """카드 한 장의 바탕. 조각들과 글이 들어갈 위·아래 경계를 함께 돌려준다.

    두 가지 꼴이 있습니다.
    · **그림 카드** — 위쪽 띠에 인포그래픽, 아래쪽 판에 글. 머리글은 맨 아래로 내려갑니다.
    · **글자 카드** — 화면 전체가 청사진. 머리글이 위에 있습니다.
    둘이 번갈아 나오며 일곱 장에 박자를 만듭니다.
    """
    w, h = CARD_SIZE
    ground, ink, dim, rule, signal = face
    band = float(art["height"]) if art else 0.0
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
         f'width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">',
         f"<style>{_font_css()}</style>",
         f'<rect width="{w}" height="{h}" fill="{ground}"/>']

    if art:
        # 그림은 흰 바탕에 통째로 담는다. 잘라 내면 표의 축이나 범례가 날아간다.
        p.append(f'<rect x="0" y="0" width="{w}" height="{band:.0f}" fill="#ffffff"/>')
        # 띠를 가득 채운다. 담아서 넣으면 양옆이 허옇게 비어 '덜 만든 것' 처럼 보인다.
        # 더 당겨 잘라 인포그래픽의 흰 테두리를 밀어내 봤지만(1.34배) 수치가 함께
        # 잘려 나갔다 — '162만원' 이 '62만원' 이 됐다. 그래서 있는 그대로 채운다.
        p.append(f'<clipPath id="band"><rect x="0" y="0" width="{w}" '
                 f'height="{band:.0f}"/></clipPath>')
        p.append(f'<image x="0" y="0" width="{w}" height="{band:.0f}" clip-path="url(#band)" '
                 f'preserveAspectRatio="xMidYMid slice" '
                 f'xlink:href="data:{art["mime"]};base64,{art["data"]}"/>')
        if banner:
            # 표지의 대문. 사진을 눌러 어둡게 하고 그 위에 흰 글씨를 얹는다.
            # 어둡게 하지 않으면 밝은 하늘이나 흰 건물 위에서 글씨가 사라진다.
            p.append(f'<rect x="0" y="0" width="{w}" height="{band:.0f}" '
                     f'fill="#000000" opacity="0.55"/>')
            day, line = banner
            p.append(f'<text x="{w/2:.0f}" y="{band/2-34:.0f}" font-size="40" '
                     f'font-family="{MONO}" letter-spacing="6" text-anchor="middle" '
                     f'fill="#ffffff" opacity="0.88">{esc(day)}</text>')
            size = 92
            while text_width(line, size) > w - 160 and size > 56:
                size -= 4
            p.append(f'<text x="{w/2:.0f}" y="{band/2+size*0.62:.0f}" font-size="{size:.0f}" '
                     f'font-weight="800" letter-spacing="-1" text-anchor="middle" '
                     f'fill="#ffffff">{esc(line)}</text>')
        p.append(f'<rect x="0" y="{band:.0f}" width="{w}" height="4" fill="{signal}"/>')

    # 제도 격자 — 글이 놓이는 판에만. 읽히려고가 아니라 이것이 재어서 그린 것임을 보이려고 있다.
    p.append(f'<clipPath id="panel"><rect x="0" y="{band:.0f}" width="{w}" '
             f'height="{h-band:.0f}"/></clipPath>')
    grid = ['<g clip-path="url(#panel)">']
    for g in range(0, max(w, h) + 1, CARD_GRID):
        if g <= w:
            grid.append(f'<line x1="{g}" y1="0" x2="{g}" y2="{h}" stroke="{rule}" '
                        f'stroke-width="1" opacity="0.5"/>')
        if g <= h:
            grid.append(f'<line x1="0" y1="{g}" x2="{w}" y2="{g}" stroke="{rule}" '
                        f'stroke-width="1" opacity="0.5"/>')
    grid.append("</g>")
    p += grid

    if art:
        top = band + 64
        p.append(f'<rect x="58" y="{band+30:.0f}" width="{w-116}" height="{h-band-88:.0f}" '
                 f'fill="none" stroke="{signal}" stroke-width="1.4" opacity="0.45"/>')
        # 사진 출처 한 줄. **'본문과 무관' 을 빼지 마세요.** 은마아파트 기사 옆에 아무
        # 아파트 사진이 붙으면 읽는 사람은 그게 은마인 줄 압니다.
        if art.get("credit"):
            p.append(f'<rect x="0" y="{band-36:.0f}" width="{w}" height="36" '
                     f'fill="#000000" opacity="0.42"/>')
            p.append(f'<text x="{w-96}" y="{band-12:.0f}" font-size="17" text-anchor="end" '
                     f'fill="#ffffff" opacity="0.9">{esc(art["credit"])}</text>')
        bottom = h - 100
        meta = []
        if channel:
            meta.append(esc(channel))
        if date:
            meta.append(esc(date.replace("-", ".")))
        if meta:
            mark, used = _logo_tag(96, h - 118, 42, white=ground != CARD_PAPER[0])
            p.append(mark)
            p.append(f'<text x="{96 + (used + 14 if used else 0):.0f}" y="{h-80}" font-size="25" '
                     f'fill="{dim}">{" · ".join(meta)}</text>')
    else:
        top, bottom = 172, h - 152
        p.append(f'<rect x="58" y="58" width="{w-116}" height="{h-116}" fill="none" '
                 f'stroke="{signal}" stroke-width="1.4" opacity="0.45"/>')
        if channel:
            mark, used = _logo_tag(96, 66, 44, white=True)
            p.append(mark)
            p.append(f'<text x="{96 + (used + 14 if used else 0):.0f}" y="102" font-size="28" '
                     f'font-weight="700" letter-spacing="1" fill="{ink}">{esc(channel)}</text>')
        if date:
            p.append(f'<text x="{w-96}" y="102" font-size="26" font-family="{MONO}" '
                     f'letter-spacing="1" text-anchor="end" fill="{dim}">'
                     f'{esc(date.replace("-", "."))}</text>')
        p.append(f'<line x1="96" y1="130" x2="{w-96}" y2="130" stroke="{rule}" stroke-width="1.5"/>')
        p.append(f'<line x1="96" y1="{h-108}" x2="{w-96}" y2="{h-108}" stroke="{rule}" '
                 f'stroke-width="1.5"/>')
    if total > 1:
        p.append(f'<text x="{w-96}" y="{h-80}" font-size="28" font-family="{MONO}" letter-spacing="2" '
                 f'text-anchor="end" fill="{dim}">{n:02d} / {total:02d}</text>')
    return p, top, bottom


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
    # 한 문장이 통째로 안 들어가는 날이 있다. 그때 줄 수로 잘라 버리면 "…상승 압력이"
    # 처럼 말이 끊긴 채 카드에 박힌다. 글씨를 줄여서라도 문장을 끝까지 보인다.
    lines = wrap(buf or text, size, width)
    while len(lines) > max_lines and size > 22:
        size -= 2
        lines = wrap(buf or text, size, width)
    return lines[:max_lines], size


def _numeral(p: list[str], text: str, x: float, y: float, size: float, fill: str,
             anchor: str = "start") -> None:
    """큰 수치 한 덩이.

    고정폭은 글자마다 너비가 같아 큰 크기에서 헐거워 보입니다. 자간을 음수로 조입니다.
    빈칸은 더 심합니다 — '6억원 이하' 의 빈칸 하나가 126px 에서 76px 을 먹어 두 낱말이
    따로 노는 것처럼 보였습니다. 낱말 사이도 함께 줄입니다.
    """
    # 마침표·쉼표도 숫자와 같은 칸을 먹어 '29 . 5%' 처럼 보인다. 그 앞뒤만 당겨 붙인다.
    pull = size * 0.16
    parts, buf, pending = [], "", 0.0
    for ch in text:
        if ch in ".,":
            if buf:
                parts.append((pending, buf))
                buf, pending = "", 0.0
            parts.append((pending or 0.0, ch) if not parts else (-pull, ch))
            pending = -pull
        else:
            buf += ch
    if buf:
        parts.append((pending, buf))
    inner = "".join(f'<tspan dx="{dx:.1f}">{esc(run)}</tspan>' if dx else esc(run)
                    for dx, run in parts)
    p.append(f'<text x="{x:.0f}" y="{y:.0f}" font-family="{DISPLAY}" font-size="{size:.0f}" '
             f'font-weight="700" letter-spacing="{-size*0.05:.1f}" word-spacing="{-size*0.34:.1f}" '
             f'text-anchor="{anchor}" fill="{fill}">{inner}</text>')


def _title_block(p: list[str], lines: list[str], size: float, x: float, y: float,
                 ink: str, signal: str, *, weight: int = 800) -> float:
    """제목 여러 줄. **마지막 줄만 신호색**으로 받는다.

    한 덩이를 통째로 흰 글씨로 두면 어디서 눈이 멎어야 할지 알 수 없습니다.
    마지막 줄이 대개 결론이라 거기에 색을 얹습니다.
    """
    line_h = size * 1.28
    for i, line in enumerate(lines):
        fill = signal if (i == len(lines) - 1 and len(lines) > 1) else ink
        p.append(f'<text x="{x:.0f}" y="{y+size*0.86+i*line_h:.0f}" font-size="{size:.0f}" '
                 f'font-weight="{weight}" letter-spacing="-1" fill="{fill}">{esc(line)}</text>')
    return line_h * len(lines)


def _cover_card(headline: str, sub: str, badge: str, total: int, date: str, channel: str,
                art: dict | None = None) -> Image:
    """표지 — 사진 위에 대문, 아래 판에 그날의 제목.

    **부제는 싣지 않습니다** (2026-09-08, 사용자 지시). 표지 부제가 3·4·5번 카드의 내용을
    앞당겨 말해 버려 뒤 카드들이 되풀이처럼 읽혔습니다 (5번 카드와 낱말 39% 겹침).
    `sub` 인자는 부르는 쪽 호환을 위해 남겨 두었지만 그리지 않습니다.
    """
    w, h = CARD_SIZE
    ground, ink, dim, rule, signal = CARD_FACES["cover"]
    banner = (date.replace("-", ".") if date else "", "부동산 주요이슈") if art else None
    p, top, bottom = _card_frame(CARD_FACES["cover"], 1, total, date=date, channel=channel,
                                 art=art, banner=banner)
    inner = w - 192

    lines, size = _fit(headline, 104 if not art else 80, inner, 4 if not art else 3,
                       floor=62 if not art else 50)
    line_h = size * 1.26
    badge_h = 96 if badge else 0
    y = top + max(0, (bottom - top - (line_h * len(lines) + badge_h)) / 2)

    p.append(f'<rect x="96" y="{y:.0f}" width="7" height="{line_h*len(lines):.0f}" fill="{signal}"/>')
    y += _title_block(p, lines, size, 126, y, ink, signal)
    if badge:
        y += 44
        _numeral(p, badge, 126, y + 44, 62, signal)
    p.append("</svg>")
    return Image("card-1-cover", _embed_fonts("\n".join(p)), headline)


def _numbers_card(nums: list[dict], n: int, total: int, date: str, channel: str) -> Image:
    """오늘의 숫자 — 가장 짙은 낯. 수치가 판독값처럼 청록으로 빛난다."""
    w, h = CARD_SIZE
    ground, ink, dim, rule, signal = CARD_FACES["numbers"]
    p, top, bottom = _card_frame(CARD_FACES["numbers"], n, total, date=date, channel=channel)
    inner = w - 192

    p.append(f'<text x="{w/2:.0f}" y="{top+66:.0f}" font-size="68" font-weight="800" '
             f'letter-spacing="-1" text-anchor="middle" fill="{CARD_LIGHT}">오늘의 숫자</text>')
    head_h = 116
    inner = w - 192

    # 설명은 **한 줄**로 놓고 너비를 안쪽 테두리까지 넓게 씁니다. 두 줄을 허용하면
    # 한 줄당 62px 씩 먹어 세 수치가 들어갈 자리가 없어집니다. 넓게 한 줄로 두면
    # 글씨는 조금 작아지지만(46 → 38쯤) 셋이 다 올라갑니다.
    label_w = w - 170

    def measure(cap: float) -> list[tuple]:
        rows = []
        for dp in nums[:3]:
            value = f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
            v_size = cap
            while text_width(value, v_size) > inner and v_size > 60:
                v_size -= 6
            # 한 줄이 기본입니다. 다만 `_fit` 은 하한(30)에 닿으면 **말을 잘라 버리므로**,
            # 그래도 안 들어가는 긴 설명은 두 줄로 풀어 줍니다. 잘린 설명보다 낫습니다.
            text = str(dp.get("label", ""))
            label, l_size = _fit(text, 46, label_w, 1, floor=32)
            if len(wrap(text, l_size, label_w)) > 1:
                label, l_size = _fit(text, 38, label_w, 2, floor=28)
            rows.append((value, v_size, label, l_size,
                         v_size * 0.82 + 24 + l_size * 1.35 * len(label) + 26))
        return rows

    # 글씨를 키운 뒤로 세 수치가 늘 들어가지는 않는다. 그냥 쌓으면 마지막 설명이
    # 쪽번호와 겹친 채 카드 밖으로 흘러나간다 (2026-09-08 에 실제로 그랬다).
    #
    # **먼저 수치를 조금 줄여 셋을 다 담아 봅니다** (112 까지). 그래도 넘치면 덜어 냅니다 —
    # 그 아래로 줄이면 '오늘의 숫자' 카드에서 숫자가 가장 작아지는 앞뒤 안 맞는 그림이
    # 됩니다. 덜어 낸 수치는 이슈 카드가 어차피 다시 말합니다.
    room = bottom - top - head_h
    cap = 140
    measured = measure(cap)
    while sum(m[4] for m in measured) > room and cap > 112:
        cap -= 8
        measured = measure(cap)
    while len(measured) > 1 and sum(m[4] for m in measured) > room:
        measured.pop()

    y = top + head_h + max(0, (room - sum(m[4] for m in measured)) / 2)
    for i, (value, v_size, label, l_size, height) in enumerate(measured):
        _numeral(p, value, w / 2, y + v_size * 0.82, v_size, ink, anchor="middle")
        for j, line in enumerate(label):
            p.append(f'<text x="{w/2:.0f}" y="{y+v_size*0.82+24+l_size+j*l_size*1.35:.0f}" '
                     f'font-size="{l_size:.0f}" text-anchor="middle" fill="{dim}">{esc(line)}</text>')
        y += height
        if i < len(measured) - 1:
            p.append(f'<line x1="96" y1="{y-15:.0f}" x2="{w-96}" y2="{y-15:.0f}" '
                     f'stroke="{ink}" stroke-width="1.5" opacity="0.28"/>')
    p.append("</svg>")
    return Image(f"card-{n}-numbers", _embed_fonts("\n".join(p)), "오늘의 숫자")


def _issue_card(issue: dict, n: int, total: int, date: str, channel: str,
                art: dict | None = None) -> Image:
    """이슈 한 건. 그림이 있으면 위에 얹고 아래 판에 글을 담는다.

    내용을 먼저 재고 세로 가운데에 놓습니다. 위에서부터 쌓기만 하면 자료가 적은 날 카드
    아래 절반이 텅 빈 채로 남습니다. `what_happened` 는 있으면 채우고 없으면 건너뜁니다.
    """
    w, h = CARD_SIZE
    ground, ink, dim, rule, signal = CARD_FACES["issue"]
    p, top, bottom = _card_frame(CARD_FACES["issue"], n, total, date=date, channel=channel, art=art)
    inner = w - 192

    blocks: list[tuple] = []
    cat = str(issue.get("category", "")).strip()
    if cat:
        blocks.append(("cat", cat, 74))

    t_max = 70 if not art else 58
    title, t_size = _fit(str(issue.get("title", "")), t_max, inner, 3, floor=44)
    blocks.append(("title", (title, t_size), t_size * 1.28 * len(title) + 38))

    nums = [dp for dp in (issue.get("numbers") or []) if dp.get("value")][:1]
    if nums and not art:      # 그림이 있는 날엔 수치까지 얹으면 판이 비좁다
        dp = nums[0]
        value = f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
        v_size = 116
        while text_width(value, v_size) > inner - 20 and v_size > 56:
            v_size -= 6
        lab, lab_size = _fit(str(dp.get("label", "")), 45, inner, 2)
        blocks.append(("number", (value, v_size, lab, lab_size),
                       v_size * 0.82 + 22 + lab_size * 1.35 * len(lab) + 44))

    body, b_size = _fit(str(issue.get("one_liner", "")), 46 if not art else 43, inner, 4)
    if body:
        blocks.append(("body", (body, b_size), b_size * 1.55 * len(body) + 34))

    for fact in [f for f in (issue.get("what_happened") or []) if f][:3]:
        lines, size = _fit(fact, 46, inner - 60, 2)
        blocks.append(("fact", (lines, size), size * 1.5 * len(lines) + 22))

    # 넘칠 때 **뒤에서부터 무작정 덜어 내면 안 됩니다.** 그림 카드에서 한 줄 요약이
    # 통째로 빠져 제목만 남은 적이 있습니다 (2026-09-08, 요약 글씨를 43 으로 키운 뒤).
    # 차례는 이렇습니다 — ① 덧붙인 사실 줄 ② 요약 글씨 줄이기 ③ 그래도 안 되면 덜어 내기.
    room = bottom - top
    total = lambda: sum(b[2] for b in blocks)          # noqa: E731
    while total() > room and any(b[0] == "fact" for b in blocks):
        for i in range(len(blocks) - 1, -1, -1):
            if blocks[i][0] == "fact":
                blocks.pop(i)
                break
    while total() > room:
        idx = next((i for i, b in enumerate(blocks) if b[0] == "body"), None)
        if idx is None or blocks[idx][1][1] <= 30:
            break
        lines, size = _fit(str(issue.get("one_liner", "")), blocks[idx][1][1] - 3, inner, 5)
        blocks[idx] = ("body", (lines, size), size * 1.55 * len(lines) + 34)
    while len(blocks) > 2 and total() > room:
        blocks.pop()
    y = top + max(0, (room - total()) / 2)

    for kind, value, height in blocks:
        if kind == "cat":
            p.append(f'<rect x="96" y="{y+6:.0f}" width="6" height="36" fill="{signal}"/>')
            p.append(f'<text x="118" y="{y+32:.0f}" font-size="39" font-weight="700" '
                     f'letter-spacing="1.5" fill="{dim}">{esc(value)}</text>')
        elif kind == "title":
            lines, size = value
            _title_block(p, lines, size, 96, y, ink, signal)
        elif kind == "number":
            text, size, lab, lab_size = value
            _numeral(p, text, 96, y + size * 0.82, size, signal)
            for i, line in enumerate(lab):
                p.append(f'<text x="96" y="{y+size*0.82+22+lab_size+i*lab_size*1.35:.0f}" '
                         f'font-size="{lab_size:.0f}" fill="{dim}">{esc(line)}</text>')
        elif kind == "body":
            lines, size = value
            for i, line in enumerate(lines):
                p.append(f'<text x="96" y="{y+size+i*size*1.55:.0f}" font-size="{size:.0f}" '
                         f'fill="{ink}" opacity="0.92">{esc(line)}</text>')
        elif kind == "fact":
            lines, size = value
            p.append(f'<rect x="96" y="{y+size*0.45:.0f}" width="24" height="1.5" fill="{rule}"/>')
            for i, line in enumerate(lines):
                p.append(f'<text x="142" y="{y+size+i*size*1.5:.0f}" font-size="{size:.0f}" '
                         f'fill="{dim}">{esc(line)}</text>')
        y += height
    p.append("</svg>")
    return Image(f"card-{n}-issue", _embed_fonts("\n".join(p)), str(issue.get("title", "")))


def _list_card(title: str, items: list[str], slug: str, n: int, total: int,
               date: str, channel: str) -> Image:
    """제목 하나에 항목 몇 줄. 번호가 도면의 부품 번호처럼 붙는다."""
    w, h = CARD_SIZE
    face = CARD_FACES.get(slug, CARD_DARK)
    ground, ink, dim, rule, signal = face
    inner = w - 260
    p, top, bottom = _card_frame(face, n, total, date=date, channel=channel)

    # 항목이 적은 날은 글씨를 키운다. 같은 크기로 두면 카드가 '덜 만든 것' 처럼 비어 보인다.
    picked = items[:6]
    base = {1: 58, 2: 54, 3: 48}.get(len(picked), 42)
    rows = []
    for item in picked:
        lines, size = _fit(item, base, inner, 2)
        rows.append((lines, size, size * 1.5 * len(lines) + 42))

    head_h = 142
    while rows and head_h + sum(r[2] for r in rows) > bottom - top:
        rows.pop()
    y = top + max(0, (bottom - top - head_h - sum(r[2] for r in rows)) / 2)

    p.append(f'<text x="96" y="{y+62:.0f}" font-size="68" font-weight="800" letter-spacing="-1" '
             f'fill="{CARD_LIGHT}">{esc(title)}</text>')
    y += head_h
    for i, (lines, size, height) in enumerate(rows, start=1):
        p.append(f'<text x="96" y="{y+size:.0f}" font-size="39" font-family="{MONO}" '
                 f'fill="{dim}">{i:02d}</text>')
        for j, line in enumerate(lines):
            p.append(f'<text x="162" y="{y+size+j*size*1.5:.0f}" font-size="{size:.0f}" '
                     f'fill="{ink}">{esc(line)}</text>')
        y += height
        if i < len(rows):
            p.append(f'<line x1="162" y1="{y-21:.0f}" x2="{w-96}" y2="{y-21:.0f}" '
                     f'stroke="{rule}" stroke-width="1.5"/>')
    p.append("</svg>")
    return Image(f"card-{n}-{slug}", _embed_fonts("\n".join(p)), title)


def _drop_repeats(issues: list[dict], threshold: float = 0.40) -> list[dict]:
    """같은 사건이 두 이슈로 갈린 것을 골라 낸다 (2026-09-08, 사용자 지시).

    2026-09-08 에 '분당 집값 상승률 전국 1위' 와 '분당 집값 상승률, 강남 제쳐' 가 따로
    이슈가 되어 카드 두 장이 낱말 67% 를 공유했습니다. 같은 수치(29.5%)를 두 번 말하니
    묶음 전체가 되풀이처럼 읽힙니다.

    **기준 0.40 은 재서 골랐습니다.** 그날 이슈 열 쌍을 전부 재 보니 겹친 한 쌍이 0.51,
    나머지 아홉 쌍은 0.03~0.16 이었습니다. 두 구간 사이가 비어 있어 그 가운데를 끊었습니다.

    **브리핑 자체는 건드리지 않습니다.** 여기서 거르는 것은 카드뿐이고, 블로그와 사이트는
    그대로 다섯 건을 싣습니다 — 근본 해결은 클러스터·프롬프트 쪽이라 따로 봐야 합니다.
    앞선 것(순위가 높은 쪽)을 남깁니다.
    """
    from .cluster import similarity

    kept: list[dict] = []
    for issue in issues:
        text = f"{issue.get('title', '')} {issue.get('one_liner', '')}"
        if any(similarity(text, f"{k.get('title', '')} {k.get('one_liner', '')}") >= threshold
               for k in kept):
            log.debug("카드에서 겹치는 이슈를 뺐습니다: %s", issue.get("title", ""))
            continue
        kept.append(issue)
    return kept


def cards(brief: dict, *, date: str = "", channel: str = "", key_numbers: list[dict] | None = None,
          max_cards: int = 10,
          art: list[Path | tuple[Path, str]] | None = None) -> list[Image]:
    """하루치 브리핑을 유튜브 게시물용 카드 5~7장으로.

    **모델을 새로 부르지 않습니다.** 이미 만들어 둔 브리핑(headline·issues·numbers·
    tomorrow_watch)을 그대로 나눠 담습니다. 그래서 카드를 켜도 하루 비용이 늘지 않습니다.

    구성: 표지(남색) → 오늘의 숫자(짙은 남색) → 이슈 몇 장(밝은 청사진) → 그 밖의 소식(남색) →
    내일 볼 것(남색). 짙음과 밝음이 교차하며 묶음에 박자를 만듭니다.
    **장수는 그날 내용에 따라 정해집니다** — 이슈가 많으면 `max_cards` 까지 늘고,
    자료가 모자란 날은 네댓 장으로 줄어듭니다. 억지로 채우지 않습니다.

    `art` 를 넘기면 표지와 이슈 카드 위쪽에 **그날 만든 인포그래픽**을 얹습니다.
    기사 사진이 아닙니다 — 남의 사진은 저작권이 있어 쓸 수 없고 수집하지도 않습니다.
    쓸 만한 그림이 없는 날은 글자만 있는 카드로 그대로 나갑니다.
    """
    issues = _drop_repeats([i for i in (brief.get("issues") or []) if i.get("title")])
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
    #
    # **장수를 고정하지 않습니다** (2026-09-08, 사용자 지시). 이슈가 많은 날은 늘고
    # 적은 날은 줄어듭니다. `max_cards` 는 상한일 뿐 목표가 아닙니다 — 억지로 채우면
    # 내용 없는 카드가 한 장 더 붙습니다.
    def layout(with_numbers: bool) -> tuple[int, list[dict]]:
        fixed = 1 + int(with_numbers) + int(bool(watch))   # 표지·숫자·내일 볼 것
        room = max(1, max_cards - fixed)                   # 이슈에 쓸 수 있는 장수
        n = min(len(issues), max(1, room - 1))             # '그 밖의 소식' 한 장을 남겨 둔다
        tail = issues[n:]
        if len(tail) == 1:             # 한 건짜리 '그 밖의 소식' 카드는 낭비다 — 제 카드를 준다
            n, tail = n + 1, []
        if len(issues) <= n:           # 이슈가 다 제 카드를 받으면 나머지 카드는 없다
            tail = []
        return n, tail

    # **오늘의 숫자 카드는 이슈 카드가 말하지 않는 수치가 있을 때만 만듭니다**
    # (2026-09-08, 사용자 지시). 예전에는 이슈 카드의 배지를 미리 보여 주는 예고편이라
    # 3번 카드와 낱말이 54% 겹쳤습니다. 겹칠 것이 없으면 한 장을 통째로 뺍니다.
    deep, rest = layout(True)
    told = {f"{dp.get('value', '')}{dp.get('unit', '')}".strip()
            for issue in issues[:deep] for dp in (issue.get("numbers") or [])}
    spare = [dp for dp in nums
             if f"{dp.get('value', '')}{dp.get('unit', '')}".strip() not in told]
    has_numbers = len(spare) >= 2
    if not has_numbers:                # 숫자 카드를 뺀 자리를 이슈에 돌려준다
        deep, rest = layout(False)

    plan = ["cover"]
    if has_numbers:
        plan.append("numbers")
    plan += ["issue"] * deep
    if rest:
        plan.append("rest")
    if watch:
        plan.append("watch")
    plan = plan[:max_cards]
    total = len(plan)

    # 쓸 만한 그림만 미리 걸러 둔다 (세로로 긴 지도·읽을 수 없는 것은 여기서 빠진다).
    bands = []
    for item in art or []:
        path, credit = item if isinstance(item, tuple) else (item, "")
        band = _art_band(path)
        if band:
            band["credit"] = credit
            bands.append(band)

    out: list[Image] = []
    issue_i = 0
    for n, kind in enumerate(plan, start=1):
        if kind == "cover":
            out.append(_cover_card(headline, brief.get("market_temperature", ""),
                                   badge, total, date, channel,
                                   art=bands.pop(0) if bands else None))
        elif kind == "numbers":
            out.append(_numbers_card(spare, n, total, date, channel))
        elif kind == "issue":
            out.append(_issue_card(issues[issue_i], n, total, date, channel,
                                   art=bands.pop(0) if bands else None))
            issue_i += 1
        elif kind == "rest":
            out.append(_list_card("그 밖의 오늘 소식", [i["title"] for i in rest],
                                  "rest", n, total, date, channel))
        elif kind == "watch":
            out.append(_list_card("내일 볼 것", watch, "watch", n, total, date, channel))
    return out
