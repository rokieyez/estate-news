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
FONT = "'Apple SD Gothic Neo','Noto Sans KR',system-ui,-apple-system,sans-serif"

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
    return [f'<text x="{x:g}" y="{y + i * 26:g}" font-size="{size:g}" fill="{MUTED}">{esc(t)}</text>'
            for i, t in enumerate(parts)]


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

    tw, th, gap = 130, 86, 8
    x0, y0 = 90, 200
    w = x0 * 2 + 6 * tw + 5 * gap
    h = y0 + len(SEOUL_LAYOUT) * (th + gap) - gap + 150

    sub = (extra or {}).get("subtitle", "")
    p = svg_open(w, h)
    p.append(f'<text x="{x0}" y="76" font-size="38" font-weight="700" fill="{INK}">'
             f'{esc(dp["label"].replace(" 수", ""))}</text>')
    if sub:
        p.append(f'<text x="{x0}" y="118" font-size="21" fill="{INK_2}">{esc(sub)}</text>')

    # 범례. 칸마다 이름도 적으므로 색만으로 구분되지는 않는다.
    p.append(f'<rect x="{x0}" y="146" width="17" height="17" rx="4" fill="{BLUE}"/>')
    p.append(f'<text x="{x0 + 25}" y="160" font-size="18" fill="{INK_2}">대상 {taxed}개구</text>')
    p.append(f'<rect x="{x0 + 165}" y="146" width="17" height="17" rx="4" fill="{SURFACE}" '
             f'stroke="{BASELINE}" stroke-width="2"/>')
    p.append(f'<text x="{x0 + 190}" y="160" font-size="18" fill="{INK_2}">'
             f'제외 {len(excluded)}개구 ({"·".join(sorted(excluded))})</text>')

    for r, row in enumerate(SEOUL_LAYOUT):
        for c, gu in row.items():
            x, y = x0 + c * (tw + gap), y0 + r * (th + gap)
            if gu in excluded:
                p.append(f'<rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="8" '
                         f'fill="{SURFACE}" stroke="{BASELINE}" stroke-width="2" '
                         f'stroke-dasharray="6 4"/>')
                p.append(f'<text x="{x + tw / 2:g}" y="{y + th / 2 + 4:g}" font-size="23" '
                         f'text-anchor="middle" fill="{INK_2}">{gu}</text>')
                p.append(f'<text x="{x + tw / 2:g}" y="{y + th / 2 + 27:g}" font-size="15" '
                         f'text-anchor="middle" fill="{MUTED}">제외</text>')
            else:
                p.append(f'<rect x="{x}" y="{y}" width="{tw}" height="{th}" rx="8" fill="{BLUE}"/>')
                p.append(f'<text x="{x + tw / 2:g}" y="{y + th / 2 + 9:g}" font-size="24" '
                         f'font-weight="600" text-anchor="middle" fill="#ffffff">{gu}</text>')

    fy = y0 + len(SEOUL_LAYOUT) * (th + gap) + 26
    notes = ["※ 실제 지형이 아닌 위치 도식입니다. 칸의 모양·크기는 면적과 무관합니다."]
    if dp.get("context"):
        notes.append(f'※ {dp["context"]}')
    notes.append(_source_line(dp, date))
    p += footnotes([n for n in notes if n], x0, fy)
    p.append("</svg>")
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

    w, h = 1000, 560
    bx, bw = 250, 560
    scale = bw / max(before, after)

    p = svg_open(w, h)
    p.append(f'<text x="90" y="76" font-size="38" font-weight="700" fill="{INK}">'
             f'{esc(dp["label"].replace(m.group(0), "").strip() or dp["label"])}, {esc(period)} 사이</text>')
    # 델타는 색만으로 뜻을 전하지 않도록 화살표와 글자를 함께 둔다.
    # 한 <text> 안의 tspan 으로 두어 x 좌표를 손으로 계산하지 않는다.
    p.append(f'<text x="90" y="164" font-size="72" font-weight="700" '
             f'fill="{CRITICAL if down else GOOD}">{"▼" if down else "▲"} '
             f'{esc(dp["value"])}{esc(dp["unit"])}'
             f'<tspan font-size="30" font-weight="400" fill="{INK_2}"> {m.group(1)}</tspan></text>')
    p.append(f'<text x="90" y="205" font-size="19" fill="{INK_2}">'
             f'{esc(dp["label"])}{" · " + esc(period) if period else ""}</text>')
    p.append(f'<text x="90" y="268" font-size="17" fill="{MUTED}">'
             f'지수 비교 · {esc(period)} 전을 100으로 두었을 때</text>')

    for i, (name, val) in enumerate([(f"{period} 전", before), ("현재", after)]):
        y = 300 + i * 78
        bwid = val * scale
        p.append(f'<text x="{bx - 18}" y="{y + 38}" font-size="21" text-anchor="end" '
                 f'fill="{INK_2}">{esc(name)}</text>')
        p.append(f'<path d="{bar_path(bx, y, bwid, 56)}" fill="{BLUE if i == 0 else BLUE_SOFT}"/>')
        inside = bwid > 90
        lx = bx + bwid - 18 if inside else bx + bwid + 16
        p.append(f'<text x="{lx:g}" y="{y + 38}" font-size="26" font-weight="700" '
                 f'text-anchor="{"end" if inside else "start"}" '
                 f'fill="{"#ffffff" if inside else INK}">{val:g}</text>')

    p.append(f'<line x1="{bx}" y1="296" x2="{bx}" y2="{300 + 78 + 56}" '
             f'stroke="{BASELINE}" stroke-width="2"/>')
    p += footnotes([
        f'※ 보도된 {m.group(1)}율({dp["value"]}{dp["unit"]})로 지수화한 값입니다. '
        f'구간별 실제 수치는 보도에 제시되지 않았습니다.',
        (_source_line(dp, date) + (f' — {dp["context"]}' if dp.get("context") else "")).strip(),
    ], 90, 492)
    p.append("</svg>")
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

    w, h = 1000, 600
    px0, px1, py0, py1 = 110, 930, 150, 470
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.15
    lo, hi = lo - pad, hi + pad

    def sx(i: int) -> float:
        return px0 + (px1 - px0) * (i / max(len(points) - 1, 1))

    def sy(v: float) -> float:
        return py1 - (py1 - py0) * ((v - lo) / (hi - lo))

    p = svg_open(w, h)
    p.append(f'<text x="90" y="70" font-size="34" font-weight="700" fill="{INK}">{esc(dp["label"])}</text>')
    p.append(f'<text x="90" y="104" font-size="19" fill="{INK_2}">'
             f'{esc(points[0][0])} ~ {esc(points[-1][0])} · {len(points)}일치 · 단위 {esc(dp.get("unit") or "-")}</text>')

    # 눈금선 4개 + 축 라벨 (가늘게, 뒤로 물러나게)
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = sy(v)
        p.append(f'<line x1="{px0}" y1="{y:.1f}" x2="{px1}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        p.append(f'<text x="{px0 - 12}" y="{y + 5:.1f}" font-size="14" text-anchor="end" fill="{MUTED}">{v:g}</text>')
    if lo < 0 < hi:
        p.append(f'<line x1="{px0}" y1="{sy(0):.1f}" x2="{px1}" y2="{sy(0):.1f}" stroke="{BASELINE}" stroke-width="1.5"/>')

    path = " ".join(f'{"M" if i == 0 else "L"}{sx(i):.1f},{sy(v):.1f}' for i, (_, v) in enumerate(points))
    p.append(f'<path d="{path}" fill="none" stroke="{BLUE}" stroke-width="2.5" stroke-linejoin="round"/>')
    for i, (d, v) in enumerate(points):
        p.append(f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="5" fill="{BLUE}" stroke="{SURFACE}" stroke-width="2"/>')
        # 날짜는 월-일만. 점이 많으면 처음·끝·중간만 적어 겹침을 막는다.
        show = len(points) <= 8 or i in (0, len(points) - 1, len(points) // 2)
        if show:
            p.append(f'<text x="{sx(i):.1f}" y="{py1 + 28}" font-size="14" text-anchor="middle" fill="{MUTED}">{esc(d[5:])}</text>')
    # 직접 라벨은 처음과 끝에만
    for i in (0, len(points) - 1):
        d, v = points[i]
        anchor = "start" if i == 0 else "end"
        p.append(f'<text x="{sx(i):.1f}" y="{sy(v) - 14:.1f}" font-size="18" font-weight="700" '
                 f'text-anchor="{anchor}" fill="{INK}">{v:g}{esc(dp.get("unit") or "")}</text>')

    p += footnotes([
        "※ 매일 기사에서 뽑힌 값을 그대로 이은 것입니다. 발표 기관·기준이 날마다 다를 수 있습니다.",
        _source_line(dp, date),
    ], 90, 530)
    p.append("</svg>")
    return Image("time-series", "\n".join(p), dp["label"])


# ── 3) 수치 카드 (기본형) ────────────────────────────────────


def stat_card(dp: dict, date: str, extra: dict | None = None) -> Image | None:
    """어떤 수치든 받아 큰 숫자 카드로 만든다. 마지막 수단."""
    if not dp.get("value"):
        return None
    w, x = 1000, 90
    maxw = w - x * 2

    label_lines = wrap(dp["label"], 26, maxw)[:2]
    notes = []
    if dp.get("context"):
        notes += [f"※ {ln}" if i == 0 else f"　 {ln}"
                  for i, ln in enumerate(wrap(dp["context"], 16, maxw))]
    if src := _source_line(dp, date):
        notes.append(src)

    # 높이는 내용에 맞춰 계산한다. 고정하면 아래쪽에 빈 공간이 남는다.
    hero_y = 86 + len(label_lines) * 36 + 88
    period_y = hero_y + 48 if dp.get("period") else hero_y
    notes_y = period_y + 66
    h = notes_y + max(len(notes) - 1, 0) * 26 + 46

    p = svg_open(w, h)
    for i, line in enumerate(label_lines):
        p.append(f'<text x="{x}" y="{86 + i * 36}" font-size="26" fill="{INK_2}">{esc(line)}</text>')
    p.append(f'<text x="{x}" y="{hero_y}" font-size="104" font-weight="700" fill="{INK}">'
             f'{esc(dp["value"])}'
             f'<tspan font-size="44" font-weight="400" fill="{INK_2}">'
             f'{esc(dp.get("unit", ""))}</tspan></text>')
    if dp.get("period"):
        p.append(f'<text x="{x}" y="{period_y}" font-size="21" fill="{INK_2}">'
                 f'기준 {esc(dp["period"])}</text>')
    p += footnotes(notes, x, notes_y)
    p.append("</svg>")
    return Image("stat-card", "\n".join(p), dp["label"])


# 정보량이 많은 형태를 먼저 채우고, 남는 자리만 기본 카드로 메운다.
SPECIFIC = (time_series, seoul_district_map, index_comparison)
FALLBACK = (stat_card,)
GENERATORS = SPECIFIC + FALLBACK


# ── 조립 ─────────────────────────────────────────────────────


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
    base_y = (h * (0.50 if portrait else 0.46)) - block_h / 2 + big * 0.85

    p = svg_open(w, h)
    # 왼쪽 세로 강조 막대 + 상단 채널명, 하단 날짜. 사진 없이도 표지로 읽히게.
    p.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="{SURFACE}"/>')
    p.append(f'<rect x="{margin}" y="{base_y - big * 0.85:.0f}" width="14" height="{block_h:.0f}" rx="4" fill="{BLUE}"/>')
    if channel:
        p.append(f'<text x="{margin}" y="{margin + 30}" font-size="{40 if portrait else 32}" '
                 f'font-weight="700" fill="{BLUE}">{esc(channel)}</text>')
    for i, line in enumerate(lines):
        p.append(f'<text x="{margin + 44}" y="{base_y + i * line_h:.0f}" font-size="{big}" '
                 f'font-weight="800" fill="{INK}">{esc(line)}</text>')
    if sub:
        sub_lines = wrap(sub, 44 if portrait else 36, w - margin * 2 - 44)[:2]
        for i, line in enumerate(sub_lines):
            p.append(f'<text x="{margin + 44}" y="{base_y + block_h + 40 + i * 58:.0f}" '
                     f'font-size="{44 if portrait else 36}" fill="{INK_2}">{esc(line)}</text>')
    if date:
        p.append(f'<text x="{margin}" y="{h - margin + 10}" font-size="{34 if portrait else 28}" '
                 f'fill="{MUTED}">{esc(date)}</text>')
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
