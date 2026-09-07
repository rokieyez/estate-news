"""발행 전 점검표 — 흩어진 경고를 한 장에 모으고, 프롬프트로만 금지했던 것을 실제로 검사한다.

LLM 을 부르지 않는다. 산출물 텍스트와 설정만 본다.
  ✅ 통과   ⚠️ 확인 필요 (발행은 가능)   ❌ 막힘 (발행 전에 고쳐야 함)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


@dataclass
class Item:
    key: str
    level: str
    title: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return ICON[self.level]


def _find_phrases(text: str, phrases: list[str]) -> list[str]:
    hits = []
    flat = " ".join((text or "").split())
    for p in phrases:
        p = p.strip()
        if p and p in flat:
            hits.append(p)
    return hits


def long_sentences(markdown_text: str, limit: int = 90) -> list[str]:
    """너무 긴 문장들. 표·소제목·인용은 빼고 본문 문장만 센다."""
    out: list[str] = []
    for block in _body_blocks(markdown_text):
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            flat = " ".join(sentence.split())
            if len(flat) > limit:
                out.append(flat)
    return out


def long_paragraphs(markdown_text: str, limit: int = 320) -> list[str]:
    """한 문단이 너무 길면 휴대폰에서 벽처럼 보인다."""
    return [b for b in _body_blocks(markdown_text) if len(b) > limit]


def _body_blocks(markdown_text: str) -> list[str]:
    blocks: list[str] = []
    for raw in (markdown_text or "").split("\n\n"):
        block = " ".join(raw.split())
        if not block or block.startswith(("#", ">", "|", "-", "*", "!", "[이미지")):
            continue
        blocks.append(block)
    return blocks


def long_captions(pack, max_chars: int = 16, max_lines: int = 2) -> list[str]:
    """두 줄에 담기지 않는 쇼츠 자막 컷. 화면에서 글자가 작아지거나 넘친다."""
    from .render import wrap_caption

    out: list[str] = []
    for i, line in enumerate(pack.shorts.lines, start=1):
        wrapped = wrap_caption(line.text, max_chars, max_lines)
        if any(len(part) > max_chars for part in wrapped.split("\n")):
            out.append(f"{i}컷 · {' '.join(line.text.split())}")
    return out


def keyword_placement(post, head_chars: int = 120, title_head: int = 15) -> tuple[str, list[str]]:
    """대표 검색어가 제목·첫 문단·소제목에 들어갔는지. (검색어, 빠진 자리 목록)"""
    from .render import outline_from_markdown

    keyword = " ".join((getattr(post, "focus_keyword", "") or "").split())
    if not keyword:
        return "", []
    title = " ".join((post.title or "").split())
    body = " ".join((post.body_markdown or "").split())
    missing: list[str] = []
    if keyword not in title:
        missing.append("제목")
    elif title.find(keyword) > title_head:
        missing.append("제목 앞쪽(지금은 뒤쪽)")
    if keyword not in body[:head_chars]:
        missing.append(f"첫 {head_chars}자")
    if not any(keyword in h for h in outline_from_markdown(post.body_markdown)):
        missing.append("소제목")
    return keyword, missing


def overlap_with_previous(body_markdown: str, prev_bodies, threshold: float = 0.8) -> tuple[float, list[str]]:
    """어제·그제 글과 사실상 같은 문장의 비율. 매일 같은 문장을 쓰면 검색에서 중복으로 취급된다."""
    from .cluster import similarity

    mine = _sentences(body_markdown)
    if not mine or not prev_bodies:
        return 0.0, []
    old = [s for _, text in prev_bodies for s in _sentences(text)]
    if not old:
        return 0.0, []
    hits = [m for m in mine if any(similarity(m, o) >= threshold for o in old)]
    return round(len(hits) / len(mine), 3), hits[:3]


def _sentences(markdown_text: str, min_len: int = 15) -> list[str]:
    out: list[str] = []
    for block in _body_blocks(markdown_text):
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            flat = " ".join(sentence.split())
            if len(flat) >= min_len:
                out.append(flat)
    return out


def build(cfg: Config, *, brief=None, post=None, pack=None, checks=None,
          link_status=None, warnings=None, llm_used: bool = True,
          empty_photo_slots: int = 0, repeats=None, prev_bodies=None) -> list[Item]:
    items: list[Item] = []
    video = cfg.get("video", {}) or {}
    blog = cfg.get("blog", {}) or {}
    naver = blog.get("naver", {}) or {}
    banned = [b for b in (video.get("banned_phrases", []) or []) if b]

    if not llm_used:
        items.append(Item("llm", FAIL, "요약·대본이 만들어지지 않았습니다",
                          "prompt-pack.md 를 챗봇에 붙여넣어 직접 만들거나, 키·한도를 확인한 뒤 다시 실행하세요."))
        return items

    # 1) 숫자 검산
    missing = [c for c in (checks or []) if c.status == "미확인"]
    if missing:
        items.append(Item("numbers", WARN, f"수치 {len(missing)}건이 기사 원문에서 확인되지 않음",
                          "아래 '원문' 링크를 눌러 기사에서 직접 찾아보세요. 기사에 없는 값이면 글에서 빼는 게 안전합니다. "
                          "[오늘의 정리 열기](brief.html)",
                          [f"{c.issue} · {c.label} **{c.display}**" + (f" — [원문]({c.url})" if c.url else "")
                           for c in missing]))
    elif checks:
        items.append(Item("numbers", OK, f"수치 {len(checks)}건 모두 기사 원문에서 확인"))

    # 2) 출처 링크
    dead = [u for u, st in (link_status or {}).items() if not st.ok]
    if dead:
        items.append(Item("links", WARN, f"출처 링크 {len(dead)}개가 열리지 않음",
                          "'기사 원문' 페이지의 ⚠️ 표시를 보고 링크를 바꾸거나 빼세요. [기사 원문 열기](sources.html)", dead[:5]))
    elif link_status:
        items.append(Item("links", OK, f"출처 링크 {len(link_status)}개 모두 정상"))

    # 3) 금지 표현 — 프롬프트로 금지했지만 실제로 안 썼는지는 여기서 본다
    if banned:
        texts = []
        if post is not None:
            texts.append(("블로그", f"{post.title}\n{post.body_markdown}"))
        if pack is not None:
            texts.append(("쇼츠", "\n".join(l.text for l in pack.shorts.lines) + "\n" + pack.shorts.hook))
            texts.append(("롱폼", pack.longform.cold_open + "\n" + "\n".join(s.script for s in pack.longform.sections)
                          + "\n" + pack.longform.outro))
        hits = [f"{where}: '{p}'" for where, t in texts for p in _find_phrases(t, banned)]
        if hits:
            items.append(Item("banned", FAIL, f"금지 표현 {len(hits)}건 발견", "해당 문장을 고친 뒤 발행하세요.", hits))
        elif texts:
            items.append(Item("banned", OK, "금지 표현 없음"))

    # 4) 블로그 분량·태그·이미지 자리
    if post is not None:
        n = len(re.sub(r"\s", "", post.body_markdown))
        lo, hi = int(blog.get("min_chars", 1800)), int(blog.get("max_chars", 3500))
        if n < lo * 0.8 or n > hi * 1.2:
            items.append(Item("blog_len", WARN, f"블로그 본문 {n:,}자 (목표 {lo:,}~{hi:,})",
                              "너무 짧으면 검색 노출이 약하고, 너무 길면 휴대폰에서 이탈합니다."))
        else:
            items.append(Item("blog_len", OK, f"블로그 본문 {n:,}자"))
        tags = len(post.tags or [])
        want = int(naver.get("tag_count", 20))
        if tags == 0 or tags > 30:
            items.append(Item("tags", WARN, f"태그 {tags}개", "네이버 태그는 최대 30개입니다."))
        elif abs(tags - want) > want // 2:
            items.append(Item("tags", WARN, f"태그 {tags}개 (설정 {want}개)"))
        else:
            items.append(Item("tags", OK, f"태그 {tags}개"))
        # 4-0) 유입의 시작 — 대표 검색어가 제자리에 있는지
        keyword, missing = keyword_placement(post)
        if not keyword:
            items.append(Item("keyword", WARN, "대표 검색어가 비어 있습니다",
                              "이 글로 누가 검색해 들어올지 정하지 않은 상태입니다. 제목과 첫 문단에 넣을 말을 하나 정하세요."))
        elif missing:
            items.append(Item("keyword", WARN, f"대표 검색어 '{keyword}' 가 {' · '.join(missing)} 에 없습니다",
                              "그 자리에 같은 표기로 넣어야 검색에 걸립니다. 변형이 아니라 글자 그대로."))
        else:
            items.append(Item("keyword", OK, f"대표 검색어 '{keyword}' 가 제목·첫 문단·소제목에 모두 있음"))

        # 4-0-2) 지난 글과 겹치는 문장 — 유사문서로 몰리지 않게
        ratio, samples = overlap_with_previous(post.body_markdown, prev_bodies or [])
        limit = float(blog.get("max_overlap_ratio", 0.15))
        if prev_bodies and ratio >= limit:
            items.append(Item("overlap", WARN, f"지난 글과 거의 같은 문장이 {ratio * 100:.0f}%",
                              "매일 같은 문장을 쓰면 검색에서 중복 문서로 취급될 수 있습니다. 표현을 바꾸세요.", samples))
        elif prev_bodies:
            items.append(Item("overlap", OK, f"지난 글과 겹치는 문장 {ratio * 100:.0f}%"))

        # 4-1) 읽기 쉬움 — 긴 문장·긴 문단
        long_s = long_sentences(post.body_markdown, int(blog.get("max_sentence_chars", 90)))
        long_p = long_paragraphs(post.body_markdown, int(blog.get("max_paragraph_chars", 320)))
        if long_s or long_p:
            bits = []
            if long_s:
                bits.append(f"긴 문장 {len(long_s)}개")
            if long_p:
                bits.append(f"긴 문단 {len(long_p)}개")
            items.append(Item("readability", WARN, " · ".join(bits) + " — 휴대폰에서 답답해 보입니다",
                              "문장은 둘로 나누고, 문단은 두세 문장에서 끊으세요.",
                              [t[:60] + "…" for t in (long_s + long_p)[:3]]))
        else:
            items.append(Item("readability", OK, "문장·문단 길이 적당"))
        if empty_photo_slots:
            items.append(Item("photos", WARN, f"직접 넣을 사진 자리 {empty_photo_slots}곳",
                              "'네이버 블로그 글' 페이지의 점선 상자 아래 '사진 찾기' 링크를 쓰세요. [네이버 블로그 글 열기](blog-naver.html)"))

    # 5) 영상 발화량
    if pack is not None:
        cpm = int(video.get("speaking_rate_cpm", 330))
        s_target = int(video.get("shorts_seconds", 60)) / 60 * cpm
        s_chars = sum(len(l.text) for l in pack.shorts.lines)
        if s_chars > s_target * 1.15:
            items.append(Item("shorts_len", WARN, f"쇼츠 발화 {s_chars}자 — {int(video.get('shorts_seconds', 60))}초에 {int(s_target)}자가 적정",
                              "빠르게 읽어야 합니다. 자막 두세 컷을 줄이세요."))
        else:
            items.append(Item("shorts_len", OK, f"쇼츠 발화 {s_chars}자 (약 {s_chars / cpm * 60:.0f}초)"))
        l_target = float(video.get("longform_minutes", 8)) * cpm
        l_chars = len(pack.longform.cold_open) + sum(len(s.script) for s in pack.longform.sections)
        if l_chars > l_target * 1.25 or l_chars < l_target * 0.6:
            items.append(Item("long_len", WARN, f"롱폼 발화 {l_chars:,}자 (약 {l_chars / cpm:.1f}분, 목표 {video.get('longform_minutes', 8)}분)"))
        else:
            items.append(Item("long_len", OK, f"롱폼 발화 {l_chars:,}자 (약 {l_chars / cpm:.1f}분)"))

    # 5-1) 자막 길이 — 두 줄에 안 들어가는 컷
    if pack is not None:
        over = long_captions(pack, int(video.get("caption_max_chars", 16)),
                             int(video.get("caption_max_lines", 2)))
        if over:
            items.append(Item("caption_len", WARN, f"두 줄에 안 들어가는 자막 {len(over)}컷",
                              "쇼츠는 세로 화면입니다. 컷을 쪼개거나 문장을 줄이세요.", over[:3]))

    # 5-2) 같은 주제 반복
    for r in (repeats or []):
        items.append(Item("repeat", WARN,
                          f"'{r['title']}' 은 {r['days_ago']}일 전에도 다뤘습니다",
                          "그대로 또 쓰면 재탕으로 보입니다. 그때와 달라진 숫자를 앞세우거나 후속 국면을 잡으세요.",
                          [f"{r['prev_date']} · {r['prev_title']}"]))

    # 6) 실행 중 나온 경고 (max_tokens 잘림, 강등 등)
    for w in (warnings or []):
        if "확인되지 않았습니다" in w or "열리지 않습니다" in w:
            continue                          # 위에서 이미 항목으로 다뤘다
        items.append(Item("warn", WARN, w))

    return items


def summarize(items: list[Item]) -> dict[str, int]:
    out = {OK: 0, WARN: 0, FAIL: 0}
    for i in items:
        out[i.level] += 1
    return out


# ── ❌ 금지 표현 자동 수정 ─────────────────────────────────

_SENT = re.compile(r"[^.!?\n]*[.!?]?")


def sentences_with(text: str, phrases: list[str]) -> list[str]:
    """금지 표현이 든 문장들 (원문 그대로, 중복 없이)."""
    out: list[str] = []
    for m in _SENT.finditer(text or ""):
        s = m.group(0)
        if s.strip() and any(p in s for p in phrases) and s not in out:
            out.append(s)
    return out


def autofix(text: str, phrases: list[str], rewrite) -> tuple[str, list[tuple[str, str]]]:
    """금지 표현이 든 문장을 rewrite(문장, 표현들) 로 바꾼다. (새 본문, [(전, 후), ...])"""
    changes: list[tuple[str, str]] = []
    for s in sentences_with(text, phrases):
        hits = [p for p in phrases if p in s]
        try:
            new = rewrite(s.strip(), hits)
        except Exception as exc:                  # 고치기 실패는 점검표 ❌ 로 남기면 된다
            import logging
            logging.getLogger(__name__).warning("문장 고쳐 쓰기 실패: %s", exc)
            continue
        if new and not any(p in new for p in phrases):
            text = text.replace(s.strip(), new, 1)
            changes.append((s.strip(), new))
    return text, changes

