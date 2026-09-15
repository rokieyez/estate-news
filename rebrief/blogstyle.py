"""블로그 첨부 그림의 「사진 배경」 판 — 표지·수치 그림·통계 그림.

2026-09-15 사용자 요청: "배경에 관련 사진을 어둡게 깔고 그 위에 하얀 글씨로, 숫자가 강조되는 자료는
그래프 같은 인포그래픽으로". 시안 다섯 개 가운데 **방구석 배경지식은 1번 시네마**(사진을 가득, 아래로
짙어짐), **부돌보 브리핑은 4번 유리판**(흐린 사진 위 반투명 판)을 골랐습니다. 두 저장소가 같은 파일을
쓰고(`tests/test_shared_files.py`), 어느 판인지는 설정 `images.blog_style` 이 정합니다. 비우면 예전 흰 카드.

**어떻게 끼어드나.** `images.py` 의 그림 함수들은 그릴 때 모듈 전역 색(INK·BLUE…)을 읽습니다.
`backdrop()` 이 그 동안만 전역 색을 어두운 판 색으로 바꾸고 `images._BACKDROP` 에 사진을 넣어 두면,
`svg_open` 이 흰 바탕 대신 사진 층을 깔고 `frame_open` 이 흰 카드 대신 판을 그립니다. 그래프 열 가지를
두 벌씩 만들지 않으려는 것입니다. 끝나면 원래 색으로 되돌립니다.

**사진은 기사 사진이 아닙니다** — 무료 사진(Pexels)이고 「본문과 무관」 을 늘 답니다(`photos.py`).
사진이 없는 날도 판은 그대로이고 바탕만 짙은 색이 됩니다.

**작은 글씨의 대비는 하얀 사진 위를 기준으로 잽니다** (`test_small_text_stays_readable_on_a_white_photo`).
사진이 어두울지 밝을지는 그날 알 수 없으므로, 가장 나쁜 경우(흰 하늘)에 덮개만으로 4.5 를 넘겨야 합니다.
"""

from __future__ import annotations

import base64
import re
from contextlib import contextmanager
from pathlib import Path

from . import images as im

# 어두운 판에서 글씨와 선. 이름은 images.py 의 전역 이름 그대로입니다.
_DARK = {
    "INK": "#ffffff",
    "INK_2": "#e8ebe6",
    "MUTED": "#d9ddd6",
    "BASELINE": "rgba(255,255,255,.5)",
    "GRID": "rgba(255,255,255,.16)",
    "SURFACE": "rgba(255,255,255,.05)",
    "CARD_EDGE": "rgba(255,255,255,.26)",
    "CRITICAL": "#ff9e94",
    "GOOD": "#9fe3a4",
    # 러너에는 프리텐다드가 없지만 쓴 글자만 잘라 SVG 에 심으므로(`_embed_fonts`) 어디서나 같게 나옵니다.
    "FONT": "'Pretendard','Apple SD Gothic Neo','Noto Sans CJK KR',sans-serif",
}

STYLES: dict[str, dict] = {
    # 시안 1 · 시네마 — 방구석 배경지식(의사당 판의 금색). 정당 상징색(빨강·파랑)은 쓰지 않습니다.
    "cinema": {
        "palette": {**_DARK, "BLUE": "#eac56a", "BLUE_SOFT": "rgba(234,197,106,.34)", "ORANGE": "#9fd8c5",
                    "CARD": "#111513", "CHIP_BG": "rgba(234,197,106,.18)", "ON_ACCENT": "#0f2f2a"},
        "base": ("#12302a", "#050c0b"),
        "shade": 0.76,              # 그래프 판의 덮개 (위) — 아래로 갈수록 짙어져 각주가 읽힌다
        "shade_bottom": 0.88,
        "cover_shade": (0.35, 0.92),
        "blur": 0,
        "panel": False,
    },
    # 시안 4 · 유리판 — 부돌보 브리핑(청사진 판의 청록).
    "glass": {
        "palette": {**_DARK, "BLUE": "#7fe3ff", "BLUE_SOFT": "rgba(127,227,255,.3)", "ORANGE": "#ffc27a",
                    "CARD": "#0c1822", "CHIP_BG": "rgba(127,227,255,.16)", "ON_ACCENT": "#062033"},
        "base": ("#0d2a44", "#04111c"),
        "shade": 0.72,
        "shade_bottom": 0.72,
        "cover_shade": (0.6, 0.6),
        "blur": 8,
        "panel": True,
    },
}
PANEL_FILL = 0.07             # 유리판의 흰빛. 대비 시험이 이 값까지 넣어 잽니다.
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
DISCLOSURE = "※ 자세한 사항은 중앙선거여론조사심의위원회 홈페이지 참조"


