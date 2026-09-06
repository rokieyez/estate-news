"""브리핑의 수치가 근거 기사에 실제로 있는지 대조한다.

요약에서 가장 위험한 오류는 숫자 오독·환각이다. LLM 을 한 번 더 부르지 않고,
각 수치의 값 문자열이 그 이슈의 기사 제목·본문 어딘가에 글자 그대로 있는지 본다.
있으면 '확인', 없으면 '미확인'. 본문을 긁지 못한 이슈는 '대조 불가' 로 따로 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Cluster, DailyBrief

VERIFIED, NOT_FOUND, NO_TEXT = "확인", "미확인", "대조 불가"


@dataclass
class NumberCheck:
    issue: str
    label: str
    value: str
    unit: str
    status: str
    hint: str = ""
    url: str = ""          # 값을 찾은 기사. 못 찾았으면 그 이슈의 대표 기사(사람이 직접 볼 곳)
    snippet: str = ""      # 그 값이 든 문장 (원문 대조용, 원래 띄어쓰기 그대로)

    @property
    def display(self) -> str:
        """값+단위. 모델이 값 칸에 단위까지 적었으면('71명' + '명') 겹치지 않게."""
        v, u = self.value.strip(), self.unit.strip()
        return v if (not u or v.endswith(u)) else v + u


def _norm(text: str) -> str:
    """콤마·공백을 걷어내 '1,234 억' 과 '1234억' 이 같은 것으로 잡히게 한다."""
    return re.sub(r"[,\s]", "", text or "")


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """정규화 문자열과, 그 i번째 글자가 원문 어디에 있었는지의 표. 문장을 원래 띄어쓰기로 보여 주기 위해."""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text or ""):
        if ch == "," or ch.isspace():
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def _value_forms(value: str) -> list[str]:
    """'-0.03' → ['-0.03', '0.03'], '1~2' → ['1~2', '1∼2', '1-2', '1', '2'] 처럼 찾을 후보들."""
    v = _norm(value)
    forms = {v}
    if v.startswith(("-", "−", "▲", "▼")):
        forms.add(v[1:])
    for sep in ("~", "∼", "-"):
        if sep in v:
            parts = [p for p in v.split(sep) if p]
            forms.update(parts)
            for alt in ("~", "∼", "-", "∼"):
                forms.add(alt.join(parts))
    # '11.0' 처럼 소수점 뒤가 0 이면 '11' 도 같은 값
    for f in list(forms):
        if re.fullmatch(r"\d+\.0+", f):
            forms.add(f.split(".")[0])
    return [f for f in forms if f and re.search(r"\d", f)]


def _find(forms: list[str], unit: str, text: str) -> int:
    """값이 본문에 '숫자로서' 있는 위치. 없으면 -1.

    '5' 가 '15%' 나 '2025' 안에 들어 있다고 확인으로 치면 안 되므로 앞뒤에 숫자가
    없어야 한다. 두 자리 이하 짧은 값은 어디에나 있으므로 단위 첫 글자까지 붙어
    있어야 한다 ('22개구', '5%', '20년'). 단위가 없으면 경계 검사만 한다.
    """
    unit_head = _norm(unit)[:1]
    for f in forms:
        short = len(re.sub(r"\D", "", f)) <= 2
        pat = r"(?<![\d.])" + re.escape(f) + (re.escape(unit_head) if short and unit_head else r"(?!\d)")
        m = re.search(pat, text)
        if m:
            return m.start()
    return -1


def _snippet(text: str, pos: int, width: int = 60) -> str:
    """찾은 위치를 가운데 둔 한 문장 안팎 (원문 텍스트 기준)."""
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    end_candidates = [i for i in (text.find(".", pos), text.find("\n", pos)) if i != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    piece = text[start:end].strip()
    if len(piece) > width * 2:
        piece = "…" + text[max(pos - width, start):pos + width] + "…"
    return " ".join(piece.split())


def _article_texts(clusters: list[Cluster]) -> dict[str, tuple[str, str, list[int]]]:
    """url → (정규화 텍스트, 원문 텍스트, 위치 표)."""
    by_url: dict[str, tuple[str, str, list[int]]] = {}
    for c in clusters:
        for a in c.articles:
            original = f"{a.title}\n{a.body or ''}"
            norm, idx = _norm_with_map(original)
            by_url[a.url] = (norm, original, idx)
    return by_url


def _locate(forms: list[str], unit: str, texts: dict[str, tuple[str, str, list[int]]]) -> tuple[str, str] | None:
    """여러 기사 중 값이 처음 발견되는 (url, 원문 문장). 없으면 None."""
    for url, (norm, original, idx) in texts.items():
        pos = _find(forms, unit, norm)
        if pos >= 0:
            return url, _snippet(original, idx[pos] if pos < len(idx) else 0)
    return None


def check_numbers(brief: DailyBrief, clusters: list[Cluster]) -> list[NumberCheck]:
    by_url = _article_texts(clusters)
    results: list[NumberCheck] = []
    for issue in brief.issues:
        own = {u: by_url[u] for u in issue.source_urls if u in by_url}
        has_body = any(len(t[0]) > 300 for t in own.values())   # 제목만 있으면 300자를 넘기 어렵다
        lead_url = next(iter(own), issue.source_urls[0] if issue.source_urls else "")
        for dp in issue.numbers:
            forms = _value_forms(dp.value)
            if not forms:
                continue
            url, snippet = "", ""
            hit = _locate(forms, dp.unit, own)
            if hit:
                status, hint = VERIFIED, ""
                url, snippet = hit
            elif (hit := _locate(forms, dp.unit, {u: t for u, t in by_url.items() if u not in own})):
                status, hint = VERIFIED, "다른 이슈의 기사에서 확인"
                url, snippet = hit
            elif not has_body:
                status, hint, url = NO_TEXT, "본문을 수집하지 못한 기사", lead_url
            else:
                # 못 찾은 값도 사람이 바로 확인하러 갈 수 있게 대표 기사 링크는 붙여 둔다
                status, hint, url = NOT_FOUND, "기사 제목·본문에 이 값이 없습니다", lead_url
            results.append(NumberCheck(issue.title, dp.label, dp.value, dp.unit, status, hint, url, snippet))
    return results


def summarize(checks: list[NumberCheck]) -> dict[str, int]:
    out = {VERIFIED: 0, NOT_FOUND: 0, NO_TEXT: 0}
    for c in checks:
        out[c.status] = out.get(c.status, 0) + 1
    return out
