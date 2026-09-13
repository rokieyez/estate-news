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
# 이 글자로 끝나는 조각은 말이 이어진다(조사·연결 어미·꾸밈꼴): 「아파트값이」「커지면서」「구독하고」「합의된」
# 「한」은 뺐다 — 「신내동 새한」 같은 단지 이름이 다음 줄에 붙는다. 「대한」「위한」은 아래 낱말 목록에.
_JOINS = set("이가은는을를에의와과도로서고며면게지데니해어아여께랑나든야된던할될")
# 말이 이어지는 부사·접속어 — 「구별 하락률은 아직」 / 「기사에 나오지 않았습니다」
_JOIN_WORDS = {"아직", "다시", "이미", "먼저", "또", "더", "가장", "모두", "바로", "특히", "결국",
               "그리고", "그러나", "하지만", "그런데", "그래서", "또한", "즉", "다만", "역시",
               "계속", "함께", "오히려", "무려", "약", "총", "왜", "어떻게", "오늘", "어제", "내일",
               "지금", "이번", "이렇게", "그렇게", "대한", "위한", "관한", "통한", "향한", "인한"}
_CLOSERS = "\"'”’」』)]"
_HANGUL = re.compile(r"[가-힣]")


def _plain(text: str) -> str:
    """마크다운 강조·코드 표시를 걷어낸다 (TTS 가 별표를 읽지 않게)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def _ends_sentence(piece: str) -> tuple[str, bool]:
    """(마침표를 채운 조각, 여기서 줄을 바꿀지).

    말이 이어지는 게 분명한 조각(조사·연결 어미·쉼표·「아직」 같은 부사)만 다음 조각과 한 줄로
    잇고, 종결 어미(다·요·죠)로 끝나면 마침표를 붙입니다. 그 밖 — 헤드라인처럼 명사로 끝나는
    조각(「인사청문회는 15일 예정」) — 은 **마침표를 지어 넣지 않고** 줄만 바꿉니다. 정치 쇼츠는
    「이 숫자는 국민의힘」/「발표 기준입니다」처럼 명사 조각이 문장 한가운데에도 와서, 마침표를
    붙이면 문장 중간에서 말이 끝나 버립니다. 줄바꿈은 TTS 가 잠깐 쉬는 정도라 덜 어색합니다.
    """
    if piece[-1] in ".?!…":
        return piece, True
    if piece[-1] == ",":
        return piece, False
    core = piece.rstrip(_CLOSERS)
    words = core.split()
    last_word = words[-1] if words else core
    if last_word.endswith("보다"):          # 「직전 거래보다」 — 견줌 조사지 문장 끝이 아니다
        return piece, False
    if _FINAL.search(core):
        return piece + ".", True
    if core.endswith("까"):
        return piece + "?", True
    if core and _HANGUL.match(core[-1]) and (core[-1] in _JOINS or last_word in _JOIN_WORDS):
        return piece, False
    return piece, True


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _covered(text: str, pieces: list[str]) -> bool:
    """마무리가 자막 조각에 이미 들어 있는지 — 끝에서부터 같은 길이만큼 모아 글자 쌍 겹침으로 본다."""
    want = len(re.sub(r"\s", "", text))
    got: list[str] = []
    for p in pieces:
        got.append(p)
        if len(re.sub(r"\s", "", "".join(got))) >= want:
            break
    a, b = _bigrams(text), _bigrams(" ".join(got))
    return bool(a and b) and 2 * len(a & b) / (len(a) + len(b)) >= 0.5


def _section(md: str, head: str) -> str:
    """「## 머리」 아래 첫 글 덩이 (다음 제목 전까지)."""
    lines, inside = [], False
    for line in md.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].startswith(head)
            continue
        if inside and line.strip():
            lines.append(line.strip().lstrip(">").strip().lstrip("#").strip())
    return _plain(" ".join(lines))


def shorts_text(md: str) -> str:
    """쇼츠의 읽는 말 — 훅 + 「자막 (읽는 말)」 칸 + 마무리. 한 문장이 한 줄.

    자막은 화면에 맞춰 짧게 끊겨 있어(「강남·서초·송파 아파트값이」/「한꺼번에 내렸습니다」)
    조각마다 줄을 바꾸면 TTS 가 조각마다 끊어 읽습니다. 문장이 끝나는 조각에서만 줄을 바꿉니다.
    훅과 마무리는 자막 표에 들어 있는 날도 있고 따로인 날도 있어, 표에 이미 있으면 한 번만 싣습니다.
    """
    pieces: list[str] = []
    for line in md.splitlines():
        m = _SHORTS_ROW.match(line.strip())
        if m:
            piece = _plain(m.group(1).replace("\\|", "|"))
            if piece:
                pieces.append(piece)
    first = next((_SHORTS_ROW.match(l.strip()) for l in md.splitlines() if _SHORTS_ROW.match(l.strip())), None)
    starts_late = bool(first) and not re.search(r"`0?0:00`", first.group(0))
    hook, cta = _section(md, "훅"), _section(md, "마무리")

    sentences: list[str] = []
    # 자막이 00:00 이 아니라 00:03 부터면 앞 3초가 훅 자리다 — 훅을 먼저 읽는다.
    # 00:00 부터면 첫 자막이 훅을 이미 말하거나(말만 조금 다름) 훅은 화면 제목일 뿐이다 (9/6~9/11 실측).
    if hook and starts_late:
        sentences.append(_ends_sentence(hook)[0])
    current: list[str] = []
    for piece in pieces:
        piece, done = _ends_sentence(piece)
        current.append(piece)
        if done:
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    if cta and not _covered(cta, pieces[::-1]):
        sentences.append(_ends_sentence(cta)[0])    # 마무리는 제 줄에 — 「아직」 뒤에 붙지 않게
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