@contextmanager
def backdrop(style: str, photos: list[tuple[Path, str]] | None = None, start: int = 0):
    """이 안에서 그린 블로그 그림은 사진 배경 판이 됩니다. 모르는 판 이름이면 아무것도 바꾸지 않습니다.

    photos 는 (파일, 출처 한 줄) 목록이고 그림마다 돌려 씁니다. start 는 첫 그림이 집을 사진 번호 —
    표지가 그날 첫 사진(국회의사당 같은 대표 장소)을 갖게 하려는 것입니다.
    """
    spec = STYLES.get(style or "")
    if not spec:
        yield False
        return
    loaded = []
    for path, credit in photos or []:
        try:
            blob = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            continue
        loaded.append({"mime": _MIME.get(Path(path).suffix.lower(), "image/jpeg"), "data": blob,
                       "credit": credit})
    saved = {name: getattr(im, name) for name in spec["palette"]}
    for name, value in spec["palette"].items():
        setattr(im, name, value)
    im._BACKDROP = {"style": style, "spec": spec, "photos": loaded, "turn": start}
    try:
        yield True
    finally:
        for name, value in saved.items():
            setattr(im, name, value)
        im._BACKDROP = None


def active_style() -> str:
    return (im._BACKDROP or {}).get("style", "")


# ── 바탕 층 ──────────────────────────────────────────────────


def layers(w: float, h: float, *, cover: bool = False) -> list[str]:
    """짙은 바탕 → 사진 → 덮개 → 사진 출처. `images.svg_open` 이 흰 바탕 대신 부릅니다."""
    state = im._BACKDROP
    spec = state["spec"]
    top, bottom = spec["base"]
    out = [f'<defs><linearGradient id="bd-base" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/></linearGradient></defs>',
           f'<rect width="{w:g}" height="{h:g}" fill="url(#bd-base)"/>']
    photos = state["photos"]
    if not photos:
        return out
    photo = photos[state["turn"] % len(photos)]
    state["turn"] += 1
    blur = spec["blur"]
    pad = blur * 4                  # 흐리게 하면 가장자리가 투명해진다 — 사진을 조금 크게 깐다
    effect = ""
    if blur:
        out.append(f'<defs><filter id="bd-blur" x="-5%" y="-5%" width="110%" height="110%">'
                   f'<feGaussianBlur stdDeviation="{blur}"/></filter></defs>')
        effect = ' filter="url(#bd-blur)"'
    out.append(f'<image xlink:href="data:{photo["mime"]};base64,{photo["data"]}" x="{-pad}" y="{-pad}" '
               f'width="{w + pad * 2:g}" height="{h + pad * 2:g}" preserveAspectRatio="xMidYMid slice"{effect}/>')
    a, b = spec["cover_shade"] if cover else (spec["shade"], spec["shade_bottom"])
    out += [f'<defs><linearGradient id="bd-shade" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#000" stop-opacity="{a}"/>'
            f'<stop offset="{0.45 if cover else 0}" stop-color="#000" stop-opacity="{a}"/>'
            f'<stop offset="1" stop-color="#000" stop-opacity="{b}"/></linearGradient></defs>',
            f'<rect width="{w:g}" height="{h:g}" fill="url(#bd-shade)"/>']
    if photo["credit"]:
        out.append(f'<text x="{w - 16:g}" y="{h - 14:g}" font-size="13" text-anchor="end" '
                   f'fill="rgba(255,255,255,.66)">{im.esc(photo["credit"])}</text>')
    return out


def panel(w: float, h: float) -> list[str]:
    """`images.frame_open` 의 흰 카드 자리. 유리판은 반투명 판, 시네마는 판 없이 사진 위에 바로."""
    if not im._BACKDROP["spec"]["panel"]:
        return []
    return [f'<rect x="{im.M}" y="{im.M}" width="{w - im.M * 2:g}" height="{h - im.M * 2:g}" rx="24" '
            f'fill="rgba(255,255,255,{PANEL_FILL})" stroke="rgba(255,255,255,.26)" stroke-width="1.5"/>']


