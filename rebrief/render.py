"""산출물 생성 — 마크다운 문서, SRT 자막, 데이터 JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import markdown as markdown_lib
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import images as images_mod
from . import keynumbers as kn
from .config import Config
from .models import BlogPost, CaptionLine, Cluster, DailyBrief, VideoPack
from .sanitize import clean_html

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
        # HTML 템플릿은 변수를 자동 이스케이프한다(모델이 쓴 제목의 < > 가 태그가 되지 않게).
        # 이미 HTML 인 값(body_html 등)은 템플릿에서 |safe 로 표시한다. 마크다운 템플릿은 그대로.
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False, default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


class Renderer:
    def __init__(self, cfg: Config, out_dir: Path, date_str: str):
        self.stats_images: dict[str, str] = {}   # 실거래가 그림 (블로그에서 다시 쓴다)
        self.cfg = cfg
        self.out_dir = out_dir
        self.date = date_str
        self.env = make_env()
        self.written: list[Path] = []
        self.last_link_status: dict = {}
        out_dir.mkdir(parents=True, exist_ok=True)

    # ── 개별 산출물 ──────────────────────────────────────────

    def brief(self, brief: DailyBrief, stats: RenderStats, checks: list | None = None,
              diff: dict | None = None, why: list[str] | None = None) -> Path:
        checks = checks or []
        return self._write(
            "brief.md",
            "brief.md.j2",
            brief=brief,
            stats=stats,
            why=why or [],
            date=self.date,
            generated_at=_now(),
            diff=diff or {},
            checks=checks,
            unverified=[c for c in checks if c.status == "미확인"],
            unchecked=[c for c in checks if c.status == "대조 불가"],
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
             slot_files: dict[int, str] | None = None,
             key_numbers: list | None = None, related: list[dict] | None = None,
             cover: str = "", policies: list | None = None,
             stats: dict | None = None, stats_image: str = "") -> Path:
        blog_cfg = self.cfg.get("blog", {}) or {}
        return self._write(
            "blog.md",
            "blog.md.j2",
            post=post,
            tags=self._tags(post),
            cover=cover,
            lead_block=lead_block_markdown(post.summary_lines, outline_from_markdown(post.body_markdown))
                       + terms_block_markdown(self._terms(post)),
            tail_block=takeaways_block_markdown(post.takeaways)
                       + stats_block_markdown(stats, stats_image)
                       + policy_block_markdown(policies)
                       + tail_block_markdown(post.closing_question, related),
            key_card=kn.card_markdown(key_numbers or []),
            body_markdown=place_images_markdown(post.body_markdown, slot_files or {}),
            clusters=clusters,
            date=self.date,
            frontmatter=bool(blog_cfg.get("frontmatter", True)),
            category=blog_cfg.get("category", "부동산"),
            disclaimer=blog_cfg.get("disclaimer", ""),
        )

    def blog_naver(self, post: BlogPost, slot_files: dict[int, str] | None = None,
                   filename: str = "blog-naver.html", key_numbers: list | None = None,
                   related: list[dict] | None = None, cover: str = "",
                   policies: list | None = None, stats: dict | None = None,
                   stats_image: str = "") -> Path:
        """네이버 스마트에디터에 붙여넣을 HTML. 브라우저로 열어 버튼으로 복사한다."""
        blog_cfg = self.cfg.get("blog", {}) or {}
        photo_links = {
            i: photo_search_links(slot.search_keywords or slot.description)
            for i, slot in enumerate(post.image_slots, start=1)
            if i not in (slot_files or {})
        }
        return self._write(
            filename,
            "blog_naver.html.j2",
            post=post,
            date=self.date,
            category=blog_cfg.get("category", "부동산"),
            body_html=to_naver_html(
                post.body_markdown, slot_files or {}, photo_links,
                highlight_min=int(blog_cfg.get("highlight_repeats", 3) or 0),
                key_numbers=key_numbers,
                summary_lines=post.summary_lines,
                closing_question=post.closing_question,
                related=related,
                cover=cover,
                terms=self._terms(post),
                takeaways=post.takeaways,
                policies=policies,
                stats=stats,
                stats_image=stats_image,
            ),
            hashtags=format_hashtags(self._tags(post)),
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
        video_cfg = self.cfg.get("video", {}) or {}
        srt = to_srt(shorts.lines, shorts.estimated_seconds,
                     int(video_cfg.get("caption_max_chars", 16)),
                     int(video_cfg.get("caption_max_lines", 2)))
        if srt:
            paths.append(self._write_raw("script-shorts.srt", srt))
        # 편집 프로그램에 그대로 넣는 컷 리스트. 그림은 이미 만들어져 있으므로 파일명을 짚어 준다.
        pictures = [p.name for p in sorted(self.out_dir.glob("img-*.png"))] or \
                   [p.name for p in sorted(self.out_dir.glob("img-*.svg"))]
        cuts = shorts_cut_csv(shorts, pictures, int(video_cfg.get("cut_list_fps", 30)))
        if cuts:
            paths.append(self._write_raw("shorts-cuts.csv", cuts))
        return paths

    def longform(self, pack: VideoPack) -> Path:
        longform = pack.longform
        char_count = sum(len(s.script) for s in longform.sections) + len(longform.cold_open)
        path = self._write(
            "script-longform.md",
            "script_longform.md.j2",
            l=longform,
            date=self.date,
            char_count=char_count,
        )
        chapters = longform_chapter_csv(longform, int((self.cfg.get("video", {}) or {}).get("cut_list_fps", 30)))
        if chapters:
            self._write_raw("longform-chapters.csv", chapters)
        return path

    def production_notes(self, brief: DailyBrief, pack: VideoPack) -> Path:
        video = self.cfg.get("video", {}) or {}
        blog = self.cfg.get("blog", {}) or {}
        disclaimer = str(blog.get("disclaimer", "") or "")
        return self._write(
            "production-notes.md",
            "production_notes.md.j2",
            brief=brief,
            s=pack.shorts,
            l=pack.longform,
            date=self.date,
            datapoints=flatten_datapoints(brief),
            # 설명란·고정 댓글은 모델이 아니라 여기서 만든다
            description=youtube_description(
                brief, pack, disclaimer=disclaimer,
                cta=str(video.get("cta", "") or "")),
            pinned=pinned_comment(brief, disclaimer),
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
        self.last_link_status = link_status
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
            "tomorrow_watch": list(brief.tomorrow_watch or []),
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

    def cover(self, post: BlogPost, key_numbers: list | None = None) -> str:
        """글 맨 위에 올릴 표지 이미지. 네이버 검색 목록의 썸네일은 보통 본문 첫 이미지다.

        돌려주는 값은 파일명(없으면 빈 문자열). 사람이 그 파일을 글 맨 위에 올린다.
        """
        cfg = self.cfg.get("images", {}) or {}
        if not cfg.get("enabled", True) or not cfg.get("cover", True):
            return ""
        sub = (post.summary_lines or [""])[0]
        badge = key_numbers[0].display if key_numbers else ""
        img = images_mod.thumbnail(
            post.title, sub=sub, badge=badge, size=(1200, 630),
            channel=str((self.cfg.get("video", {}) or {}).get("channel_name", "") or ""),
            date=self.date,
        )
        img.slug = "0-cover"
        return self._write_image(img, cfg)

    def thumbnails(self, pack: VideoPack, key_numbers: list | None = None) -> list[Path]:
        """롱폼·쇼츠 표지. 제목 후보와 썸네일 문구는 대본 생성 때 이미 나와 있다.
        오늘의 첫 번째 핵심 수치가 있으면 모서리 배지로 붙인다."""
        cfg = self.cfg.get("images", {}) or {}
        if not cfg.get("enabled", True) or not cfg.get("thumbnails", True):
            return []
        channel = str((self.cfg.get("video", {}) or {}).get("channel_name", "") or "")
        variants = max(1, int(cfg.get("thumbnail_variants", 2)))
        jobs = []
        # 1안·2안 — 문구 후보 순서대로. 제목 기록장과 같은 번호를 쓴다.
        for n, text in enumerate(pack.longform.thumbnail_texts[:variants], start=1):
            jobs.append((f"thumb-longform{'' if n == 1 else f'-{n}'}", text,
                         (pack.longform.title_candidates or [""])[0], (1280, 720)))
        for n, text in enumerate(pack.shorts.title_candidates[:variants], start=1):
            jobs.append((f"thumb-shorts{'' if n == 1 else f'-{n}'}", text, pack.shorts.hook, (1080, 1920)))
        paths: list[Path] = []
        badge = key_numbers[0].display if key_numbers else ""
        for slug, text, sub, size in jobs:
            img = images_mod.thumbnail(text, sub=sub, channel=channel, date=self.date, size=size, badge=badge)
            img.slug = slug
            self._write_image(img, cfg, prefix="")
            paths.append(self.out_dir / f"{slug}.svg")
        return paths

    def _terms(self, post: BlogPost) -> list[tuple[str, str]]:
        """글에 나온 어려운 말 풀이. 사전은 사람이 적어 둔 고정 문장을 쓴다."""
        from .glossary import explain

        limit = int((self.cfg.get("blog", {}) or {}).get("glossary_max", 3) or 0)
        return explain(post.body_markdown, limit) if limit else []

    def _tags(self, post: BlogPost) -> list[str]:
        """모델 태그 + 글에 나온 지역 + 고정 태그로 네이버 30칸을 채운다."""
        from .regions import find_regions

        naver = (self.cfg.get("blog", {}) or {}).get("naver", {}) or {}
        found = find_regions(f"{post.title}\n{post.body_markdown}", limit=4)
        return expand_tags(post.tags, found, list(naver.get("fixed_tags", []) or []),
                           limit=int(naver.get("tag_count", 30)))

    def _write_image(self, img, cfg: dict, prefix: str = "img-", scale: int | None = None) -> str:
        """SVG 를 쓰고, 되면 PNG 도 쓴다. 본문에서 가리킬 파일명(PNG 우선)을 돌려준다.

        PNG 가 만들어졌으면 SVG 는 지웁니다(`images.keep_svg`). 같은 그림이 두 벌씩 날마다
        저장소에 쌓이기 때문입니다. 변환에 실패한 날은 SVG 가 유일한 결과물이라 그대로 둡니다.
        """
        svg = self._write_raw(f"{prefix}{img.slug}.svg", img.svg)
        if cfg.get("png", True):
            png = svg.with_suffix(".png")
            if images_mod.svg_to_png(svg, png, int(scale or cfg.get("png_scale", 2))):
                self.written.append(png)
                if not cfg.get("keep_svg", False):
                    svg.unlink(missing_ok=True)
                    if svg in self.written:
                        self.written.remove(svg)
                return png.name
        return svg.name

    def stats(self, data: dict, series: dict | None = None,
              history: list[dict] | None = None, history_region: str = "",
              jeonse_history: list[dict] | None = None) -> Path | None:
        """실거래가 집계표와 그림. 자료가 없으면 아무것도 만들지 않는다."""
        if not data or not data.get("districts"):
            return None
        cfg = self.cfg.get("images", {}) or {}
        files: dict[str, str] = {}
        if cfg.get("enabled", True):
            extra = {"channel": str((self.cfg.get("video", {}) or {}).get("channel_name", "") or "")}
            made = (
                ("volume", images_mod.trade_volume_bar(data, self.date, extra)),
                ("index", images_mod.price_index_line(series or {}, self.date, extra)),
                ("history", images_mod.trade_history_line(history or [], history_region,
                                                          self.date, extra)),
                ("jeonse", images_mod.jeonse_history_line(jeonse_history or [], history_region,
                                                          self.date, extra)),
                ("map", images_mod.district_choropleth(data, self.date, extra)),
                ("map_jeonse", images_mod.district_choropleth(data, self.date, extra,
                                                             metric="jeonse")),
            )
            scale = int(cfg.get("png_scale_stats", 1) or cfg.get("png_scale", 2))
            for key, img in made:
                if img:
                    files[key] = self._write_image(img, cfg, scale=scale)
        self.stats_images = files
        # 검색 색인이 읽을 수 있게 집계 결과를 그대로 한 벌 남긴다 (그림 경로는 뺀다)
        import json as _json

        self._write_raw("stats.json", _json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        # 템플릿은 StrictUndefined 라 빠진 항목이 있으면 바로 터진다. 예전에 모은 자료도
        # 그릴 수 있게 새로 생긴 항목의 기본값을 먼저 깔아 둔다.
        payload = {"rent": [], "sizes": [], "map": {}, "map_jeonse": {}, "swings": [],
                   "warnings": [], **data}
        return self._write("stats.md", "stats.md.j2", index=index_table(series or {}),
                           images=files, history_region=history_region, **payload)

    def policy(self, docs: list) -> Path | None:
        """정부 발표 원문 3줄 요약 + 원본 파일. 없으면 파일을 만들지 않는다."""
        if not docs:
            return None
        return self._write("policy.md", "policy.md.j2", docs=docs, date=self.date)

    def prompt_pack(self, text: str) -> Path:
        return self._write_raw("prompt-pack.md", text)

    def checklist(self, result, artifacts: dict, link_status: dict | None = None) -> Path:
        """발행 전 점검표. md 는 사람이, json 은 사이트 카드가 읽는다."""
        from . import checklist as cl

        items = cl.build(
            self.cfg,
            brief=artifacts.get("brief"), post=artifacts.get("post"), pack=artifacts.get("pack"),
            checks=artifacts.get("checks"), link_status=link_status or {},
            warnings=result.warnings, llm_used=result.llm_used,
            empty_photo_slots=int(artifacts.get("empty_photo_slots", 0) or 0),
            repeats=artifacts.get("repeats") or [],
            prev_bodies=artifacts.get("prev_bodies") or [],
        )
        if artifacts.get("autofixed"):
            items.insert(0, cl.Item("autofix", cl.WARN, f"금지 표현 문장 {len(artifacts['autofixed'])}개를 자동으로 고쳐 씀",
                                    "고친 문장이 자연스러운지만 확인하세요.", list(artifacts["autofixed"])))
        summary = cl.summarize(items)
        self._write_raw("checklist.json", json.dumps({
            "date": self.date, "summary": summary,
            "items": [{"key": i.key, "level": i.level, "title": i.title, "detail": i.detail, "lines": i.lines}
                      for i in items],
        }, ensure_ascii=False, indent=2) + "\n")
        return self._write("checklist.md", "checklist.md.j2", date=self.date, items=items, summary=summary)

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


def to_naver_html(body_markdown: str, slot_files: dict[int, str] | None = None,
                  photo_links: dict[int, list[tuple[str, str]]] | None = None,
                  highlight_min: int = 3, key_numbers: list | None = None,
                  summary_lines: list[str] | None = None, closing_question: str = "",
                  related: list[dict] | None = None, outline: bool = True,
                  cover: str = "", terms: list[tuple[str, str]] | None = None,
                  takeaways: list[str] | None = None, policies: list | None = None,
                  stats: dict | None = None, stats_image: str = "") -> str:
    """마크다운 본문을 네이버 에디터가 이해하는 HTML 로 바꾼다.

    스마트에디터는 마크다운을 모른다. 대신 클립보드에 서식 있는 HTML 이 들어오면
    제목·표·굵게·목록을 그대로 받아들이므로, 의미 태그(h2/table/strong/ul)로
    변환해 두고 브라우저에서 복사하게 한다.

    slot_files 가 있으면 해당 자리의 점선 상자에 파일명을 적고, 그 아래 미리보기
    이미지를 붙인다. 미리보기는 복사에 포함되지 않는다(class="nocopy").
    highlight_min 회 이상 되풀이되는 수치와 key_numbers(오늘의 핵심 수치)는 강조한다.
    key_numbers 가 있으면 맨 위에 '오늘의 숫자' 카드를 붙인다.
    """
    html = clean_html(markdown_lib.markdown(
        body_markdown or "",
        extensions=["tables", "sane_lists"],
        output_format="html",
    ))
    slot_files = slot_files or {}
    photo_links = photo_links or {}
    counter = {"n": 0}

    def slot(match: re.Match) -> str:
        counter["n"] += 1
        caption = " ".join(match.group(1).split())
        filename = slot_files.get(counter["n"])
        if not filename:
            links = photo_links.get(counter["n"]) or []
            box = f'<div class="imgslot">📷 이미지 — {caption}</div>'
            if links:
                anchors = " · ".join(f'<a href="{url}" target="_blank" rel="noopener">{name}</a>' for name, url in links)
                box += f'<div class="photo-links nocopy">사진 찾기: {anchors}</div>'
            return box
        return (
            f'<div class="imgslot has-file">📷 이미지 — {caption}'
            f'<br><small>→ 파일 <b>{filename}</b> 을 이 자리에 올리고 상자는 지웁니다</small></div>'
            f'<img class="preview nocopy" src="{filename}" alt="{caption}">'
        )

    html = highlight_repeated_numbers(html, highlight_min, {n.key for n in (key_numbers or [])})
    html = _IMAGE_SLOT.sub(slot, html)
    html = _IMAGE_SLOT_INLINE.sub(slot, html)   # 문단 안에 섞여 들어온 경우
    head = (cover_block_html(cover)
            + lead_block_html(summary_lines, outline_from_markdown(body_markdown) if outline else [])
            + terms_block_html(terms or []))
    return (head + kn.card_html(key_numbers or []) + html
            + takeaways_block_html(takeaways)
            + stats_block_html(stats, stats_image) + policy_block_html(policies)
            + tail_block_html(closing_question, related))


_HL_STYLE = "background-color:#fff59d"


def highlight_repeated_numbers(html: str, min_count: int = 3, keys: set[str] | None = None) -> str:
    """수치를 세 단계로 강조한다.

    · 핵심 수치(keys — 브리핑 datapoint)와 min_count 회 이상 되풀이되는 수치를 '중요' 로 본다.
    · 중요 수치의 첫 등장: 형광펜. 핵심 수치면 굵게도. 두 번째부터는 밑줄만(눈이 덜 피로하게).
    · '집값' 같은 글자는 세지 않고 숫자+단위만 센다. '3억 원' 과 '3억원' 은 같은 수치.
    · 태그·엔티티·이미지 자리·소제목은 건드리지 않는다.
    인라인 style 을 쓰는 이유: 네이버 에디터가 class 는 버려도 background-color 는 살리기 때문.
    """
    if not html:
        return html
    keys = set(keys or ())
    counts = kn.keys_in(html) if min_count > 0 else {}
    important = {k for k, n in counts.items() if n >= min_count} | keys
    if not important:
        return html
    seen: set[str] = set()

    def wrap(m: re.Match) -> str:
        key = kn.number_key(m)
        if key not in important:
            return m.group(0)
        if key in seen:
            return f"<u>{m.group(0)}</u>"
        seen.add(key)
        inner = f"<strong>{m.group(0)}</strong>" if key in keys else m.group(0)
        return f'<span class="hl" style="{_HL_STYLE}">{inner}</span>'

    parts = kn.OPAQUE.split(html)          # 짝수 칸이 글자, 홀수 칸이 태그류
    for i in range(0, len(parts), 2):
        parts[i] = kn.NUM_TOKEN.sub(wrap, parts[i])
    return "".join(parts)



def youtube_description(brief: "DailyBrief", pack: "VideoPack", *, disclaimer: str = "",
                        cta: str = "", limit: int = 8) -> str:
    """유튜브 설명란을 프로그램이 조립한다.

    예전에는 모델에게 설명란 전문을 쓰게 했는데, 여기 들어갈 것은 **챕터 타임코드**와
    **출처 주소** 뿐이고 둘 다 우리가 이미 가진 값입니다. 모델이 다시 쓰면 돈이 들고,
    매번 형식이 조금씩 달라집니다.
    """
    lines: list[str] = []
    if brief.headline:
        lines += [brief.headline, ""]
    if getattr(brief, "lead", ""):
        lines += [brief.lead, ""]

    lines.append("── 챕터 ──")
    lines.append("00:00 콜드오픈")
    for section in pack.longform.sections:
        at = (section.at or "").strip()
        lines.append(f"{at} {section.chapter}".strip())
    lines.append("")

    urls: list[str] = []
    for issue in brief.issues:
        for url in (issue.source_urls or []):
            if url not in urls:
                urls.append(url)
    if urls:
        lines.append("── 출처 ──")
        lines += urls[:limit]
        lines.append("")
    if cta:
        lines.append(cta)
    if disclaimer:
        lines.append(disclaimer)
    return "\n".join(lines).strip() + "\n"


def pinned_comment(brief: "DailyBrief", disclaimer: str = "") -> str:
    """고정 댓글도 같은 이유로 프로그램이 만든다. 이슈 한 줄 요약 3개 + 주의 문구."""
    lines = [f"· {i.one_liner}" for i in brief.issues[:3] if i.one_liner]
    if not lines:
        lines = [brief.headline] if brief.headline else []
    if disclaimer:
        lines += ["", disclaimer]
    return "\n".join(lines)


def explain_issues(brief: DailyBrief, clusters: list[Cluster], tz: str = "Asia/Seoul") -> list[str]:
    """이슈마다 '왜 이게 뽑혔는지' 한 줄. 모델을 부르지 않고 수집 결과만 센다.

    이슈와 묶음(cluster)은 근거 기사 주소가 겹치는 것으로 맞춘다. 모델이 순서를 바꾸거나
    제목을 다르게 붙여도 주소는 그대로이기 때문이다. 겹치는 게 없으면 순서대로 짝짓는다.
    """
    lines: list[str] = []
    for index, issue in enumerate(brief.issues):
        urls = set(issue.source_urls or [])
        best, best_hits = None, 0
        for rank, cluster in enumerate(clusters):
            hits = sum(1 for a in cluster.articles if a.url in urls)
            if hits > best_hits:
                best, best_hits = (rank, cluster), hits
        if best is None:
            best = (index, clusters[index]) if index < len(clusters) else None
        if best is None:
            lines.append("")
            continue
        rank, cluster = best
        publishers = {a.publisher or a.feed_name for a in cluster.articles if (a.publisher or a.feed_name)}
        parts = [f"오늘 {rank + 1}위", f"매체 {len(publishers)}곳", f"기사 {len(cluster.articles)}건"]
        latest = max((a.published for a in cluster.articles if a.published), default=None)
        if latest is not None:
            parts.append(f"최신 {_to_local(latest, tz).strftime('%m-%d %H:%M')}")
        lines.append(" · ".join(parts))
    return lines


def _to_local(moment: datetime, tz: str = "Asia/Seoul") -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(tz))
    except Exception:
        return moment


_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def outline_from_markdown(body_markdown: str) -> list[str]:
    """본문의 ## 소제목 목록. 글 맨 앞 목차로 쓴다 (네이버는 앵커 링크가 살지 않으므로 글자만)."""
    return [" ".join(m.group(1).split()) for m in _H2.finditer(body_markdown or "") if m.group(1).strip()]


