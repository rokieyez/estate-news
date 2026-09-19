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
    head, tail = spoken_extras(pieces, starts_late, _section(md, "훅"), _section(md, "마무리"))

    sentences: list[str] = []
    if head:
        sentences.append(_ends_sentence(head)[0])
    current: list[str] = []
    for piece in pieces:
        piece, done = _ends_sentence(piece)
        current.append(piece)
        if done:
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    if tail:
        sentences.append(_ends_sentence(tail)[0])   # 마무리는 제 줄에 — 「아직」 뒤에 붙지 않게
    return "\n".join(sentences)


def spoken_extras(pieces: list[str], starts_late: bool, hook: str, cta: str) -> tuple[str, str]:
    """자막 표 밖에 있는데 음성으로는 읽히는 말 — (앞에 붙일 훅, 끝에 붙일 마무리). 없으면 빈 글.

    음성(`shorts_text`)과 자막 파일·컷 CSV(`render.spoken_caption_lines`)가 이 한 곳의 규칙을 같이 쓴다.
    한쪽에만 들어가면 음성에만 있는 말 때문에 motion-studio 의 쉼 맞추기가 뒤로 밀린다
    (2026-09-18 부동산 쇼츠 — 마무리가 음성에만 있어 마지막 화면이 안 생기고 뒤 씬이 늘어졌다).
    자막이 00:00 이 아니라 00:03 부터면 앞 3초가 훅 자리다 — 훅을 먼저 읽는다.
    00:00 부터면 첫 자막이 훅을 이미 말하거나(말만 조금 다름) 훅은 화면 제목일 뿐이다 (9/6~9/11 실측).
    마무리는 자막 끝에 이미 없을 때만 붙인다.
    """
    hook, cta = _plain(hook or ""), _plain(cta or "")
    head = hook if hook and starts_late else ""
    tail = cta if cta and not _covered(cta, pieces[::-1]) else ""
    return head, tail


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
# 틀리기 쉬운 것은 **기호**입니다 — ㎡ 를 건너뛰거나 「m2」로, %p 를 「퍼센트 피」로, 1~14일 의 ~ 를
# 소리 없이, → 를 읽지 않거나 「화살표」로. 그것을 말로 풀어 씁니다.
# **숫자도 한글로 풉니다 (2026-09-19 사용자 요청 — 일레븐랩스가 숫자를 엉망으로 읽었다).** 처음엔 TTS 가 문맥에
# 맞춰 읽으리라 보고 그대로 뒀는데(9/13), 한국어 숫자는 일레븐랩스가 고쳐 읽어 주지 않습니다(그 옵션은 일본어뿐).
# 규칙은 아래 「숫자를 한글로」 절 — 기본은 한자어 수, 「곳·채·명·살·시…」 앞의 작은 수만 고유어 수.
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
    (re.compile(r"㎏|(?<=\d)\s?kg(?![A-Za-z])"), "킬로그램"),
    (re.compile(r"㎝|(?<=\d)\s?cm(?![A-Za-z])"), "센티미터"),
    (re.compile(r"㎜|(?<=\d)\s?mm(?![A-Za-z])"), "밀리미터"),
    (re.compile(r"(?<=\d)\s?m(?![A-Za-z0-9²])"), "미터"),
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


# ── 숫자를 한글로 (2026-09-19) ───────────────────────────────────────────────────
# 기본은 한자어 수(구십칠억 삼천만원 · 영 쩜 구칠퍼센트 · 팔월 십팔일 · 팔십사주). 고유어로 읽는 단위 앞의
# 작은 수만 고유어 수(세 곳 · 두 채 · 다섯 명 · 열두 시). 이름에 붙은 수(상계주공12 · 강남3구)도 한자어로
# 읽히고, 그렇게 읽으면 틀리는 이름(코로나19 → 코로나 일구)은 설정 voice.say_as 에 적습니다 — say_as 가 먼저 돕니다.
# 규칙이 못 알아본 것(3:1 · 1/3 · G20 · 1-2호선)은 그대로 두고 `unread_numbers` 가 알림에 올립니다.
# motion-studio 의 scripts/tts-lib.mjs 에 같은 규칙이 있습니다 — 한쪽을 고치면 다른 쪽도.