# ── 글자 도우미 ──────────────────────────────────────────────


def _mono_width(s: str, size: float) -> float:
    """고정폭 숫자는 0.6em, 한글(대체 글꼴)은 1em 으로 어림한다."""
    return sum(1.0 if ord(ch) > 0x1100 else 0.6 for ch in s) * size


def _clip(s: str, size: float, width: float) -> str:
    while s and im.text_width(s, size) > width:
        s = s[:-2] + "…"
    return s


def _pretty(value: str) -> str:
    """'7000' → '7,000'. 이미 쉼표가 있거나 소수·범위면 그대로 둔다."""
    return f"{int(value):,}" if re.fullmatch(r"\d{4,}", value or "") else (value or "")


def numeral(x: float, y: float, dp: dict, size: float, fill: str, *, anchor: str = "start") -> str:
    """숫자는 고정폭으로 크게, 뒤의 단위는 한 단계 작게. '40%' 에 단위 '%' 가 또 붙지 않게 한다."""
    value = _pretty(str(dp.get("value", "")).strip())
    unit = dp.get("unit") or ""
    if unit and value.endswith(unit):
        unit = ""
    m = re.match(r"^([-+]?[\d.,~]+)(.*)$", value)
    head, tail = (m.group(1), m.group(2) + unit) if m else ("", value + unit)
    parts = []
    if head:
        parts.append(f'<tspan font-family="{im.DISPLAY}" font-weight="700" letter-spacing="{-size * 0.03:.1f}">'
                     f'{im.esc(head)}</tspan>')
    if tail:
        parts.append(f'<tspan font-size="{size * 0.42:.0f}" font-weight="700" fill="{im.INK_2}" dx="{size * 0.06:.0f}">'
                     f'{im.esc(tail)}</tspan>')
    return (f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" text-anchor="{anchor}" fill="{fill}">'
            + "".join(parts) + "</text>")


def _note_lines(text: str, width: float, lead: str = "※ ") -> list[str]:
    lines = im.wrap(f"{lead}{text}", 17, width)
    return [ln if i == 0 else f"　 {ln}" for i, ln in enumerate(lines)]


# ── 표지 ─────────────────────────────────────────────────────


def cover(title: str, *, badge: str = "", channel: str = "", date: str = "",
          size: tuple[int, int] = (1200, 630)) -> im.Image:
    """블로그 글 맨 위 표지(네이버 검색 목록의 썸네일). `backdrop()` 안에서 부릅니다."""
    w, h = size
    p = im.svg_open(w, h, cover=True)
    if active_style() == "glass":
        _glass_cover(p, w, h, title, badge, channel, date)
    else:
        _cinema_cover(p, w, h, title, badge, channel, date)
    p.append("</svg>")
    return im.Image("cover", im._embed_fonts("\n".join(p)), title)


def _title_lines(title: str, size: float, width: float, floor: float) -> tuple[list[str], float]:
    lines = im.wrap(title, size, width)
    while len(lines) > 3 and size > floor:
        size -= 4
        lines = im.wrap(title, size, width)
    return lines[:3], size


def _cinema_cover(p: list[str], w: float, h: float, title: str, badge: str, channel: str, date: str) -> None:
    """시안 1 — 사진을 가득, 제목은 왼쪽 아래. 마지막 줄만 금색."""
    x = 60
    if channel:
        p.append(f'<rect x="{x}" y="46" width="6" height="26" fill="{im.BLUE}"/>')
        p.append(f'<text x="{x + 18}" y="68" font-size="24" font-weight="700" fill="{im.INK}">{im.esc(channel)}</text>')
    if date:
        p.append(f'<text x="{w - x}" y="68" font-size="22" text-anchor="end" font-family="{im.MONO}" '
                 f'fill="rgba(255,255,255,.85)">{im.esc(date.replace("-", "."))}</text>')
    lines, size = _title_lines(title, 64, w - x * 2, 44)
    line_h = size * 1.22
    last = h - 82
    first = last - line_h * (len(lines) - 1)
    if badge:
        text = f"오늘의 숫자 {badge}"
        bw = im.text_width(text, 22) + 44
        by = first - size - 56
        p.append(f'<rect x="{x}" y="{by:.0f}" width="{bw:.0f}" height="42" rx="21" fill="{im.BLUE}"/>')
        p.append(f'<text x="{x + bw / 2:.0f}" y="{by + 29:.0f}" font-size="22" font-weight="700" '
                 f'text-anchor="middle" fill="{im.ON_ACCENT}">{im.esc(text)}</text>')
    for i, line in enumerate(lines):
        fill = im.BLUE if len(lines) > 1 and i == len(lines) - 1 else im.INK
        p.append(f'<text x="{x}" y="{first + i * line_h:.0f}" font-size="{size:g}" font-weight="800" '
                 f'fill="{fill}">{im.esc(line)}</text>')