def _esc(text: str) -> str:
    from html import escape

    return escape(" ".join((text or "").split()))


def cover_block_html(filename: str) -> str:
    """표지 이미지 자리. 검색 결과 목록에 뜨는 썸네일이 되므로 본문 맨 위에 온다."""
    if not filename:
        return ""
    return (
        '<div class="imgslot has-file">📷 대표 이미지 — 검색 목록에 이 그림이 썸네일로 뜹니다'
        f'<br><small>→ 파일 <b>{filename}</b> 을 <b>글 맨 위</b>에 올리고 상자는 지웁니다</small></div>'
        f'<img class="preview nocopy" src="{filename}" alt="대표 이미지">'
    )


def lead_block_html(summary_lines: list[str] | None, outline: list[str] | None) -> str:
    """글 맨 앞 요약 3줄과 목차. 검색으로 들어온 사람이 스크롤 없이 판단하게 한다."""
    parts = []
    if summary_lines:
        rows = "".join(f'<li style="margin-bottom:6px">{_esc(line)}</li>' for line in summary_lines[:3])
        parts.append(
            '<div style="background-color:#f2f8ff;border-left:4px solid #256abf;padding:14px 16px;margin:0 0 22px">'
            '<b style="font-size:15px">3줄 요약</b>'
            f'<ul style="margin:8px 0 0;padding-left:18px">{rows}</ul></div>'
        )
    if outline and len(outline) >= 3:
        # 요약 상자 바로 아래에 오므로 눈에 덜 무겁게 — 글씨를 한 단계 줄이고 줄 간격을 좁힌다
        rows = "".join(f'<li style="margin-bottom:2px">{_esc(t)}</li>' for t in outline)
        parts.append(
            '<div style="background-color:#f7f8fa;padding:12px 16px;margin:0 0 22px;font-size:14px">'
            '<b>이 글의 순서</b>'
            f'<ol style="margin:6px 0 0;padding-left:20px;line-height:1.6">{rows}</ol></div>'
        )
    return "".join(parts)


