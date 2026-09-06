"""산출물 생성 — 마크다운 문서, SRT 자막, 데이터 JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import markdown as markdown_lib
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import images as images_mod
from .config import Config
from .models import BlogPost, CaptionLine, Cluster, DailyBrief, VideoPack

TEMPLATE_DIR = Path(__file__).parent / "templates"


@dataclass
class RenderStats:
    articles: int = 0
    publishers: int = 0
    feeds_ok: int = 0
    feeds_total: int = 0


def make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,   # 오타 난 변수는 조용히 비는 대신 에러를 낸다
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


class Renderer:
    def __init__(self, cfg: Config, out_dir: Path, date_str: str):
        self.cfg = cfg
        self.out_dir = out_dir
        self.date = date_str
        self.env = make_env()
        self.written: list[Path] = []
        out_dir.mkdir(parents=True, exist_ok=True)

    # ── 개별 산출물 ──────────────────────────────────────────

    def brief(self, brief: DailyBrief, stats: RenderStats) -> Path:
        return self._write(
            "brief.md",
            "brief.md.j2",
            brief=brief,
            stats=stats,
            date=self.date,
            generated_at=_now(),
        )

    def brief_fallback(self, clusters: list[Cluster], stats: RenderStats) -> Path:
        return self._write(
            "brief.md",
            "brief_fallback.md.j2",
            clusters=clusters,
            stats=stats,
            date=self.date,
            generated_at=_now(),
        )

    def blog(self, post: BlogPost, clusters: list[Cluster],
             slot_files: dict[int, str] | None = None) -> Path:
        blog_cfg = self.cfg.get("blog", {}) or {}
        return self._write(
            "blog.md",
            "blog.md.j2",
            post=post,
            body_markdown=place_images_markdown(post.body_markdown, slot_files or {}),
            clusters=clusters,
            date=self.date,
            frontmatter=bool(blog_cfg.get("frontmatter", True)),
            category=blog_cfg.get("category", "부동산"),
            disclaimer=blog_cfg.get("disclaimer", ""),
        )

    def blog_naver(self, post: BlogPost, slot_files: dict[int, str] | None = None,
                   filename: str = "blog-naver.html") -> Path:
        """네이버 스마트에디터에 붙여넣을 HTML. 브라우저로 열어 버튼으로 복사한다."""
        blog_cfg = self.cfg.get("blog", {}) or {}
        return self._write(
            filename,
            "blog_naver.html.j2",
            post=post,
            date=self.date,
            category=blog_cfg.get("category", "부동산"),
            body_html=to_naver_html(post.body_markdown, slot_files or {}),
            hashtags=format_hashtags(post.tags),
            write_url=(blog_cfg.get("naver", {}) or {}).get(
                "write_url", "https://blog.naver.com/"
            ) or "https://blog.naver.com/",
        )

    def shorts(self, pack: VideoPack) -> list[Path]:
        shorts = pack.shorts
        char_count = sum(len(line.text) for line in shorts.lines)
        graphic_cuts = sum(
            1 for line in shorts.lines
            if any(word in line.visual for word in ("자막", "카드", "그래픽", "차트"))
        )
        paths = [
            self._write(
                "script-shorts.md",
                "script_shorts.md.j2",
                s=shorts,
                date=self.date,
                char_count=char_count,
                graphic_cuts=graphic_cuts,
            )
        ]
        srt = to_srt(shorts.lines, shorts.estimated_seconds)
        if srt:
            paths.append(self._write_raw("script-shorts.srt", srt))
        return paths

    def longform(self, pack: VideoPack) -> Path:
        longform = pack.longform
        char_count = sum(len(s.script) for s in longform.sections) + len(longform.cold_open)
        return self._write(
            "script-longform.md",
            "script_longform.md.j2",
            l=longform,
            date=self.date,
            char_count=char_count,
        )

    def production_notes(self, brief: DailyBrief, pack: VideoPack) -> Path:
        return self._write(
            "production-notes.md",
            "production_notes.md.j2",
            brief=brief,
            s=pack.shorts,
            l=pack.longform,
            date=self.date,
            datapoints=flatten_datapoints(brief),
        )

    def sources(
        self,
        clusters: list[Cluster],
        stats: RenderStats,
        feed_errors: list[dict],
        leftovers: list,
        link_status: dict | None = None,
    ) -> Path:
        link_status = link_status or {}
        dead = {url: st.note for url, st in link_status.items() if not st.ok}
        return self._write(
            "sources.md",
            "sources.md.j2",
            clusters=clusters,
            stats=stats,
            feed_errors=feed_errors,
            leftovers=leftovers,
            date=self.date,
            dead_links=dead,
            checked_links=len(link_status),
        )

    def data_json(self, brief: DailyBrief) -> Path:
        payload = {
            "date": self.date,
            "headline": brief.headline,
            "market_temperature": brief.market_temperature,
            "issues": [
                {
                    "title": issue.title,
                    "category": issue.category,
                    "one_liner": issue.one_liner,
                    "numbers": [n.model_dump() for n in issue.numbers],
                }
                for issue in brief.issues
            ],
            "datapoints": flatten_datapoints(brief),
        }
        return self._write_raw(
            "data.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def images(self, brief: DailyBrief, history: list[dict] | None = None,
               post: BlogPost | None = None) -> dict[int, str]:
        """수치를 인포그래픽으로 만든다. 블로그 글이 있으면 그 이미지 자리에 맞춰 만든다.

        돌려주는 값은 {자리 번호: 파일명}. 그릴 게 없는 날은 빈 dict.
        """
        cfg = self.cfg.get("images", {}) or {}
        if not cfg.get("enabled", True):
            return {}
        datapoints = flatten_datapoints(brief)
        limit = int(cfg.get("max", 3))
        slot_labels = [s.datapoint_label for s in (post.image_slots if post else [])]
        by_slot, extras = images_mod.build_for_slots(
            datapoints, self.date, slot_labels,
            headline=brief.headline, history=history or [], limit=limit,
        ) if slot_labels else ({}, images_mod.build(
            datapoints, self.date, headline=brief.headline, limit=limit, history=history or [],
        ))

        slot_files: dict[int, str] = {}
        for slot_no, img in by_slot.items():
            slot_files[slot_no] = self._write_image(img, cfg)
        for img in extras:
            self._write_image(img, cfg)
        return slot_files

    def thumbnails(self, pack: VideoPack) -> list[Path]:
        """롱폼·쇼츠 표지. 제목 후보와 썸네일 문구는 대본 생성 때 이미 나와 있다."""
        cfg = self.cfg.get("images", {}) or {}
        if not cfg.get("enabled", True) or not cfg.get("thumbnails", True):
            return []
        channel = str((self.cfg.get("video", {}) or {}).get("channel_name", "") or "")
        jobs = []
        if pack.longform.thumbnail_texts:
            jobs.append(("thumb-longform", pack.longform.thumbnail_texts[0],
                         (pack.longform.title_candidates or [""])[0], (1280, 720)))
        if pack.shorts.title_candidates:
            jobs.append(("thumb-shorts", pack.shorts.title_candidates[0], pack.shorts.hook, (1080, 1920)))
        paths: list[Path] = []
        for slug, text, sub, size in jobs:
            img = images_mod.thumbnail(text, sub=sub, channel=channel, date=self.date, size=size)
            img.slug = slug
            self._write_image(img, cfg, prefix="")
            paths.append(self.out_dir / f"{slug}.svg")
        return paths

    def shorts_draft(self, pack: VideoPack) -> Path | None:
        """자막 카드를 이어 붙인 쇼츠 초안 mp4. ffmpeg·크롬이 없으면 None."""
        video_cfg = self.cfg.get("video", {}) or {}
        if not video_cfg.get("draft", True):
            return None
        from .video import build_shorts_draft

        pngs = sorted(self.out_dir.glob("img-*.png"))
        path = build_shorts_draft(
            self.out_dir, pack.shorts, pngs,
            channel=str(video_cfg.get("channel_name", "") or ""),
            fps=int(video_cfg.get("draft_fps", 30)),
        )
        if path:
            self.written.append(path)
        return path

    def _write_image(self, img, cfg: dict, prefix: str = "img-") -> str:
        """SVG 를 쓰고, 되면 PNG 도 쓴다. 본문에서 가리킬 파일명(PNG 우선)을 돌려준다."""
        svg = self._write_raw(f"{prefix}{img.slug}.svg", img.svg)
        if cfg.get("png", True):
            png = svg.with_suffix(".png")
            if images_mod.svg_to_png(svg, png, int(cfg.get("png_scale", 2))):
                self.written.append(png)
                return png.name
        return svg.name

    def prompt_pack(self, text: str) -> Path:
        return self._write_raw("prompt-pack.md", text)

    # ── 내부 ─────────────────────────────────────────────────

    def _write(self, filename: str, template: str, **context) -> Path:
        rendered = self.env.get_template(template).render(**context)
        return self._write_raw(filename, rendered)

    def _write_raw(self, filename: str, content: str) -> Path:
        path = self.out_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.written.append(path)
        return path


# ── 헬퍼 ─────────────────────────────────────────────────────


_IMAGE_SLOT = re.compile(r"<p>\s*\[이미지\s*:\s*(.*?)\]\s*</p>", re.DOTALL)
_IMAGE_SLOT_INLINE = re.compile(r"\[이미지\s*:\s*(.*?)\]", re.DOTALL)


def to_naver_html(body_markdown: str, slot_files: dict[int, str] | None = None) -> str:
    """마크다운 본문을 네이버 에디터가 이해하는 HTML 로 바꾼다.

    스마트에디터는 마크다운을 모른다. 대신 클립보드에 서식 있는 HTML 이 들어오면
    제목·표·굵게·목록을 그대로 받아들이므로, 의미 태그(h2/table/strong/ul)로
    변환해 두고 브라우저에서 복사하게 한다.

    slot_files 가 있으면 해당 자리의 점선 상자에 파일명을 적고, 그 아래 미리보기
    이미지를 붙인다. 미리보기는 복사에 포함되지 않는다(class="nocopy").
    """
    html = markdown_lib.markdown(
        body_markdown or "",
        extensions=["tables", "sane_lists"],
        output_format="html",
    )
    slot_files = slot_files or {}
    counter = {"n": 0}

    def slot(match: re.Match) -> str:
        counter["n"] += 1
        caption = " ".join(match.group(1).split())
        filename = slot_files.get(counter["n"])
        if not filename:
            return f'<div class="imgslot">📷 이미지 — {caption}</div>'
        return (
            f'<div class="imgslot has-file">📷 이미지 — {caption}'
            f'<br><small>→ 파일 <b>{filename}</b> 을 이 자리에 올리고 상자는 지웁니다</small></div>'
            f'<img class="preview nocopy" src="{filename}" alt="{caption}">'
        )

    html = _IMAGE_SLOT.sub(slot, html)
    html = _IMAGE_SLOT_INLINE.sub(slot, html)   # 문단 안에 섞여 들어온 경우
    return html


def place_images_markdown(body_markdown: str, slot_files: dict[int, str]) -> str:
    """마크다운 판에는 자리에 맞는 그림을 실제 이미지 문법으로 넣는다. 못 맞춘 자리는 그대로."""
    if not slot_files:
        return body_markdown
    counter = {"n": 0}

    def slot(match: re.Match) -> str:
        counter["n"] += 1
        caption = " ".join(match.group(1).split())
        filename = slot_files.get(counter["n"])
        return f"![{caption}]({filename})" if filename else match.group(0)

    return _IMAGE_SLOT_INLINE.sub(slot, body_markdown or "")


def format_hashtags(tags: list[str]) -> str:
    """네이버는 본문에 쓴 #해시태그를 그대로 블로그 태그로 등록한다."""
    seen: list[str] = []
    for tag in tags or []:
        cleaned = tag.strip().lstrip("#").replace(" ", "")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return " ".join(f"#{t}" for t in seen[:30])   # 네이버 태그 상한 30개