def _glass_cover(p: list[str], w: float, h: float, title: str, badge: str, channel: str, date: str) -> None:
    """시안 4 — 흐린 사진 위 반투명 판 한가운데에 제목."""
    pw = w - 300
    lines, size = _title_lines(title, 58, pw - 120, 40)
    line_h = size * 1.3
    ph = 84 + line_h * len(lines) + 92
    px, py = (w - pw) / 2, (h - ph) / 2
    p.append(f'<rect x="{px:g}" y="{py:.0f}" width="{pw:g}" height="{ph:.0f}" rx="28" '
             f'fill="rgba(255,255,255,.1)" stroke="rgba(255,255,255,.32)" stroke-width="1.5"/>')
    if channel:
        p.append(f'<text x="{w / 2:g}" y="{py + 64:.0f}" font-size="24" font-weight="700" text-anchor="middle" '
                 f'fill="{im.BLUE}">{im.esc(channel)}</text>')
    for i, line in enumerate(lines):
        p.append(f'<text x="{w / 2:g}" y="{py + 84 + line_h * (i + 1) - line_h * 0.22:.0f}" font-size="{size:g}" '
                 f'font-weight="800" text-anchor="middle" fill="{im.INK}">{im.esc(line)}</text>')
    foot = "  |  ".join(b for b in (f"오늘의 숫자 {badge}" if badge else "", date.replace("-", ".")) if b)
    if foot:
        p.append(f'<text x="{w / 2:g}" y="{py + ph - 40:.0f}" font-size="22" text-anchor="middle" '
                 f'fill="{im.INK}">{im.esc(foot)}</text>')


# ── 수치 그림 ────────────────────────────────────────────────

# 같은 단위로 바꿔 견줄 수 있는 것들. 값은 가장 작은 단위로 몇인지.
_FAMILIES = {
    "time": {"년": 12, "개월": 1, "달": 1, "주": 12 / 52, "일": 12 / 365},
    "money": {"조원": 1e12, "조": 1e12, "억원": 1e8, "억": 1e8, "만원": 1e4, "원": 1},
}
# 크기로 견주면 뜻이 틀어지는 단위 — 비율·순위·배수는 막대 길이가 크기가 아니다.
_NOT_SIZES = {"%", "%p", "배", "위", "포인트", "p", "대1"}


def _measure(dp: dict) -> tuple[str, float] | None:
    """(단위 갈래, 가장 작은 단위로 바꾼 크기). 범위·비율 같은 것은 None."""
    value, unit = str(dp.get("value", "")), dp.get("unit") or ""
    text = value if value.endswith(unit) else value + unit
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([가-힣%]*)\s*", text.replace(",", ""))
    if not m:
        return None
    number, unit = float(m.group(1)), m.group(2)
    if number <= 0:
        return None
    for family, table in _FAMILIES.items():
        if unit in table:
            return family, number * table[unit]
    if unit and unit not in _NOT_SIZES:
        return f"count:{unit}", number
    return None


_PER_UNIT = re.compile(r"[가-힣㎡]+당|차이|격차|상승액|증가액|감소액|인상액")


def _words(label: str) -> set[str]:
    return {w for w in re.split(r"[\s·,()\[\]『』「」'\"]+", label or "") if len(w) >= 2}