def terms_block_html(terms: list[tuple[str, str]]) -> str:
    """낯선 말 풀이. 배경지식 없는 사람이 첫 문단에서 막히지 않게 요약 바로 아래 둔다."""
    if not terms:
        return ""
    rows = "".join(
        f'<li style="margin-bottom:5px"><b>{_esc(t)}</b> — {_esc(m)}</li>' for t, m in terms
    )
    return ('<div style="background-color:#fff8e1;padding:12px 16px;margin:0 0 22px;font-size:14px">'
            '<b>낯선 말 풀이</b>'
            f'<ul style="margin:6px 0 0;padding-left:18px;line-height:1.6">{rows}</ul></div>')


def takeaways_block_html(takeaways: list[str] | None) -> str:
    """'그래서 나는?' — 남 얘기를 내 얘기로 바꾸는 자리. 본문 끝에 둔다."""
    if not takeaways:
        return ""
    rows = "".join(f'<li style="margin-bottom:6px">{_esc(t)}</li>' for t in takeaways[:3])
    return ('<div style="background-color:#f2f8ff;border-left:4px solid #256abf;'
            'padding:14px 16px;margin:26px 0 0">'
            '<b style="font-size:15px">그래서 나는?</b>'
            f'<ul style="margin:8px 0 0;padding-left:18px">{rows}</ul></div>')


