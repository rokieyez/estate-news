"""구독(Pro/Max)으로 모델 부르기 — API 요금 없이 (2026-09-11 사용자 결정).

`ContentGenerator` 의 모든 호출(브리핑·블로그·대본·정책 요약·결산·문장 고쳐쓰기)이 이 길로 갈 수 있다.
러너에서는 저장소 Secret `CLAUDE_CODE_OAUTH_TOKEN`(맥에서 `claude setup-token`, 1년짜리)으로 Claude Code
(`claude -p`)를 부른다. 공식 문서가 "CI pipelines and scripts" 용이라 적었고, 이 토큰으로 돌리면 API 요금이
아니라 **구독 사용량**으로 처리된다(code.claude.com/docs/en/authentication).

API 와 다른 점 — 채팅은 구조화 출력을 강제하지 못하므로 스키마를 글로 붙이고 답에서 JSON 을 꺼내
pydantic 으로 검사한다(붙여넣기 모드와 같은 방식, 2026-09-11 러너에서 세 단계 모두 첫 시도에 통과).

지킬 것 — ① `--bare` 금지(그 모드는 이 토큰을 읽지 않음) ② `ANTHROPIC_API_KEY` 를 넘기지 않음(인증 순서상
API 키가 이겨서 **요금이 나감**) ③ 빈 임시 폴더에서 실행(저장소에서 돌면 긴 CLAUDE.md 를 읽음) ④ `--tools ""`.
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
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


def extract_json(text: str) -> dict | None:
    """답에서 JSON 객체 하나를 꺼낸다 — ```json 상자 → 첫 { 부터. 앞뒤 설명 문장은 건너뛴다."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text or "", re.S):
        body = m.group(1).strip()
        try:
            obj, _ = dec.raw_decode(body)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    s = text or ""
    for i, ch in enumerate(s):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(s[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def ask(cfg, *, system: str, user: str, output_format, model: str, max_tokens: int,
        effort: str = "", timeout: float = 900) -> Reply:
    """한 번 묻고 pydantic 으로 검사한 답을 돌려준다. JSON 을 못 꺼내면 한 번 더 묻는다."""
    import pydantic

    schema = json.dumps(output_format.model_json_schema(), ensure_ascii=False)
    prompt = user + SCHEMA_RULE.format(schema=schema)
    last = ""
    for attempt in range(2):
        data = _run(cfg, system=system, prompt=prompt, model=model, max_tokens=max_tokens,
                    effort=effort, timeout=timeout)
        text = str(data.get("result") or "")
        u = data.get("usage") or {}
        usage = _Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                       int(u.get("cache_read_input_tokens") or 0),
                       int(u.get("cache_creation_input_tokens") or 0))
        stop = str(data.get("stop_reason") or "end_turn")
        obj = extract_json(text)
        if obj is None:
            last = "답에서 JSON 을 찾지 못했습니다"
        else:
            try:
                return Reply(parsed_output=output_format.model_validate(obj), usage=usage, stop_reason=stop)
            except pydantic.ValidationError as exc:
                last = f"형식이 맞지 않습니다: {str(exc).splitlines()[0][:160]}"
        log.warning("구독 %s 답을 못 읽음(%d번째): %s", output_format.__name__, attempt + 1, last)
    raise SubscriptionError(f"{output_format.__name__} {last}")


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
        if _AUTH.search(why):
            raise SubscriptionError(f"구독 토큰으로 로그인하지 못했습니다 — 만료됐으면 맥에서 "
                                    f"`claude setup-token` 으로 다시 만들어 Secret 을 바꾸세요 ({why})")
        raise SubscriptionError(f"구독 호출 실패: {why}", retryable=bool(_RETRYABLE.search(why)))
    log.info("구독 호출: %s · 출력 %s토큰 · %.0f초", model, (data.get("usage") or {}).get("output_tokens", "?"),
             (data.get("duration_ms") or 0) / 1000)
    return data