def pair_of(dp: dict, pool: list[dict]) -> dict | None:
    """같은 이슈에서 같은 갈래 단위로 견줄 수 있는 다른 수치 하나 (예: 보유 17년 ↔ 실거주 4개월).

    **금액은 짝짓지 않습니다.** 2026-09-14 부동산 자료에 「분양가 상승액 1억1000만원」 과 「3.3㎡당 분양가
    2000만원」 이 한 이슈에 있었습니다 — 단위는 같아도 하나는 오른 폭, 하나는 단가라 막대로 견주면 거짓말이
    됩니다. '~당'·차이·증가액 같은 수치도 같은 이유로 뺍니다. 그리고 라벨이 낱말 하나는 나눠야
    (기간·상승·증인…) 같은 것을 잰 수치로 봅니다.
    """
    mine = _measure(dp)
    if not mine or mine[0] == "money" or not dp.get("issue") or _PER_UNIT.search(dp.get("label", "")):
        return None
    for other in pool:
        if other is dp or other.get("label") == dp.get("label") or other.get("issue") != dp.get("issue"):
            continue
        if _PER_UNIT.search(other.get("label", "")) or not _words(dp.get("label", "")) & _words(other.get("label", "")):
            continue
        theirs = _measure(other)
        if theirs and theirs[0] == mine[0]:
            return other
    return None


_SHARE_WORDS = ("비율", "비중", "점유율", "찬성", "반대", "응답", "투표율", "가율", "참여율", "보급률",
                "공실률", "달성률", "취업률", "실업률", "자가율")
_CHANGE_WORDS = ("증감", "변동", "상승", "하락", "증가", "감소", "올라", "내려", "떨어", "폭", "대비")


def share_value(dp: dict) -> float | None:
    """전체(100) 가운데 몫으로 읽히는 % 만. 증감률을 원그래프로 그리면 거짓말이 된다."""
    unit = (dp.get("unit") or "").lower()
    value = str(dp.get("value", ""))
    if "%p" in unit or "%p" in value.lower() or ("%" not in unit and not value.strip().endswith("%")):
        return None
    label = dp.get("label", "")
    if not any(w in label for w in _SHARE_WORDS) or any(w in label for w in _CHANGE_WORDS):
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", value)
    v = float(m.group(1)) if m else None
    return v if v is not None and 0 < v <= 100 else None