def terms_block_markdown(terms: list[tuple[str, str]]) -> str:
    if not terms:
        return ""
    return "**낯선 말 풀이**\n\n" + "\n".join(f"- **{t}** — {m}" for t, m in terms) + "\n"


def takeaways_block_markdown(takeaways: list[str] | None) -> str:
    if not takeaways:
        return ""
    return "**그래서 나는?**\n\n" + "\n".join(f"- {t}" for t in takeaways[:3]) + "\n"


def policy_block_html(docs: list | None) -> str:
    """블로그 끝에 붙는 정부 발표 원문 링크. 글은 짧게 두고 링크만 건다."""
    if not docs:
        return ""
    rows = []
    for d in docs:
        files = " · ".join(f'<a href="{_esc(f["url"])}">{_esc(f["name"])[:40]}</a>' for f in (d.files or [])[:2])
        rows.append(
            f'<li style="margin-bottom:8px"><a href="{_esc(d.url)}">{_esc(d.title)}</a>'
            f' <span style="color:#888888;font-size:13px">({_esc(d.dept)} {_esc(d.date)})</span>'
            + (f'<br><span style="font-size:13px">첨부 {files}</span>' if files else "")
            + "</li>"
        )
    return ('<div style="margin:24px 0 0;padding:14px 16px;background-color:#f7f8fa">'
            '<b>오늘 나온 정부 발표 원문</b>'
            '<ul style="margin:8px 0 0;padding-left:18px">' + "".join(rows) + "</ul></div>")


