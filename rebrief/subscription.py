"""구독(Pro/Max)으로 모델 부르기 — API 요금 없이 (2026-09-11 사용자 결정).

`ContentGenerator` 의 모든 호출(브리핑·블로그·대본·정책 요약·결산·문장 고쳐쓰기)이 이 길로 갈 수 있다.
러너에서는 저장소 Secret `CLAUDE_CODE_OAUTH_TOKEN`(맥에서 `claude setup-token`, 1년짜리)으로 Claude Code
(`claude -p`)를 부른다. 공식 문서가 "CI pipelines and scripts" 용이라 적었고, 이 토큰으로 돌리면 API 요금이
아니라 **구독 사용량**으로 처리된다(code.claude.com/docs/en/authentication).

API 와 다른 점 — 채팅은 구조화 출력을 강제하지 못하므로 스키마를 글로 붙이고 답에서 JSON 을 꺼내
pydantic 으로 검사한다(붙여넣기 모드와 같은 방식, 2026-09-11 러너에서 세 단계 모두 첫 시도에 통과).

지킬 것 — ① `--bare` 금지(그 모드는 이 토큰을 읽지 않음) ② `ANTHROPIC_API_KEY` 를 넘기지 않음(인증 순서상
API 키가 이겨서 **요금이 나감**) ③ 빈 임시 폴더에서 실행(저장소에서 돌면 긴 CLAUDE.md 를 읽음) ④ `--tools ""`.

**출력 한도에 닿으면 답이 두 턴으로 나뉜다 (2026-09-14, 옆 프로젝트 community-shorts 에서 원래 답을 받아 확인).**
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` 는 생각(thinking)과 답이 함께 쓴다. 한도에 닿으면 Claude Code 가 이어 쓰고, JSON 결과의
`result` 에는 **마지막 턴만** 담긴다 — JSON 앞부분이 빠져 못 읽거나, 안쪽 조각(목록의 항목 하나)을 답으로 집어 엉뚱한
형식 오류가 난다. 이어 쓰는 턴이 안전 검사(`stop_reason: refusal`, `reasoning_extraction`)에 잘못 걸려 오류로 오기도 한다.
그쪽은 한도 4,000 에서 네 번 중 세 번 실패했고, 고친 뒤 세 번 모두 성공했다.
그래서 이제 — 답이 나뉘었거나(`num_turns` > 1) 잘렸거나 안전 검사에 걸리면 **한도를 두 배로**(64,000 까지) 올려 다시 부르고,
형식이 틀리면 무엇이 틀렸는지 적어 다시 묻는다(세 번까지). 스키마의 필수 필드가 다 있는 객체만 답으로 꺼낸다.
이 저장소들의 실측(러너 로그 9/12~9/14): 영상 대본 출력 15,736~27,406토큰 · 한도 32,000 — 정치 9/12 가 한도의 86%.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
SCHEMA_RULE = ("\n\n---\n출력 규칙: 설명이나 인사 없이 **JSON 하나만** ```json 상자에 담아 답하세요. "
               "아래 JSON 스키마의 필드 이름과 형식을 그대로 따릅니다. 문자열 안의 줄바꿈은 \\n 으로 적습니다.\n\n"
               "```json\n{schema}\n```")
ATTEMPTS = 3
MAX_BUDGET = 64000        # 나뉜 답을 다시 부를 때 올리는 한도의 상한

# 한도·과부하·연결처럼 기다리거나 다른 모델로 해 볼 만한 실패
_RETRYABLE = re.compile(r"usage limit|rate limit|overloaded|529|5\d\d|timed? ?out|ECONNRESET|network",
                        re.IGNORECASE)
_AUTH = re.compile(r"authenticat|oauth|401|invalid.*token|expired", re.IGNORECASE)


def claude_bin(cfg) -> str:
    return str(cfg.get("llm.claude_bin", "claude") or "claude")


def ready(cfg) -> str:
    """구독으로 부를 수 있으면 빈 문자열, 아니면 못 하는 이유."""
    if not os.environ.get(TOKEN_ENV):
        return f"구독 토큰({TOKEN_ENV})이 없습니다"
    if not shutil.which(claude_bin(cfg)):
        return "claude 명령(Claude Code)이 설치돼 있지 않습니다"
    return ""


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Reply:
    """API 응답과 같은 꼴 — `Usage.add` 와 `_parse` 가 그대로 읽는다."""
    parsed_output: object = None
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str = "end_turn"
    subscription: bool = True          # 비용 장부에 0원으로 적게 한다


class SubscriptionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, refused: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.refused = refused         # 안전 검사에 걸림 — 한도를 올려 다시 부를 만하다


def extract_json(text: str, required: set[str] | None = None) -> dict | None:
    """답에서 JSON 객체 하나를 꺼낸다 — ```json 상자 → 첫 { 부터. 앞뒤 설명 문장은 건너뛴다.

    required 를 주면 그 필드가 다 있는 객체만 꺼낸다. 답 앞부분이 빠지면 바깥 객체는 못 읽고 안쪽 조각만
    읽히는데, 그 조각을 답으로 집으면 원인과 먼 형식 오류가 난다.
    """
    dec = json.JSONDecoder()

    def ok(obj) -> bool:
        return isinstance(obj, dict) and (not required or required <= set(obj))

    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text or "", re.S):
        body = m.group(1).strip()
        try:
            obj, _ = dec.raw_decode(body)
            if ok(obj):
                return obj
        except json.JSONDecodeError:
            pass
    s = text or ""
    for i, ch in enumerate(s):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(s[i:])
                if ok(obj):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def looks_cut(text: str) -> bool:
    """```json 상자를 열고 닫지 않았거나 중괄호가 덜 닫힌 채 끝났으면, 답이 중간에 잘린 것이다."""
    t = text or ""
    if t.count("```") % 2 == 1:
        return True
    i = t.find("{")
    return i >= 0 and t.count("{", i) > t.count("}", i)


def validation_summary(exc) -> str:
    """pydantic 오류를 한 줄로 — 어느 필드가 빠지거나 틀렸는지."""
    errs = exc.errors()
    bits = []
    for e in errs[:4]:
        loc = ".".join(str(x) for x in e.get("loc", ())) or "(전체)"
        bits.append(f"{loc} {'없음' if e.get('type') == 'missing' else str(e.get('msg', ''))[:40]}")
    return " · ".join(bits) + (f" 외 {len(errs) - 4}개" if len(errs) > 4 else "")


def ask(cfg, *, system: str, user: str, output_format, model: str, max_tokens: int,
        effort: str = "", timeout: float = 900) -> Reply:
    """묻고 pydantic 으로 검사한 답을 돌려준다. 못 읽으면 세 번까지 다시 묻는다.

    · 답이 나뉘었거나(num_turns > 1) 잘렸거나 안전 검사에 걸리면 한도를 두 배로 올려 다시 부른다.
    · JSON 을 못 찾거나 형식이 틀리면 무엇이 틀렸는지 덧붙여 다시 묻는다 (한도는 그대로).
    · 끝까지 안전 검사에 걸리면 `retryable` 로 올려 대체 모델이 해 보게 한다 — Claude Code 도 모델을 바꿔 보라고 안내한다.
    """
    import pydantic

    spec = output_format.model_json_schema()
    prompt = user + SCHEMA_RULE.format(schema=json.dumps(spec, ensure_ascii=False))
    required = set(spec.get("required") or [])
    budget, note, last, refused = int(max_tokens), "", "", False
    for attempt in range(ATTEMPTS):
        if attempt:
            log.warning("구독 %s 답을 못 읽음(%d번째): %s", output_format.__name__, attempt, last)
        try:
            data = _run(cfg, system=system, prompt=prompt + note, model=model, max_tokens=budget,
                        effort=effort, timeout=timeout)
        except SubscriptionError as exc:
            if not exc.refused:
                raise
            last, refused = exc.message, True
            budget, note = min(budget * 2, MAX_BUDGET), ""
            continue
        refused = False
        text = str(data.get("result") or "")
        u = data.get("usage") or {}
        usage = _Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                       int(u.get("cache_read_input_tokens") or 0),
                       int(u.get("cache_creation_input_tokens") or 0))
        stop = str(data.get("stop_reason") or "end_turn")
        split = int(data.get("num_turns") or 1) > 1
        obj = extract_json(text, required=required)
        if obj is None and (split or looks_cut(text)):
            last = (f"답이 {'두 턴으로 나뉘어' if split else '중간에 잘려'} 앞뒤가 빠졌습니다 "
                    f"(출력 {usage.output_tokens:,}토큰 · 한도 {budget:,})")
            budget, note = min(budget * 2, MAX_BUDGET), ""
            continue
        if obj is None:
            obj = extract_json(text)
        if obj is None:
            last = "답에서 JSON 을 찾지 못했습니다"
            note = "\n\n(앞선 답에서 JSON 을 찾지 못했습니다. 설명 없이 JSON 하나만 답하세요.)"
            continue
        try:
            return Reply(parsed_output=output_format.model_validate(obj), usage=usage, stop_reason=stop)
        except pydantic.ValidationError as exc:
            what = validation_summary(exc)
            last = f"형식이 맞지 않습니다: {what}"
            note = (f"\n\n(앞선 답이 형식에 맞지 않았습니다: {what}. "
                    "스키마의 필드 이름과 형식을 그대로 따라 JSON 하나만 다시 답하세요.)")
    raise SubscriptionError(f"{output_format.__name__} {last}", retryable=refused)


def _run(cfg, *, system: str, prompt: str, model: str, max_tokens: int, effort: str,
         timeout: float) -> dict:
    cmd = [claude_bin(cfg), "-p", "--output-format", "json", "--model", model, "--tools", "",
           "--no-session-persistence", "--system-prompt", system]
    if effort and not model.startswith("claude-haiku"):
        cmd += ["--effort", effort]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(max_tokens)),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=tmp, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SubscriptionError(f"{int(timeout) // 60}분 안에 답이 오지 않았습니다",
                                    retryable=True) from exc
        except OSError as exc:
            raise SubscriptionError(f"claude 를 실행하지 못했습니다: {exc}") from exc
    try:
        data = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["(출력 없음)"]
        raise SubscriptionError(f"claude 응답을 읽지 못했습니다: {tail[0][:200]}", retryable=True)
    if data.get("is_error") or done.returncode != 0:
        why = str(data.get("result") or done.stderr or "알 수 없는 오류")[:300]
        # 안전 검사를 먼저 본다 — 그 안내문의 요청 번호에 401 같은 숫자가 섞이면 인증 실패로 잘못 읽는다
        if str(data.get("stop_reason") or "") == "refusal" or "safeguards" in why:
            raise SubscriptionError(f"안전 검사에 걸렸습니다 ({why[:160]})", refused=True)
        if _AUTH.search(why):
            raise SubscriptionError(f"구독 토큰으로 로그인하지 못했습니다 — 만료됐으면 맥에서 "
                                    f"`claude setup-token` 으로 다시 만들어 Secret 을 바꾸세요 ({why})")
        raise SubscriptionError(f"구독 호출 실패: {why}", retryable=bool(_RETRYABLE.search(why)))
    log.info("구독 호출: %s · 출력 %s토큰 · %s턴 · %.0f초", model,
             (data.get("usage") or {}).get("output_tokens", "?"), data.get("num_turns", "?"),
             (data.get("duration_ms") or 0) / 1000)
    return data
