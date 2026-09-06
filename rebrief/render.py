"""산출물 생성 — 마크다운 문서, SRT 자막, 데이터 JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

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

    def blog(self, post: BlogPost, clusters: list[Cluster]) -> Path:
        blog_cfg = self.cfg.get("blog", {}) or {}
        return self._write(
            "blog.md",
            "blog.md.j2",
            post=post,
            clusters=clusters,
            date=self.date,
            frontmatter=bool(blog_cfg.get("frontmatter", True)),
            category=blog_cfg.get("category", "부동산"),
            disclaimer=blog_cfg.get("disclaimer", ""),
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
    ) -> Path:
        return self._write(
            "sources.md",
            "sources.md.j2",
            clusters=clusters,
            stats=stats,
            feed_errors=feed_errors,
            leftovers=leftovers,
            date=self.date,
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


def to_srt(lines: list[CaptionLine], total_seconds: int | None = None) -> str:
    """자막 줄 목록을 SRT 파일 내용으로 변환한다.

    각 자막의 끝 시각은 '다음 자막의 시작'으로 잡는다. 마지막 자막만
    전체 길이 또는 +3초로 닫는다. 편집 프로그램에서 그대로 임포트된다.
    """
    starts: list[float] = []
    for index, line in enumerate(lines):
        parsed = parse_timecode(line.at)
        if parsed is None:
            # 타임코드를 못 읽으면 앞 자막 뒤에 2.5초 간격으로 이어 붙인다.
            parsed = (starts[-1] + 2.5) if starts else 0.0
        # 시간이 거꾸로 가면 SRT 가 깨지므로 단조 증가를 강제한다.
        if starts and parsed <= starts[-1]:
            parsed = starts[-1] + 0.5
        starts.append(parsed)

    if not starts:
        return ""

    tail = float(total_seconds) if total_seconds else starts[-1] + 3.0
    if tail <= starts[-1]:
        tail = starts[-1] + 3.0

    blocks: list[str] = []
    for index, (line, start) in enumerate(zip(lines, starts)):
        end = starts[index + 1] if index + 1 < len(starts) else tail
        blocks.append(
            f"{index + 1}\n{_srt_stamp(start)} --> {_srt_stamp(end)}\n{line.text}\n"
        )
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
        "| 날짜 | 브리핑 | 블로그 | 쇼츠 | 롱폼 | 제작메모 | 데이터 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in days[:60]:
        def cell(filename: str, label: str) -> str:
            return f"[{label}]({day.name}/{filename})" if (day / filename).exists() else "—"

        lines.append(
            f"| **{day.name}** "
            f"| {cell('brief.md', '브리핑')} "
            f"| {cell('blog.md', '블로그')} "
            f"| {cell('script-shorts.md', '쇼츠')} "
            f"| {cell('script-longform.md', '롱폼')} "
            f"| {cell('production-notes.md', '메모')} "
            f"| {cell('data.json', 'JSON')} |"
        )

    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