def _eok(amount: float) -> str:
    """원 단위를 '12.5억' 으로. 글에서 읽기 쉬운 단위는 억이다."""
    return f"{amount / 100_000_000:.1f}억"


def index_table(series: dict) -> dict:
    """지수 여러 개를 기준일로 맞춘 표. 템플릿에서 다시 짝지을 필요가 없게 여기서 정리한다."""
    names = [name for name, rows in (series or {}).items() if rows]
    if not names:
        return {}
    by_time: dict[str, dict] = {}
    for name in names:
        for row in series[name]:
            key = row.get("when") or row.get("time", "")
            by_time.setdefault(key, {"when": key, "by_name": {}})["by_name"][name] = row["value"]
    region = series[names[0]][0].get("region", "") or "전국"
    # 표는 여기서 줄 단위로 만들어 넘긴다. 템플릿 안에서 반복문을 겹치면 줄바꿈이 먹힌다.
    def cell(value) -> str:
        return f"{value:.2f}" if isinstance(value, (int, float)) else "—"

    lines = [
        "| 기준일 | " + " | ".join(names) + " |",
        "| --- |" + " ---: |" * len(names),
    ] + [
        f"| {by_time[k]['when']} | " + " | ".join(cell(by_time[k]["by_name"].get(n)) for n in names) + " |"
        for k in sorted(by_time)
    ]
    return {"names": names, "region": region, "lines": lines,
            "rows": [by_time[k] for k in sorted(by_time)]}