# ── TTS 가 틀리게 읽기 쉬운 기호·단위를 풀어 쓴 글 (2026-09-13, 마무리 아이디어 3) ─────────
#
# 숫자 자체는 그대로 둡니다. 요즘 TTS 는 「3곳」을 「세 곳」, 「84.8」을 「팔십사 점 팔」로 문맥에
# 맞춰 읽는데, 우리가 한글로 바꿔 넣으면 「삼 곳」 같은 틀린 읽기를 우리가 만들게 됩니다.
# 틀리기 쉬운 것은 **기호**입니다 — ㎡ 를 건너뛰거나 「m2」로, %p 를 「퍼센트 피」로, 1~14일 의 ~ 를
# 소리 없이, → 를 읽지 않거나 「화살표」로. 그것만 말로 풀어 씁니다.
# **줄은 늘리거나 줄이지 않습니다** — motion-studio 가 줄과 쉼으로 음성에 자막을 맞춥니다.
# 자막(SRT)과 글자가 달라지므로 motion-studio 에 **대본으로 넣을 글은 「자막만 복사」** 쪽입니다.

_NUM = r"\d+(?:[.,]\d+)*"
# 수 뒤에 붙는 단위 — 긴 것부터. 조사까지 먹지 않게 아는 단위만 받는다(「1~14일까지」의 '까').
_UNIT_ALT = "|".join(re.escape(u) for u in (
    "%p", "%", "㎡", "억원", "만원", "조원", "퍼센트", "억", "만", "조", "원", "일", "월", "년", "주",
    "시", "분", "초", "명", "건", "곳", "개", "채", "배", "층", "위", "호", "대", "세", "살", "차", "회", "평"))
_RANGE = re.compile(r"(" + _NUM + r")\s*[~∼〜]\s*(" + _NUM + r")(" + _UNIT_ALT + r")?")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_ARROW = re.compile(r"\s*(?:→|⇒|->)\s*")
_AMOUNT = r"(" + _NUM + r"(?:" + _UNIT_ALT + r")?)"
_UP = re.compile(_AMOUNT + r"\s*[↑▲]")
_DOWN = re.compile(_AMOUNT + r"\s*[↓▼]")
_MARK_UP = re.compile(r"▲\s*" + _AMOUNT)
_MARK_DOWN = re.compile(r"▼\s*" + _AMOUNT)
_SIGN = re.compile(r"(^|[\s(\[「'\"])([+\-−])(?=\d)")
_DOT = re.compile(r"(?<=[가-힣A-Za-z0-9])\s*[·ㆍ]\s*(?=[가-힣A-Za-z0-9])")
_UNITS = (
    (re.compile(r"%\s*[pP](?![A-Za-z])"), "퍼센트포인트"),
    (re.compile(r"%"), "퍼센트"),
    (re.compile(r"㎡|(?<=\d)\s?m²|(?<=\d)\s?m2(?![0-9A-Za-z])"), "제곱미터"),
    (re.compile(r"㎢"), "제곱킬로미터"),
    (re.compile(r"㎞|(?<=\d)\s?km(?![A-Za-z])"), "킬로미터"),
    (re.compile(r"(?<![A-Za-z])vs\.?(?![A-Za-z])", re.I), "대"),
)


def _speak_line(line: str) -> str:
    if not line.strip():
        return line
    s = _ISO_DATE.sub(lambda m: f"{m[1]}년 {int(m[2])}월 {int(m[3])}일", line)
    s = _RANGE.sub(lambda m: f"{m[1]}{m[3] or ''}에서 {m[2]}{m[3] or ''}", s)
    s = _MARK_UP.sub(r"\1 증가", s)
    s = _MARK_DOWN.sub(r"\1 감소", s)
    s = _UP.sub(r"\1 상승", s)
    s = _DOWN.sub(r"\1 하락", s)
    s = _ARROW.sub("에서 ", s)
    s = _SIGN.sub(lambda m: m[1] + ("플러스 " if m[2] == "+" else "마이너스 "), s)
    for pattern, word in _UNITS:
        s = pattern.sub(word, s)
    s = _DOT.sub(", ", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def speakable(text: str) -> str:
    """읽는 말에서 TTS 가 틀리게 읽기 쉬운 기호·단위만 말로 풀어 쓴다. 줄 수는 그대로."""
    return "\n".join(_speak_line(line) for line in (text or "").split("\n"))