def flatten_datapoints(brief: DailyBrief) -> list[dict]:
    """이슈별로 흩어진 수치를 표/차트용 평면 리스트로 모은다."""
    rows: list[dict] = []
    for issue in brief.issues:
        for number in issue.numbers:
            row = number.model_dump()
            row["issue"] = issue.title
            row["category"] = issue.category
            rows.append(row)
    return rows


_TIMECODE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?$")


def parse_timecode(value: str) -> float | None:
    """'00:03', '1:02:03', '3' 을 초로 바꾼다. 못 읽으면 None."""
    value = (value or "").strip()
    if not value:
        return None
    match = _TIMECODE.match(value)
    if match:
        hours, minutes, seconds, millis = match.groups()
        total = int(minutes) * 60 + int(seconds)
        if hours:
            total += int(hours) * 3600
        if millis:
            total += int(millis.ljust(3, "0")) / 1000
        return float(total)
    try:
        return float(value)
    except ValueError:
        return None


def _srt_stamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_timings(lines: list[CaptionLine], total_seconds: int | None = None) -> list[tuple[float, float]]:
    """자막 줄마다 (시작, 끝) 초.

    끝 시각은 '다음 자막의 시작'으로 잡고 마지막만 전체 길이 또는 +3초로 닫는다.
    타임코드를 못 읽으면 앞 자막 뒤 2.5초, 시간이 거꾸로 가면 +0.5초로 단조 증가를 강제한다.
    SRT 와 쇼츠 초안 영상이 같은 규칙을 쓴다.
    """
    starts: list[float] = []
    for line in lines:
        parsed = parse_timecode(line.at)
        if parsed is None:
            parsed = (starts[-1] + 2.5) if starts else 0.0
        if starts and parsed <= starts[-1]:
            parsed = starts[-1] + 0.5
        starts.append(parsed)
    if not starts:
        return []
    tail = float(total_seconds) if total_seconds else starts[-1] + 3.0
    if tail <= starts[-1]:
        tail = starts[-1] + 3.0
    return [(start, starts[i + 1] if i + 1 < len(starts) else tail) for i, start in enumerate(starts)]