def stats_block_html(data: dict | None, image: str = "") -> str:
    """직접 센 실거래 숫자. 모델이 지어낼 수 없게 프로그램이 값을 그대로 넣는다."""
    if not data or not data.get("districts"):
        return ""
    rows = data["districts"][:3]
    top = " · ".join(
        f'{_esc(r["name"])} {r["now"]["count"]}건'
        f'({"+" if r["change"] >= 0 else ""}{r["change"]})'
        for r in rows
    )
    parts = [
        '<div style="margin:28px 0 0;padding:16px 18px;background-color:#f7f8fa">',
        f'<b>직접 센 숫자 — {_esc(data.get("month_label", ""))} 아파트 실거래</b>',
        f'<p style="margin:10px 0 0">서울 {len(data["districts"])}개 구에서 신고된 매매는 '
        f'<b>{data["total"]}건</b>입니다. '
        f'{_esc(data.get("before_label", ""))} {data["total_before"]}건과 견주면 '
        f'{data["total"] - data["total_before"]:+d}건입니다.</p>',
        f'<p style="margin:8px 0 0;font-size:15px;color:#555555">거래가 많은 곳: {top}</p>',
    ]
    jeonse = (data.get("jeonse") or [])[:1]
    if jeonse:
        j = jeonse[0]
        parts.append(
            f'<p style="margin:10px 0 0;font-size:15px;color:#555555">'
            f'{_esc(j["name"])} 전세가율(전세 보증금 ÷ 매매가)은 가운뎃값 <b>{j["median"]}%</b>입니다. '
            f'같은 단지·같은 면적 {j["count"]}곳을 견줬습니다.</p>')
    swings = (data.get("swings") or [])[:2]
    if swings:
        # 단지 하나가 아니라 구 전체가 움직인 이야기다. 신고가와 성격이 달라 따로 적는다.
        moved = " · ".join(
            f'{_esc(s["name"])} {s["pct"]:+.1f}%({s["before"]}→{s["now"]}건'
            + (f', {_esc(s["hotspot"]["dong"])}에 {s["hotspot"]["share"]}% 몰림' if s.get("hotspot") else "")
            + ")"
            for s in swings)
        parts.append(
            f'<p style="margin:10px 0 0;font-size:15px;color:#555555">'
            f'서울 25개 구 가운데 거래가 가장 크게 움직인 곳: {moved}</p>')
    hot = [h for h in (data.get("highlights") or []) if h["kind"] == "신고가"][:2]
    if hot:
        items = "".join(
            f'<li>{_esc(h["district"])} {_esc(h["name"])} {h["area"]}㎡ — '
            f'{_eok(h["amount"])} (이전 최고 {_eok(h["before"])}, {h["pct"]:+.1f}%)</li>'
            for h in hot
        )
        parts.append('<p style="margin:12px 0 4px"><b>이번 달 신고가</b></p>'
                     f'<ul style="margin:0;padding-left:18px;font-size:15px">{items}</ul>')
    if image:
        parts.append(f'<img class="preview nocopy" src="{_esc(image)}" alt="지역별 거래 건수" '
                     'style="max-width:100%;margin-top:12px">')
        parts.append('<p style="margin:6px 0 0;font-size:13px;color:#888888">'
                     f'→ 그림 파일 <b>{_esc(image)}</b> 을 이 자리에 올리세요</p>')
    parts.append('<p style="margin:12px 0 0;font-size:13px;color:#888888">'
                 '국토교통부 실거래가 신고 자료를 직접 집계했습니다. '
                 '해제(계약 취소) 신고분은 뺐습니다.</p></div>')
    return "".join(parts)


def stats_block_markdown(data: dict | None, image: str = "") -> str:
    """보관용 blog.md 에도 같은 내용을 남긴다."""
    if not data or not data.get("districts"):
        return ""
    rows = data["districts"][:3]
    top = " · ".join(f'{r["name"]} {r["now"]["count"]}건({r["change"]:+d})' for r in rows)
    lines = [
        f'\n**직접 센 숫자 — {data.get("month_label", "")} 아파트 실거래**\n',
        f'서울 {len(data["districts"])}개 구 신고 매매 **{data["total"]}건** '
        f'({data.get("before_label", "")} {data["total_before"]}건, '
        f'{data["total"] - data["total_before"]:+d}건)',
        f'\n거래가 많은 곳: {top}\n',
    ]
    jeonse = (data.get("jeonse") or [])[:1]
    if jeonse:
        j = jeonse[0]
        lines.append(f'\n{j["name"]} 전세가율 가운뎃값 **{j["median"]}%** '
                     f'(같은 단지·같은 면적 {j["count"]}곳)\n')
    swings = (data.get("swings") or [])[:2]
    if swings:
        moved = " · ".join(
            f'{s["name"]} {s["pct"]:+.1f}%({s["before"]}→{s["now"]}건'
            + (f', {s["hotspot"]["dong"]}에 {s["hotspot"]["share"]}% 몰림' if s.get("hotspot") else "")
            + ")"
            for s in swings)
        lines.append(f'\n서울 25개 구 가운데 거래가 가장 크게 움직인 곳: {moved}\n')
    hot = [h for h in (data.get("highlights") or []) if h["kind"] == "신고가"][:2]
    if hot:
        lines.append("\n이번 달 신고가\n")
        lines += [f'- {h["district"]} {h["name"]} {h["area"]}㎡ — {_eok(h["amount"])} '
                  f'(이전 최고 {_eok(h["before"])}, {h["pct"]:+.1f}%)' for h in hot]
        lines.append("")
    if image:
        lines.append(f'\n![지역별 거래 건수]({image})\n')
    lines.append("*국토교통부 실거래가 신고 자료를 직접 집계했습니다. 해제 신고분은 뺐습니다.*\n")
    return "\n".join(lines)


