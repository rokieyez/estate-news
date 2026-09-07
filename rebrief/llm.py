"""Claude API 호출 — 사실 정리 → 블로그 → 영상 대본.

구조화 출력(structured outputs)을 써서 응답을 Pydantic 모델로 바로 받는다.
LLM 이 마크다운이나 잡담을 섞어 보내는 일이 없으므로 후처리 파서가 필요 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anthropic

from .config import Config
from .models import BlogPost, Cluster, DailyBrief, Rewrite, VideoPack, WeeklyReview
from .prompts import (
    build_blog_user,
    build_brief_messages,
    build_shared_context,
    build_video_user,
    build_weekly_messages,
)

log = logging.getLogger(__name__)


class _Retryable(Exception):
    """한도·과부하·연결 오류처럼 다른 모델로 다시 시도해 볼 만한 실패."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LLMError(RuntimeError):
    """호출 자체가 실패했거나 모델이 응답을 거부한 경우."""


# 1M 토큰당 달러. shared/claude-api 기준 (2026-06).
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
}


@dataclass
class Usage:
    """한 번의 실행에서 쓴 토큰과 대략적인 비용."""

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    notes: list[str] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)   # 호출별 내역 — 어디서 돈이 나가는지 보려고
    _usd: float = 0.0

    def add(self, response, model: str | None = None, kind: str = "") -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        model = model or self.model
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        if model not in self.models_used:
            self.models_used.append(model)
        # 강등되면 호출마다 모델이 다를 수 있으므로 그 호출의 모델 단가로 더한다
        rate_in, rate_out = PRICING.get(model, (5.00, 25.00))
        million = 1_000_000
        usd = (inp / million * rate_in + cw / million * rate_in * 1.25
               + cr / million * rate_in * 0.10 + out / million * rate_out)
        self._usd += usd
        self.details.append({
            "kind": kind or "기타", "model": model,
            "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cr, "cache_write_tokens": cw,
            "usd": round(usd, 4),
        })

    @property
    def estimated_usd(self) -> float:
        if self.calls and self._usd:
            return self._usd
        # add() 를 거치지 않고 필드만 채운 경우(테스트 등)를 위한 계산
        rate_in, rate_out = PRICING.get(self.model, (5.00, 25.00))
        million = 1_000_000
        return (
            self.input_tokens / million * rate_in
            + self.cache_write_tokens / million * rate_in * 1.25
            + self.cache_read_tokens / million * rate_in * 0.10
            + self.output_tokens / million * rate_out
        )

    def by_kind(self) -> list[str]:
        """'브리핑 $0.21 (opus)' 처럼 호출별 한 줄씩. 없으면 빈 목록."""
        return [f"{d['kind']} ${d['usd']:.3f} ({d['model']})" for d in self.details]

    def summary(self) -> str:
        shown = "+".join(self.models_used) if len(self.models_used) > 1 else self.model
        return (
            f"{shown} · {self.calls}회 호출 · "
            f"입력 {self.input_tokens:,} (캐시읽기 {self.cache_read_tokens:,}) / "
            f"출력 {self.output_tokens:,} 토큰 · 약 ${self.estimated_usd:.3f}"
        )