def stat_card(dp: dict, date: str, extra: dict | None = None) -> im.Image | None:
    """사진 배경 판의 수치 그림. `images.stat_card` 가 판이 켜져 있을 때 넘깁니다.

    생김새대로 셋 중 하나 — ① 같은 이슈에 견줄 수치가 있으면 두 막대 ② 몫으로 읽히는 % 면
    유리판은 원, 시네마는 100칸 막대 ③ 그 밖에는 큰 숫자. 없는 값은 지어내지 않습니다.
    """
    if not dp.get("value"):
        return None
    extra = extra or {}
    w = 1000
    inner = w - (im.M + im.P) * 2
    other = pair_of(dp, extra.get("datapoints") or [])
    share = None if other else share_value(dp)
    ring = active_style() == "glass"

    notes = _note_lines(dp["context"], inner) if dp.get("context") else []
    if other:
        same_unit = (other.get("unit") or "") == (dp.get("unit") or "")
        notes.append("※ 막대 길이는 두 수치의 크기를 견준 것입니다."
                     + ("" if same_unit else " 서로 다른 단위는 같은 단위로 환산했습니다."))
    if src := im._source_line(dp, date):
        notes.append(src)
    sub = f'기준 {dp["period"]}' if dp.get("period") else ""
    # 두 막대면 막대마다 라벨이 붙으므로 제목은 이슈 이름 — 첫 막대 라벨을 제목에 또 쓰지 않는다
    title = (dp.get("issue") or dp["label"]) if other else dp["label"]
    content = 2 * 112 if other else (270 if share is not None and ring else 230 if share is not None else 150)
    h = im.card_height(w, title, sub, content, notes)
    p, g = im.frame_open(w, h, title=title, subtitle=sub, channel=im._channel(extra), date=date)
    x, y = g["x"], g["top"]

    if other:
        rows = sorted([dp, other], key=lambda r: -_measure(r)[1])
        biggest = _measure(rows[0])[1]
        bar_w = inner - 250
        for i, row in enumerate(rows):
            top = y + i * 112
            mine = row is dp
            p.append(f'<text x="{x}" y="{top + 16}" font-size="21" fill="{im.INK_2}">'
                     f'{im.esc(_clip(row.get("label", ""), 21, inner))}</text>')
            width = max(6.0, bar_w * _measure(row)[1] / biggest)
            p.append(f'<path d="{im.bar_path(x, top + 34, width, 46, 6)}" fill="{im.BLUE if mine else im.BLUE_SOFT}"/>')
            p.append(numeral(x + width + 18, top + 72, row, 40, im.INK))
    elif share is not None and ring:
        p.append(numeral(x, y + 118, dp, 112, im.BLUE))
        p.append(f'<text x="{x}" y="{y + 170}" font-size="22" fill="{im.INK_2}">전체 100 가운데 {share:g}</text>')
        cx, cy, r, stroke = x + inner - 140, y + 128, 112, 30
        circ = 2 * 3.141592653589793 * r
        p.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{im.GRID}" stroke-width="{stroke}"/>')
        p.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{im.BLUE}" stroke-width="{stroke}" '
                 f'stroke-dasharray="{circ * share / 100:.1f} {circ:.1f}" transform="rotate(-90 {cx} {cy})"/>')
        p.append(f'<text x="{cx}" y="{cy + 13}" font-size="36" font-weight="700" text-anchor="middle" '
                 f'font-family="{im.DISPLAY}" fill="{im.INK}">{share:g}%</text>')
    elif share is not None:
        p.append(numeral(x, y + 100, dp, 112, im.BLUE))
        track_y = y + 138
        p.append(f'<rect x="{x}" y="{track_y}" width="{inner}" height="40" rx="6" fill="{im.GRID}"/>')
        p.append(f'<path d="{im.bar_path(x, track_y, inner * share / 100, 40, 6)}" fill="{im.BLUE}"/>')
        for frac, label, anchor in ((0, "0", "start"), (0.5, "50%", "middle"), (1, "100%", "end")):
            p.append(f'<text x="{x + inner * frac:g}" y="{track_y + 70}" font-size="17" text-anchor="{anchor}" '
                     f'fill="{im.MUTED}">{label}</text>')
        p.append(f'<line x1="{x + inner / 2:g}" y1="{track_y - 8}" x2="{x + inner / 2:g}" y2="{track_y + 48}" '
                 f'stroke="{im.BASELINE}" stroke-width="1.5" stroke-dasharray="4 4"/>')
    else:
        p.append(numeral(x, y + 104, dp, 120, im.BLUE))
        p.append(f'<rect x="{x}" y="{y + 130}" width="96" height="6" rx="3" fill="{im.BLUE}"/>')
    im.frame_close(p, notes, g)
    return im.Image("stat-card", im._embed_fonts("\n".join(p)), dp["label"],
                    covers=[other["label"]] if other else [])


# ── 여론조사 ─────────────────────────────────────────────────

_POLL_WORDS = ("지지율", "지지도", "국정수행", "국정 수행", "긍정평가", "긍정 평가", "부정평가", "부정 평가",
               "여론조사", "적합도", "호감도", "비호감")
_MARGIN = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%\s*p", re.I)


def poll_value(dp: dict) -> float | None:
    """여론조사 결과로 읽히는 % (0~100). 차이(%p)·증감은 뺍니다 — 막대로 그리면 몫처럼 읽힙니다."""
    unit = (dp.get("unit") or "").lower()
    value = str(dp.get("value", "")).strip()
    label = dp.get("label", "")
    if "%p" in unit or "포인트" in unit or "%p" in value.lower():
        return None
    if "%" not in unit and not value.endswith("%"):
        return None
    if not any(w in label for w in _POLL_WORDS) or any(w in label for w in _CHANGE_WORDS):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%?", value)
    v = float(m.group(1)) if m else None
    return v if v is not None and 0 < v <= 100 else None


def _match_poll(rows: list[dict], polls: list[dict]) -> dict | None:
    """수치 설명에 조사기관 이름이 나오면 선관위 등록부의 그 조사. 이름이 없으면 짐작하지 않습니다."""
    text = re.sub(r"\s", "", " ".join(f'{r.get("label", "")}{r.get("context", "")}{r.get("source", "")}'
                                      for r in rows))
    for poll in polls or []:
        name = re.sub(r"\(주\)|㈜|주식회사|\s", "", str(poll.get("agency", "")))
        if len(name) >= 2 and name in text:
            return poll
    return None