def policy_block_markdown(docs: list | None) -> str:
    """보관용 blog.md 에도 같은 원문 링크를 남긴다."""
    if not docs:
        return ""
    rows = []
    for d in docs:
        files = " · ".join(f"[{f['name'][:40]}]({f['url']})" for f in (d.files or [])[:2])
        rows.append(f"- [{d.title}]({d.url}) ({d.dept} {d.date})" + (f"\n  - 첨부 {files}" if files else ""))
    return "\n**오늘 나온 정부 발표 원문**\n\n" + "\n".join(rows) + "\n"


def tail_block_html(closing_question: str = "", related: list[dict] | None = None) -> str:
    """마무리 질문과 지난 글 링크. 댓글과 다음 글 클릭을 노린다."""
    parts = []
    if closing_question:
        parts.append(
            '<p style="background-color:#f7f8fa;padding:14px 16px;margin:28px 0 0">'
            f'<b>{_esc(closing_question)}</b><br>'
            '<span style="font-size:14px;color:#666666">댓글로 알려 주시면 다음 글에 반영하겠습니다.</span></p>'
        )
    if related:
        rows = "".join(
            f'<li style="margin-bottom:6px"><a href="{_esc(r["url"])}">{_esc(r["title"])}</a>'
            f' <span style="color:#888888;font-size:13px">({r["date"]})</span></li>'
            for r in related
        )
        parts.append(
            '<div style="margin:24px 0 0;padding:14px 16px;background-color:#f7f8fa">'
            '<b>함께 보면 좋은 지난 글</b>'
            f'<ul style="margin:8px 0 0;padding-left:18px">{rows}</ul></div>'
        )
    return "".join(parts)