_SINO = "영일이삼사오육칠팔구"
_NATIVE_ONES = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")
_NATIVE_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
# 고유어 수로 읽는 단위 → 몇까지 고유어로 읽나 (그보다 크면 한자어 — 「구십삼 명」)
_NATIVE_UNITS = {
    "번째": 99, "시간": 20, "차례": 20, "가지": 20, "군데": 20, "마리": 20, "사람": 20, "필지": 20, "그루": 20,
    "바퀴": 20, "마디": 20, "방울": 20,
    "곳": 20, "채": 20, "명": 20, "개": 20, "건": 20, "표": 20, "석": 20, "달": 20, "줄": 20, "쌍": 20,
    "잔": 20, "끼": 20, "칸": 20, "살": 99, "시": 12, "배": 10, "번": 10,
}
# 고유어 단위로 시작하지만 다른 낱말인 것 — 한자어 수로 읽는다 (5개월 · 3달러 · 1번지)
_NOT_NATIVE = ("개월", "개년", "개국", "개소", "개사", "달러", "번지", "번호", "번길", "배럴", "배수", "배당",
               "표준", "표본", "석유", "채권", "채널", "시장", "시즌", "시리즈", "시군", "시도", "시민", "시점",
               "건물", "건설", "명의로", "명절")
_UNIT_KEYS = sorted(list(_NATIVE_UNITS) + list(_NOT_NATIVE), key=len, reverse=True)
_NUMTOK = re.compile(r"(?<![\dA-Za-z.,:/\-])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\dA-Za-z:/]|,\d|-\d|\.\d)")
_UNREAD = re.compile(r"[A-Za-z]*\d[\dA-Za-z.,:/\-]*")


def _sino_chunk(n: int) -> str:
    """1~9999 → 「삼천이백오십」. 천·백·십 앞의 1 은 읽지 않는다."""
    out = ""
    for power, name in ((1000, "천"), (100, "백"), (10, "십"), (1, "")):
        digit = n // power % 10
        if digit:
            out += ("" if digit == 1 and name else _SINO[digit]) + name
    return out


def sino(n: int) -> str:
    """한자어 수 — 12345 → 「만 이천삼백사십오」, 100000000 → 「일억」."""
    if n == 0:
        return "영"
    groups = []
    for power, name in ((12, "조"), (8, "억"), (4, "만"), (0, "")):
        chunk = n // 10 ** power % 10000
        if chunk:
            groups.append(("" if chunk == 1 and name == "만" else _sino_chunk(chunk)) + name)
    return " ".join(groups)