class ContentGenerator:
    """설정에 맞춰 Claude 를 세 번 호출한다."""

    def __init__(self, cfg: Config, model: str | None = None):
        self.cfg = cfg
        self.model = model or str(cfg.get("llm.model", "claude-opus-5"))
        # 영상 대본은 출력 토큰이 가장 많다. 값싼 모델로 돌리면 하루 비용이 눈에 띄게 준다.
        # 비워 두면 기본 모델을 그대로 쓴다. 강등(fallback)은 두 경우 모두 그대로 동작한다.
        self.script_model = str(cfg.get("llm.script_model", "") or "").strip() or self.model
        self.max_tokens = int(cfg.get("llm.max_tokens", 16000))
        self.effort = str(cfg.get("llm.effort", "high"))
        self.fallback_model = str(cfg.get("llm.fallback_model", "") or "").strip()
        timeout = float(cfg.get("llm.timeout_seconds", 600))
        self.client = anthropic.Anthropic(api_key=cfg.api_key, timeout=timeout)
        self.usage = Usage(model=self.model)
        self._shared_context: str | None = None

    # ── 1단계: 사실 정리 ─────────────────────────────────────

    def generate_brief(self, clusters: list[Cluster], run_date: str) -> DailyBrief:
        system, user = build_brief_messages(self.cfg, clusters, run_date)
        log.info("브리핑 생성 중… (이슈 %d개)", len(clusters))
        brief = self._parse(
            system=system,
            user=user,
            output_format=DailyBrief,
            cache_system=True,
            kind="브리핑",
        )
        brief.date = brief.date or run_date
        return brief

    # ── 2·3단계: 같은 system 블록을 공유해 캐시를 태운다 ────

    def generate_blog(self, brief: DailyBrief) -> BlogPost:
        log.info("블로그 글 생성 중…")
        return self._parse(
            system=self._shared(brief),
            user=build_blog_user(self.cfg),
            output_format=BlogPost,
            cache_system=True,
            kind="블로그",
        )

    def generate_video(self, brief: DailyBrief) -> VideoPack:
        log.info("영상 대본 생성 중…")
        return self._parse(
            system=self._shared(brief),
            user=build_video_user(self.cfg),
            output_format=VideoPack,
            cache_system=True,
            kind="영상 대본",
            model=self.script_model,
        )

    # ── 주간 결산 (별도 system, 캐시 없음) ───────────────────

    def generate_weekly(self, days: list[dict], week_label: str) -> WeeklyReview:
        system, user = build_weekly_messages(self.cfg, days, week_label)
        log.info("주간 결산 생성 중… (%d일치)", len(days))
        return self._parse(system=system, user=user, output_format=WeeklyReview, cache_system=False,
                           kind="주간 결산")

    # ── 문장 고쳐 쓰기 (점검표 ❌ 자동 수정) ─────────────────

    def rewrite(self, sentence: str, phrases: list[str], tone: str = "") -> str:
        system = ("당신은 부동산 콘텐츠 편집자입니다. 주어진 문장에서 금지 표현을 빼고 같은 뜻으로 "
                  "다시 씁니다. 사실·숫자는 바꾸지 않습니다. 단정적 예측이나 투자 권유로 읽히지 않게 합니다."
                  + (f" 톤: {tone}" if tone else ""))
        user = f"금지 표현: {', '.join(phrases)}\n\n문장:\n{sentence}"
        return self._parse(system=system, user=user, output_format=Rewrite, cache_system=False,
                           kind="문장 고쳐쓰기").text.strip()

    def _shared(self, brief: DailyBrief) -> str:
        if self._shared_context is None:
            self._shared_context = build_shared_context(self.cfg, brief)
        return self._shared_context

    # ── 공통 호출 ────────────────────────────────────────────

    def _parse(self, *, system: str, user: str, output_format, cache_system: bool,
               kind: str = "", model: str | None = None):
        """지정 모델로 부르고, 한도·장애면 대체 모델로 한 번 더 시도한다."""
        base = model or self.model
        try:
            response, model = self._call(system, user, output_format, cache_system, base)
        except _Retryable as exc:
            if not self.fallback_model or self.fallback_model == base:
                raise LLMError(exc.message) from exc
            log.warning("%s — %s 로 다시 시도합니다.", exc.message, self.fallback_model)
            try:
                response, model = self._call(system, user, output_format, cache_system, self.fallback_model)
            except _Retryable as exc2:
                raise LLMError(f"{exc.message} (대체 모델 {self.fallback_model} 도 실패: {exc2.message})") from exc2
            self.usage.notes.append(
                f"{output_format.__name__} 은 기본 모델이 실패해 {self.fallback_model} 로 생성했습니다."
            )

        self.usage.add(response, model, kind=kind)

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise LLMError(f"모델이 응답을 거부했습니다 (사유: {getattr(detail, 'category', '미상')}).")
        if response.stop_reason == "max_tokens":
            self.usage.notes.append(
                f"{output_format.__name__} 응답이 max_tokens({self.max_tokens})에 걸려 잘렸을 수 있습니다."
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError(f"{output_format.__name__} 형식으로 응답을 해석하지 못했습니다.")
        return parsed

    def _call(self, system: str, user: str, output_format, cache_system: bool, model: str):
        system_blocks = [{"type": "text", "text": system}]
        # 캐시는 모델마다 따로 잡힌다. 대본을 다른 모델로 돌리는 날 캐시를 또 쓰면 돈만 더 든다.
        if cache_system and model == self.model:
            # 접두사 캐싱: blog/video 호출이 같은 system 을 공유하므로
            # 두 번째 호출부터 입력 토큰이 1/10 가격으로 처리된다.
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}
        kwargs = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
        }
        kwargs.update(self._reasoning_kwargs(model))
        try:
            return self.client.messages.parse(**kwargs), model
        except anthropic.AuthenticationError as exc:
            raise LLMError("ANTHROPIC_API_KEY 가 유효하지 않습니다.") from exc
        except anthropic.RateLimitError as exc:
            raise _Retryable("API 사용량 한도에 걸렸습니다. 잠시 후 다시 실행하세요.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"요청이 거부됐습니다: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise _Retryable(f"API 서버에 연결하지 못했습니다: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 529:
                raise _Retryable(f"API 오류 {exc.status_code}: {exc}") from exc
            raise LLMError(f"API 오류 {exc.status_code}: {exc}") from exc

    def _reasoning_kwargs(self, model: str | None = None) -> dict:
        """모델별로 지원하는 추론 옵션이 달라 여기서 갈라 준다."""
        model = model or self.model
        if model.startswith("claude-haiku"):
            # Haiku 4.5 는 adaptive thinking 과 effort 를 지원하지 않는다.
            return {}
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