def to_srt(lines: list[CaptionLine], total_seconds: int | None = None) -> str:
    """자막 줄 목록을 SRT 파일 내용으로 변환한다. 편집 프로그램에서 그대로 임포트된다."""
    timings = caption_timings(lines, total_seconds)
    if not timings:
        return ""
    blocks = [
        f"{index + 1}\n{_srt_stamp(start)} --> {_srt_stamp(end)}\n{line.text}\n"
        for index, (line, (start, end)) in enumerate(zip(lines, timings))
    ]
    return "\n".join(blocks)


def update_index(cfg: Config) -> Path | None:
    """output/INDEX.md 에 날짜별 산출물 목록을 갱신한다."""
    if not cfg.get("output.write_index", True):
        return None

    out_dir = cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    days = sorted(
        (p for p in out_dir.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
        reverse=True,
    )

    lines = [
        "# 산출물 목록",
        "",
        f"마지막 갱신 {_now()} · 총 {len(days)}일치",
        "",
        "| 날짜 | 브리핑 | 블로그 | 네이버 | 쇼츠 | 롱폼 | 제작메모 | 데이터 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in days[:60]:
        def cell(filename: str, label: str) -> str:
            return f"[{label}]({day.name}/{filename})" if (day / filename).exists() else "—"

        lines.append(
            f"| **{day.name}** "
            f"| {cell('brief.md', '브리핑')} "
            f"| {cell('blog.md', '블로그')} "
            f"| {cell('blog-naver.html', 'HTML')} "
            f"| {cell('script-shorts.md', '쇼츠')} "
            f"| {cell('script-longform.md', '롱폼')} "
            f"| {cell('production-notes.md', '메모')} "
            f"| {cell('data.json', 'JSON')} |"
        )

    lines += _weekly_section(out_dir)
    lines += _cost_section(cfg)

    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _weekly_section(out_dir: Path) -> list[str]:
    weekly = out_dir / "weekly"
    if not weekly.exists():
        return []
    weeks = sorted((p for p in weekly.iterdir() if p.is_dir()), reverse=True)
    if not weeks:
        return []
    lines = ["", "## 주간 결산", "", "| 주차 | 결산 | 네이버 | 데이터 |", "| --- | --- | --- | --- |"]
    for wk in weeks[:26]:
        def cell(filename: str, label: str) -> str:
            return f"[{label}](weekly/{wk.name}/{filename})" if (wk / filename).exists() else "—"
        lines.append(f"| **{wk.name}** | {cell('weekly.md', '결산')} | {cell('weekly-naver.html', 'HTML')} | {cell('data.json', 'JSON')} |")
    return lines


def _cost_section(cfg: Config) -> list[str]:
    """state/costs.json 이 있으면 최근 비용 요약을 INDEX 에 붙인다."""
    from .store import CostLog

    log_ = CostLog(cfg.state_dir / "costs.json")
    if not log_.entries:
        return []
    krw = float(cfg.get("llm.krw_per_usd", 1400))
    week_usd, week_days = log_.recent(7)
    month_usd, month_days = log_.recent(30)
    by_date = log_.by_date()
    avg = (sum(by_date.values()) / len(by_date)) if by_date else 0.0
    lines = [
        "",
        "## 비용 (실측)",
        "",
        f"환율 {krw:,.0f}원/$ 기준 · 기록 {len(by_date)}일 · 하루 평균 ${avg:.3f} (약 {avg * krw:,.0f}원)",
        "",
        "| 기간 | 실행일 | 비용 |",
        "| --- | --- | --- |",
        f"| 최근 7일 | {week_days}일 | ${week_usd:.3f} (약 {week_usd * krw:,.0f}원) |",
        f"| 최근 30일 | {month_days}일 | ${month_usd:.3f} (약 {month_usd * krw:,.0f}원) |",
        "",
        "<details><summary>날짜별</summary>",
        "",
        "| 날짜 | 비용 |",
        "| --- | --- |",
    ]
    for d, usd in list(by_date.items())[-30:][::-1]:
        lines.append(f"| {d} | ${usd:.3f} |")
    lines += ["", "</details>"]
    return lines


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
