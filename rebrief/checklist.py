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


def build(cfg: Config, *, brief=None, post=None, pack=None, checks=None,
          link_status=None, warnings=None, llm_used: bool = True,
          empty_photo_slots: int = 0) -> list[Item]:
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

