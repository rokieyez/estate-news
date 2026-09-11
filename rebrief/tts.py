"""대본 페이지에서 **읽는 말만** 뽑는다 — TTS(음성 합성) 서비스에 그대로 붙여 넣을 글.

대본 페이지에는 제목 후보·화면 지시·자료화면·체크리스트가 섞여 있어, 통째로 복사해 TTS 에
넣으면 "B-roll 자료화면" 까지 읽어 버립니다 (2026-09-11 사용자 요청 "자막만 선택복사").

사이트를 만들 때 `script-*.md` 에서 뽑습니다. 모델 답(VideoPack)이 아니라 마크다운에서 뽑는
이유는 지난 날짜까지 버튼이 생기게 하려는 것입니다 — 마크다운은 모든 날에 남아 있습니다.
틀(`script_shorts.md.j2`·`script_longform.md.j2`)을 고치면 여기도 맞춰야 합니다. 시험
`test_tts_text_follows_the_script_templates` 가 실제 틀로 그린 대본에서 뽑아 봅니다.
"""

from __future__ import annotations

import re

# 쇼츠 표의 한 줄: | 1 | `00:00` | 자막 | 화면 |
_SHORTS_ROW = re.compile(r"^\|\s*\d+\s*\|\s*`[^`]*`\s*\|\s*(.*?)\s*\|\s*.*\|\s*$")
# 롱폼에서 읽는 구간의 머리: 콜드오픈 · 「1. 챕터」 · 아웃트로
_SPOKEN_HEAD = re.compile(r"^##\s+(콜드오픈|\d+\.\s|아웃트로)")
# 읽는 구간이 끝나는 자리: 구분선 · 자료화면·그래픽 메모 · 다른 제목
_SPOKEN_END = re.compile(r"^(---+|\*\*B-roll|\*\*그래픽|#)")
# 자막 조각이 문장 끝인지 — 한국어 종결 어미로 끝나면 마침표를 붙여 TTS 가 쉬게 한다
_FINAL = re.compile(r"(다|요|죠)$")


def _plain(text: str) -> str:
    """마크다운 강조·코드 표시를 걷어낸다 (TTS 가 별표를 읽지 않게)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def shorts_text(md: str) -> str:
    """쇼츠 표의 「자막 (읽는 말)」 칸만 이어 붙인다. 한 문장이 한 줄.

    자막은 화면에 맞춰 짧게 끊겨 있어(「강남·서초·송파 아파트값이」/「한꺼번에 내렸습니다」)
    조각마다 줄을 바꾸면 TTS 가 조각마다 끊어 읽습니다. 문장이 끝나는 조각에서만 줄을 바꿉니다.
    """
    sentences: list[str] = []
    current: list[str] = []
    for line in md.splitlines():
        m = _SHORTS_ROW.match(line.strip())
        if not m:
            continue
        piece = _plain(m.group(1).replace("\\|", "|"))
        if not piece:
            continue
        if _FINAL.search(piece):
            piece += "."
        current.append(piece)
        if piece[-1] in ".?!…":
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    return "\n".join(sentences)


def longform_text(md: str) -> str:
    """롱폼의 콜드오픈·챕터 본문·아웃트로만. 구간 사이는 빈 줄 하나.

    챕터 제목·타임코드·자료화면·그래픽 메모는 읽는 말이 아니라 뺍니다.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in md.splitlines():
        stripped = line.strip()
        if _SPOKEN_HEAD.match(stripped):
            current = []
            blocks.append(current)
            continue
        if current is None:
            continue
        if _SPOKEN_END.match(stripped):
            current = None
            continue
        if stripped:
            current.append(_plain(stripped))
    return "\n\n".join("\n".join(b) for b in blocks if b)


def narration(filename: str, md: str) -> str:
    """대본 파일 이름에 맞춰 읽는 말을 돌려준다. 대본이 아니면 빈 문자열."""
    if filename == "script-shorts.md":
        return shorts_text(md)
    if filename == "script-longform.md":
        return longform_text(md)
    return ""