def poll_chart(dp: dict, date: str, extra: dict | None = None) -> im.Image | None:
    """여론조사 % 를 0~100 막대로. 같은 이슈의 다른 조사 수치(긍정·부정 등)는 한 판에 모읍니다.

    그림에도 조사 개요를 답니다(공직선거법 제108조). 수치 설명에 조사기관이 나오고 선관위 등록부에
    그 기관이 있으면 등록부의 개요를, 없으면 **보도에 개요가 없었다고** 적습니다 — 지어 넣지 않습니다.
    오차범위를 알면 막대 끝에 옅은 띠로 보여 줍니다(띠 안의 차이는 앞선다고 읽으면 안 됩니다).
    """
    if poll_value(dp) is None:
        return None
    extra = extra or {}
    pool = extra.get("datapoints") or []
    rows = [dp] + [o for o in pool if o is not dp and o.get("label") != dp.get("label") and dp.get("issue")
                   and o.get("issue") == dp.get("issue") and poll_value(o) is not None][:4]
    w = 1000
    inner = w - (im.M + im.P) * 2
    poll = _match_poll(rows, extra.get("polls") or [])
    overview = (poll or {}).get("summary", "")
    found = _MARGIN.search(" ".join(f'{r.get("context", "")} {r.get("source", "")}' for r in rows) + " " + overview)
    margin = float(found.group(1)) if found else None

    notes = _note_lines(dp["context"], inner) if dp.get("context") and len(rows) == 1 else []
    if overview:
        notes += _note_lines(f"조사 개요: {overview}", inner)
    else:
        notes.append("※ 보도에 조사기관·조사기간·표본 수·오차범위가 나오지 않았습니다.")
    if margin:
        notes.append(f"※ 옅은 띠는 오차범위(±{margin:g}%p)입니다. 띠 안의 차이는 앞섰다고 볼 수 없습니다.")
    notes.append(DISCLOSURE)
    if src := im._source_line(dp, date):
        notes.append(src)

    title = dp["issue"] if len(rows) > 1 and dp.get("issue") else dp["label"]
    sub = f'기준 {dp["period"]}' if dp.get("period") else ""
    row_h = 100
    h = im.card_height(w, title, sub, 30 + row_h * len(rows), notes)
    p, g = im.frame_open(w, h, title=title, subtitle=sub, channel=im._channel(extra), date=date)
    x, y = g["x"], g["top"] + 20
    bar_w = inner - 150

    mid = x + bar_w / 2
    p.append(f'<text x="{mid:g}" y="{y - 4}" font-size="15" text-anchor="middle" fill="{im.MUTED}">50%</text>')
    p.append(f'<line x1="{mid:g}" y1="{y + 4}" x2="{mid:g}" y2="{y + row_h * len(rows) - 12}" '
             f'stroke="{im.BASELINE}" stroke-width="1.5" stroke-dasharray="4 4"/>')
    for i, row in enumerate(rows):
        v = poll_value(row)
        top = y + i * row_h
        p.append(f'<text x="{x}" y="{top + 30}" font-size="21" fill="{im.INK_2}">'
                 f'{im.esc(_clip(row.get("label", ""), 21, inner))}</text>')
        p.append(f'<rect x="{x}" y="{top + 44}" width="{bar_w:g}" height="38" rx="6" fill="{im.GRID}"/>')
        p.append(f'<path d="{im.bar_path(x, top + 44, bar_w * v / 100, 38, 6)}" '
                 f'fill="{im.BLUE if i == 0 else im.BLUE_SOFT}"/>')
        if margin:
            lo, hi = max(0.0, v - margin), min(100.0, v + margin)
            p.append(f'<rect x="{x + bar_w * lo / 100:.1f}" y="{top + 38}" width="{bar_w * (hi - lo) / 100:.1f}" '
                     f'height="50" rx="4" fill="{im.INK}" opacity=".22"/>')
        p.append(f'<text x="{x + bar_w + 18:g}" y="{top + 75}" font-size="34" font-weight="700" '
                 f'font-family="{im.DISPLAY}" fill="{im.INK}">{v:g}%</text>')
    im.frame_close(p, notes, g)
    return im.Image("poll-chart", im._embed_fonts("\n".join(p)), title,
                    covers=[r["label"] for r in rows[1:]])