def lead_block_markdown(summary_lines: list[str] | None, outline: list[str] | None) -> str:
    parts = []
    if summary_lines:
        parts.append("**3줄 요약**\n\n" + "\n".join(f"- {' '.join(l.split())}" for l in summary_lines[:3]) + "\n")
    if outline and len(outline) >= 3:
        parts.append("**이 글의 순서**\n\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(outline, start=1)) + "\n")
    return "\n".join(parts)


def tail_block_markdown(closing_question: str = "", related: list[dict] | None = None) -> str:
    parts = []
    if closing_question:
        parts.append(f"**{' '.join(closing_question.split())}**\n")
    if related:
        parts.append("**함께 보면 좋은 지난 글**\n\n"
                     + "\n".join(f"- [{r['title']}]({r['url']}) ({r['date']})" for r in related) + "\n")
    return "\n".join(parts)


def photo_search_links(query: str) -> list[tuple[str, str]]:
    """사진 자리용 스톡 검색 링크. 키 없이 링크만 만든다. 한글이면 픽사베이(한국어)가 먼저."""
    from urllib.parse import quote

    q = " ".join((query or "").split())
    if not q:
        return []
    is_korean = any("가" <= ch <= "힣" for ch in q)
    links = [
        ("픽사베이", f"https://pixabay.com/ko/images/search/{quote(q)}/"),
        ("언스플래시", f"https://unsplash.com/s/photos/{quote(q.replace(' ', '-'))}"),
        ("펙셀스", f"https://www.pexels.com/ko-kr/search/{quote(q)}/"),
    ]
    return links if is_korean else links[1:] + links[:1]


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


def expand_tags(tags: list[str], regions: list[str] | None = None,
                fixed: list[str] | None = None, limit: int = 30) -> list[str]:
    """모델이 준 태그에 고정 태그와 지역 태그를 더해 칸을 채운다.

    네이버 태그는 30개까지 등록된다. 남는 칸을 비워 둘 이유가 없다.
    지역은 '송파구' 와 '송파구아파트' 두 벌로 넣는다 — 사람들이 둘 다 검색한다.
    """
    out: list[str] = []

    def push(value: str) -> None:
        cleaned = (value or "").strip().lstrip("#").replace(" ", "")
        if cleaned and cleaned not in out and len(out) < limit:
            out.append(cleaned)

    for tag in tags or []:
        push(tag)
    for region in regions or []:
        push(region)
        push(f"{region}아파트")
    for tag in fixed or []:
        push(tag)
    return out


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
    SRT 자막 파일이 이 규칙으로 만들어진다.
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


def _greedy_lines(words: list[str], width: int) -> list[str]:
    """폭 width 로 띄어쓰기에서 접는다. 한 낱말이 폭보다 길면 글자 수로 자른다."""
    lines: list[str] = []
    current = ""
    for word in words:
        while len(word) > width:                      # 붙여 쓴 긴 낱말
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def wrap_caption(text: str, max_chars: int = 16, max_lines: int = 2) -> str:
    """세로 화면에서 읽히도록 자막 한 컷을 짧은 줄로 끊는다.

    한 줄 max_chars 자가 기준이지만, 그 폭으로 max_lines 줄에 못 담으면 담길 때까지 폭을
    한 글자씩 넓힌다 — 글자를 버리지 않으면서 줄 길이를 고르게 하려는 것이다. 두 줄이 될
    때는 가운데에 가장 가까운 띄어쓰기에서 잘라 한쪽만 길어 보이지 않게 한다.
    """
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    if len(flat) <= max_chars:
        return flat

    words = flat.split(" ")
    width = max_chars
    lines = _greedy_lines(words, width)
    while len(lines) > max_lines and width < len(flat):
        width += 1
        lines = _greedy_lines(words, width)

    if len(lines) == 2 and len(words) > 1:            # 두 줄은 길이를 맞춘다
        best, best_gap = None, None
        for i in range(1, len(words)):
            left = len(" ".join(words[:i]))
            right = len(flat) - left - 1
            if max(left, right) > width:
                continue
            gap = abs(left - right)
            if best_gap is None or gap < best_gap:
                best, best_gap = i, gap
        if best is not None:
            lines = [" ".join(words[:best]), " ".join(words[best:])]
    return "\n".join(lines)


def to_srt(lines: list[CaptionLine], total_seconds: int | None = None,
           max_chars: int = 16, max_lines: int = 2) -> str:
    """자막 줄 목록을 SRT 파일 내용으로 변환한다. 편집 프로그램에서 그대로 임포트된다."""
    timings = caption_timings(lines, total_seconds)
    if not timings:
        return ""
    blocks = [
        f"{index + 1}\n{_srt_stamp(start)} --> {_srt_stamp(end)}\n"
        f"{wrap_caption(line.text, max_chars, max_lines)}\n"
        for index, (line, (start, end)) in enumerate(zip(lines, timings))
    ]
    return "\n".join(blocks)


def _timecode(seconds: float, fps: int = 30) -> str:
    """HH:MM:SS:FF — 프리미어·다빈치가 마커 시각으로 읽는 형식."""
    total = max(seconds, 0.0)
    h, rem = divmod(int(total), 3600)
    m, sec = divmod(rem, 60)
    frames = min(int(round((total - int(total)) * fps)), fps - 1)
    return f"{h:02d}:{m:02d}:{sec:02d}:{frames:02d}"


def _csv(rows: list[list[str]]) -> str:
    """엑셀이 한글을 깨뜨리지 않도록 BOM 을 붙인 CSV 문자열."""
    import csv as csv_mod
    import io

    buffer = io.StringIO()
    writer = csv_mod.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()


_GRAPHIC_WORDS = ("자막", "카드", "그래픽", "차트", "표", "수치")


def shorts_cut_csv(shorts, image_files: list[str] | None = None, fps: int = 30) -> str:
    """쇼츠 편집용 컷 리스트. 컷마다 시각·자막·화면 지시·쓸 그림을 한 줄에 담는다."""
    timings = caption_timings(shorts.lines, shorts.estimated_seconds)
    if not timings:
        return ""
    pictures = list(image_files or [])
    used = 0
    rows = [["컷", "시작(TC)", "끝(TC)", "시작(초)", "길이(초)", "자막", "화면 지시", "쓸 그림"]]
    for i, (line, (start, end)) in enumerate(zip(shorts.lines, timings), start=1):
        picture = ""
        if pictures and any(w in (line.visual or "") for w in _GRAPHIC_WORDS):
            picture = pictures[used % len(pictures)]
            used += 1
        rows.append([
            str(i), _timecode(start, fps), _timecode(end, fps),
            f"{start:.2f}", f"{end - start:.2f}",
            " ".join((line.text or "").split()),
            " ".join((line.visual or "").split()),
            picture,
        ])
    return _csv(rows)


def longform_chapter_csv(longform, fps: int = 30) -> str:
    """롱폼 챕터 마커. 챕터 시각·제목·자료화면·띄울 수치를 한 줄씩."""
    rows = [["번호", "시작(TC)", "시작(초)", "챕터", "자료화면(B롤)", "띄울 수치·문구"]]
    for i, section in enumerate(longform.sections, start=1):
        start = parse_timecode(section.at)
        start = 0.0 if start is None else start
        rows.append([
            str(i), _timecode(start, fps), f"{start:.2f}",
            " ".join((section.chapter or "").split()),
            " / ".join(" ".join(b.split()) for b in (section.broll or [])),
            " / ".join(" ".join(g.split()) for g in (section.graphics or [])),
        ])
    return _csv(rows) if len(rows) > 1 else ""


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
    lines += _monthly_section(out_dir)
    lines += _titles_section(cfg)
    lines += _cost_section(cfg)

    path = out_dir / "INDEX.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _monthly_section(out_dir: Path) -> list[str]:
    monthly = out_dir / "monthly"
    if not monthly.exists():
        return []
    months = sorted((p for p in monthly.iterdir() if p.is_dir()), reverse=True)
    if not months:
        return []
    lines = ["", "## 월간 결산", "", "| 달 | 결산 | 네이버 HTML | 원자료 |", "| --- | --- | --- | --- |"]
    for mo in months:
        def cell(filename: str, label: str, mo: Path = mo) -> str:
            return f"[{label}](monthly/{mo.name}/{filename})" if (mo / filename).exists() else "—"
        lines.append(f"| **{mo.name}** | {cell('monthly.md', '결산')} | "
                     f"{cell('monthly-naver.html', 'HTML')} | {cell('data.json', 'JSON')} |")
    return lines


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


def _titles_section(cfg: Config) -> list[str]:
    from .store import TitleLog

    log_ = TitleLog(cfg.state_dir / "titles.json")
    picked = log_.picked()
    if not picked:
        return []
    lines = ["", "## 제목 기록", "", "| 유형 | 건수 | 평균 조회수 |", "| --- | --- | --- |"]
    for kind, v in log_.by_type().items():
        avg = f"{v['avg_views']:,}" if v["avg_views"] is not None else "—"
        lines.append(f"| {kind} | {v['count']} | {avg} |")
    lines += ["", "<details><summary>날짜별</summary>", "", "| 날짜 | 어디 | 고른 제목 | 유형 | 조회수 |", "| --- | --- | --- | --- | --- |"]
    for r in picked[:60]:
        views = f"{r['views']:,}" if r.get("views") is not None else "—"
        lines.append(f"| {r['date']} | {r['kind']} | {r.get('title', '')} | {r.get('type', '')} | {views} |")
    lines += ["", "</details>"]
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
