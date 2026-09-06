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


def _norm(text: str) -> str:
    """콤마·공백을 걷어내 '1,234 억' 과 '1234억' 이 같은 것으로 잡히게 한다."""
    return re.sub(r"[,\s]", "", text or "")


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


def _present(forms: list[str], unit: str, text: str) -> bool:
    """값이 본문에 '숫자로서' 있는지 본다.

    '5' 가 '15%' 나 '2025' 안에 들어 있다고 확인으로 치면 안 되므로 앞뒤에 숫자가
    없어야 한다. 두 자리 이하 짧은 값은 어디에나 있으므로 단위 첫 글자까지 붙어
    있어야 한다 ('22개구', '5%', '20년'). 단위가 없으면 경계 검사만 한다.
    """
    unit_head = _norm(unit)[:1]
    for f in forms:
        short = len(re.sub(r"\D", "", f)) <= 2
        pat = r"(?<![\d.])" + re.escape(f) + (re.escape(unit_head) if short and unit_head else r"(?!\d)")
        if re.search(pat, text):
            return True
    return False


def _article_texts(clusters: list[Cluster]) -> tuple[dict[str, str], str]:
    """url → 정규화된 제목+본문. 전체 합본도 같이 돌려준다."""
    by_url: dict[str, str] = {}
    for c in clusters:
        for a in c.articles:
            by_url[a.url] = _norm(f"{a.title}\n{a.body or ''}")
    return by_url, "\n".join(by_url.values())


def check_numbers(brief: DailyBrief, clusters: list[Cluster]) -> list[NumberCheck]:
    by_url, everything = _article_texts(clusters)
    results: list[NumberCheck] = []
    for issue in brief.issues:
        own = [by_url[u] for u in issue.source_urls if u in by_url]
        own_text = "\n".join(own)
        has_body = any(len(t) > 300 for t in own)       # 제목만 있으면 300자를 넘기 어렵다
        for dp in issue.numbers:
            forms = _value_forms(dp.value)
            if not forms:
                continue
            if _present(forms, dp.unit, own_text):
                status, hint = VERIFIED, ""
            elif _present(forms, dp.unit, everything):
                status, hint = VERIFIED, "다른 이슈의 기사에서 확인"
            elif not has_body:
                status, hint = NO_TEXT, "본문을 수집하지 못한 기사"
            else:
                status, hint = NOT_FOUND, "기사 제목·본문에 이 값이 없습니다"
            results.append(NumberCheck(issue.title, dp.label, dp.value, dp.unit, status, hint))
    return results


def summarize(checks: list[NumberCheck]) -> dict[str, int]:
    out = {VERIFIED: 0, NOT_FOUND: 0, NO_TEXT: 0}
    for c in checks:
        out[c.status] = out.get(c.status, 0) + 1
    return out
