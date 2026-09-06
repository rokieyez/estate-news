"""Claude API 호출 — 사실 정리 → 블로그 → 영상 대본.

구조화 출력(structured outputs)을 써서 응답을 Pydantic 모델로 바로 받는다.
LLM 이 마크다운이나 잡담을 섞어 보내는 일이 없으므로 후처리 파서가 필요 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import anthropic

from .config import Config
from .models import BlogPost, Cluster, DailyBrief, VideoPack, WeeklyReview
from .prompts import (
    build_blog_user,
    build_brief_messages,
    build_shared_context,
    build_video_user,
    build_weekly_messages,
)

log = logging.getLogger(__name__)


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

    def add(self, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    @property
    def estimated_usd(self) -> float:
        rate_in, rate_out = PRICING.get(self.model, (5.00, 25.00))
        million = 1_000_000
        return (
            self.input_tokens / million * rate_in
            + self.cache_write_tokens / million * rate_in * 1.25
            + self.cache_read_tokens / million * rate_in * 0.10
            + self.output_tokens / million * rate_out
        )

    def summary(self) -> str:
        return (
            f"{self.model} · {self.calls}회 호출 · "
            f"입력 {self.input_tokens:,} (캐시읽기 {self.cache_read_tokens:,}) / "
            f"출력 {self.output_tokens:,} 토큰 · 약 ${self.estimated_usd:.3f}"
        )


class ContentGenerator:
    """설정에 맞춰 Claude 를 세 번 호출한다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = str(cfg.get("llm.model", "claude-opus-5"))
        self.max_tokens = int(cfg.get("llm.max_tokens", 16000))
        self.effort = str(cfg.get("llm.effort", "high"))
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
        )

    def generate_video(self, brief: DailyBrief) -> VideoPack:
        log.info("영상 대본 생성 중…")
        return self._parse(
            system=self._shared(brief),
            user=build_video_user(self.cfg),
            output_format=VideoPack,
            cache_system=True,
        )

    # ── 주간 결산 (별도 system, 캐시 없음) ───────────────────

    def generate_weekly(self, days: list[dict], week_label: str) -> WeeklyReview:
        system, user = build_weekly_messages(self.cfg, days, week_label)
        log.info("주간 결산 생성 중… (%d일치)", len(days))
        return self._parse(system=system, user=user, output_format=WeeklyReview, cache_system=False)

    def _shared(self, brief: DailyBrief) -> str:
        if self._shared_context is None:
            self._shared_context = build_shared_context(self.cfg, brief)
        return self._shared_context

    # ── 공통 호출 ────────────────────────────────────────────

    def _parse(self, *, system: str, user: str, output_format, cache_system: bool):
        system_blocks = [{"type": "text", "text": system}]
        if cache_system:
            # 접두사 캐싱: blog/video 호출이 같은 system 을 공유하므로
            # 두 번째 호출부터 입력 토큰이 1/10 가격으로 처리된다.
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
        }
        kwargs.update(self._reasoning_kwargs())

        try:
            response = self.client.messages.parse(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("ANTHROPIC_API_KEY 가 유효하지 않습니다.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("API 사용량 한도에 걸렸습니다. 잠시 후 다시 실행하세요.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"요청이 거부됐습니다: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"API 서버에 연결하지 못했습니다: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API 오류 {exc.status_code}: {exc}") from exc

        self.usage.add(response)

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

    def _reasoning_kwargs(self) -> dict:
        """모델별로 지원하는 추론 옵션이 달라 여기서 갈라 준다."""
        if self.model.startswith("claude-haiku"):
            # Haiku 4.5 는 adaptive thinking 과 effort 를 지원하지 않는다.
            return {}
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
        }