def native(n: int) -> str:
    """고유어 수 (꾸밈꼴) — 3 → 「세」, 12 → 「열두」, 20 → 「스무」. 1~99 만."""
    if n == 20:
        return "스무"
    return _NATIVE_TENS[n // 10] + _NATIVE_ONES[n % 10]


def _digits(frac: str) -> str:
    """소수점 아래 한 자리씩. 「육」은 받침 없는 소리나 ㄹ 뒤에서 「륙」 (오륙 · 칠륙) — 2026-09-19 로키즈가 듣고 바로잡음."""
    out = ""
    for d in frac:
        word = _SINO[int(d)]
        out += "륙" if word == "육" and out and out[-1] in "이사오구일칠팔륙" else word
    return out


def _read_number(m: re.Match) -> str:
    whole, frac = m[1].replace(",", ""), m[2]
    if len(whole) > 16:
        return m[0]                                   # 경 단위 너머는 손대지 않는다 (unread_numbers 가 알린다)
    rest = m.string[m.end():]
    tail = rest.lstrip(" ")
    if frac:                                          # 소수점은 「쩜」, 아래는 한 자리씩 — 0.97 → 영 쩜 구칠, 14.56 → 십사 쩜 오륙
        return f"{sino(int(whole))} 쩜 {_digits(frac)}"
    if len(whole) > 1 and whole[0] == "0":            # 007 → 공공칠
        return "".join("공" if d == "0" else _SINO[int(d)] for d in whole)
    n = int(whole)
    if tail.startswith("세대") and len(whole) == 4 and whole[1] == "0" and whole[3] == "0" and whole[2] != "0":
        return "".join("공" if d == "0" else _SINO[int(d)] for d in whole)      # 2030세대 → 이공삼공세대
    if tail.startswith("월") and n in (6, 10):
        return "유" if n == 6 else "시"                # 유월 · 시월
    if n == 1 and tail[:1] in ("만", "천", "백"):
        return ""                                     # 1만 → 만, 1천만원 → 천만원 (1억은 「일억」)
    unit = next((u for u in _UNIT_KEYS if tail.startswith(u)), "")
    if unit in _NATIVE_UNITS and 1 <= n <= _NATIVE_UNITS[unit]:
        before = m.string[:m.start()].rstrip()
        if not (unit == "번" and before.endswith(("기호", "제"))):              # 기호 2번은 「이번」
            word = "첫" if unit == "번째" and n == 1 else native(n)
            return word + ("" if rest.startswith(" ") else " ")
    return sino(n)


# 수 바로 뒤의 조사는 우리가 읽은 소리의 받침에 맞춘다 — 「동성3는」을 「동성삼는」으로 두면 말이 걸린다.
# 「이·가」는 맞추지 않는다 (「을지로3가」의 「가」는 조사가 아니다).
_PARTICLE = re.compile(r"(?<=[가-힣])(은|는|을|를|과|와|으로|로)(?![가-힣])")
_PAIRS = {"은": "는", "는": "은", "을": "를", "를": "을", "과": "와", "와": "과", "으로": "로", "로": "으로"}


_PAUSE = re.compile(r"(\d\s?(?:년|개월|달|주|일|분기|시간|분)\s?(?:새|사이|만에))\s+(?=\d)")


def _with_particle(m: re.Match) -> str:
    read = _read_number(m)
    rest = m.string[m.end():]
    if read != m[0] and re.match(r" ?건(?!물|설)", rest):
        return read + "\x01"                          # 수 뒤의 「건」은 [껀] — 아래 _spell_line 이 바꾼다
    hit = _PARTICLE.match("가" + rest, 1) if read and read != m[0] and read[-1] != " " else None
    if not hit:
        return read
    jong = (ord(read[-1]) - 0xAC00) % 28              # 0 = 받침 없음, 8 = ㄹ
    particle = hit[1]
    closed = jong != 0 and not (particle in ("으로", "로") and jong == 8)      # 「일로·칠로·팔로」
    want = particle if (particle in ("은", "을", "과", "으로")) == closed else _PAIRS[particle]
    return read + "\x00" + want + "\x00" + str(len(particle))


def _spell_line(line: str) -> str:
    # 「건」(件)은 된소리로 읽는다 — 구백사십껀 · 계약껀수 (2026-09-19 로키즈가 듣고 바로잡음). 「사건·조건」은 건드리지 않는다
    # 「1년 새 14.56%」처럼 기간 뒤에 수가 바로 오면 두 수가 붙어 들린다 — 「새·사이·만에」 뒤에 쉼표로 쉬게 한다 (같은 날)
    line = _PAUSE.sub(r"\1, ", line)
    out = _NUMTOK.sub(_with_particle, line.replace("건수", "껀수"))
    out = re.sub(r"\x01( ?)건", r"\1껀", out).replace("\x01", "")
    # 자리표(\x00조사\x00길이) 뒤에 남은 원래 조사를 걷어 낸다
    return re.sub(r"\x00([가-힣]+)\x00(\d)([가-힣]{1,2})", lambda m: m[1] + m[3][int(m[2]):], out)


def spell_numbers(text: str) -> str:
    """글 속의 숫자를 한글로 — 줄 수는 그대로. 규칙이 못 알아본 것은 그대로 남는다."""
    return "\n".join(_spell_line(line) for line in (text or "").split("\n"))


def unread_numbers(text: str) -> list[str]:
    """풀어 쓴 뒤에도 남은 숫자 — 알림에 올려 사람이 voice.say_as 에 읽는 법을 적게 한다."""
    found: list[str] = []
    for token in _UNREAD.findall(text or ""):
        token = token.rstrip(".,:/-")
        if token and token not in found:
            found.append(token)
    return found


def say_as(text: str, table: dict | None) -> str:
    """글자대로 읽히면 틀리는 낱말을 소리 나는 대로 바꾼다 — 설정 `voice.say_as` (2026-09-18 사용자 요청).

    「신고가」(新高價)는 [신고까]로 읽는데 TTS 는 「신고 가」로 읽는다. 채널마다 다르게 둔다 — 정치에서는
    「신고가 접수됐다」(申告-가)처럼 글자대로 읽어야 하는 날이 있어서다. 음성에 보낼 글만 바꾸고 자막은 그대로다.
    숫자가 든 이름의 읽는 법(「코로나19」→「코로나 일구」)도 여기 적는다 — 그래서 숫자 풀기보다 먼저 돈다.
    """
    for written, spoken in (table or {}).items():
        text = text.replace(str(written), str(spoken))
    return text


def speakable(text: str, table: dict | None = None, numbers: bool = True) -> str:
    """읽는 말에서 TTS 가 틀리게 읽기 쉬운 기호·단위를 말로 풀고 숫자를 한글로 쓴다. 줄 수는 그대로.
    table 을 주면 소리 나는 대로 바꿀 낱말(`say_as`)을 먼저 바꾼다. numbers=False 면 숫자는 그대로 둔다."""
    spoken = "\n".join(_speak_line(line) for line in say_as(text or "", table).split("\n"))
    return spell_numbers(spoken) if numbers else spoken
