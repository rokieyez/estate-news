import sys
from pathlib import Path

# 저장소 루트를 import 경로에 넣어 `rebrief` 를 패키지로 쓴다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 공용 픽스처 (test_pipeline / test_extras 가 함께 쓴다) ────

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import yaml

from rebrief.config import Config, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.text = content.decode("utf-8")
        self.status_code = status_code
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


# .env 에 실제 인증키가 있으면 load_config() 가 환경변수로 올린다. 그러면 테스트가
# 진짜 서버를 부르려 든다. 인증키는 매 테스트 시작 때 비워 둔다 —
# 필요한 테스트는 스스로 monkeypatch.setenv 로 넣는다.
#
# ANTHROPIC_API_KEY 까지 비우는 이유: 이 키가 있고 없고에 따라 cfg.llm_enabled 가 달라져
# **로컬은 통과하고 러너는 실패하는** 시험이 생긴다 (실제로 월간 결산 시험이 그랬다).
@pytest.fixture(autouse=True)
def _no_optional_keys(monkeypatch):
    for name in ("DATA_GO_KR_KEY", "REB_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def feed_bytes() -> bytes:
    """발행 시각을 '방금'으로 채운 RSS 본문."""
    recent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    return FIXTURE.read_text(encoding="utf-8").replace("__PUBDATE__", recent).encode("utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """실제 config/ 를 읽되 출력·상태 경로만 임시 폴더로 돌린다."""
    real = load_config()
    settings = json.loads(json.dumps(real.settings, default=str))
    settings["output"]["dir"] = str(tmp_path / "output")
    settings["collect"]["fetch_body"] = False        # 본문 수집은 네트워크가 필요
    settings["run"]["skip_recent_days"] = 0
    settings.setdefault("images", {})["png"] = False   # 테스트는 브라우저를 띄우지 않는다
    # 사진 검색은 망을 탄다. .env 에 PEXELS_API_KEY 를 넣은 컴퓨터에서 시험이
    # 조용히 인터넷을 쓰기 시작하는 것을 막는다.
    settings["images"]["photos"] = False
    settings["collect"]["check_links"] = False        # 링크 점검은 별도 테스트에서 스텁으로
    settings.setdefault("paste", {})["open_issue"] = False   # 시험이 gh 로 이슈를 열면 안 된다
    settings["paste"]["auto"] = False          # 구독 자동 답하기는 그 시험에서만 켠다
    settings.setdefault("llm", {})["mode"] = "api"           # 붙여넣기 모드는 그 시험에서만 켠다
    settings.setdefault("stats", {})["enabled"] = False  # 정부 통계는 개별 테스트에서만 켠다

    # 피드는 픽스처 하나만 쓴다.
    sources = yaml.safe_load(yaml.safe_dump(real.sources, allow_unicode=True))
    sources["feeds"] = [
        {"id": "fixture", "name": "테스트피드", "url": "https://example.test/rss", "weight": 1.0}
    ]

    config = Config(settings=settings, sources=sources, config_dir=real.config_dir)
    config.repo_root = tmp_path
    return config


@pytest.fixture(autouse=True)
def stub_network(monkeypatch, feed_bytes):
    def fake_get(url, **kwargs):
        return FakeResponse(feed_bytes)

    monkeypatch.setattr("rebrief.collect.requests.get", fake_get)
