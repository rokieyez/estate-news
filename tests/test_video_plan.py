"""영상 계획 (2026-09-17 사용자 결정) — 롱폼은 주간 결산 때만, 쇼츠는 날마다 + 일레븐랩스 음성.

두 저장소 공통 파일입니다. 일레븐랩스는 부르지 않습니다 — `voice._post`·`voice._get` 을 갈아끼웁니다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rebrief import pipeline, voice
from rebrief.site import build_site
from tests.test_pipeline import RUN_DATE, FakeGenerator, _seed_days

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk_test_never_printed"
MP3 = b"ID3" + b"\x00" * 4000


class Reply:
    def __init__(self, status: int = 200, content: bytes = MP3, body: dict | None = None):
        self.status_code, self.content, self._body = status, content, body
        self.text = json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def eleven(monkeypatch):
    """일레븐랩스 자리에 서는 가짜. 받은 요청을 calls 에 쌓는다."""
    calls: list[dict] = []

    def post(url, **kw):
        calls.append({"url": url, **kw})
        return Reply()

    monkeypatch.setenv(voice.KEY_ENV, KEY)
    monkeypatch.setattr(voice, "_post", post)
    monkeypatch.setattr(voice, "_get", lambda url, **kw: Reply(
        body={"character_limit": 56000, "character_count": 1000}))
    return calls


def _run(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    return pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)


# ── 롱폼은 주간 결산 때만 ─────────────────────────────────────


def test_daily_run_asks_for_shorts_only(cfg, monkeypatch):
    from rebrief.models import ShortsPack
    from rebrief.prompts import build_video_user

    prompt = build_video_user(cfg)
    assert "■ 쇼츠" in prompt and "■ 롱폼" not in prompt and "쇼츠 한 편" in prompt
    assert "longform" not in ShortsPack.model_json_schema()["properties"]

    result = _run(cfg, monkeypatch)
    out = cfg.output_dir / RUN_DATE
    assert (out / "script-shorts.md").exists() and not (out / "script-longform.md").exists()
    assert not list(out.glob("longform-chapters*.csv")) and not list(out.glob("thumb-longform*"))
    notes = (out / "production-notes.md").read_text(encoding="utf-8")
    assert "제목 후보 (쇼츠)" in notes and "제목 후보 (롱폼)" not in notes
    assert not [w for w in result.warnings if "실패" in w], result.warnings


def test_longform_daily_setting_brings_the_old_path_back(cfg, monkeypatch):
    from rebrief.prompts import build_video_user

    cfg.settings["video"]["longform"] = "daily"
    assert "■ 롱폼" in build_video_user(cfg)
    _run(cfg, monkeypatch)
    assert (cfg.output_dir / RUN_DATE / "script-longform.md").exists()


def test_weekly_review_writes_the_longform_script_from_the_review(cfg, monkeypatch):
    from rebrief.prompts import build_weekly_longform_messages
    from rebrief.weekly import run_weekly

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    made: list[FakeGenerator] = []

    class Spy(FakeGenerator):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr("rebrief.weekly.ContentGenerator", Spy)
    _seed_days(cfg, ["2026-09-02", "2026-09-04", "2026-09-06"])
    result = run_weekly(cfg, end_date="2026-09-06", use_llm=True)

    assert made[0].calls == ["weekly", "weekly_longform"]            # 결산 글 먼저, 그 글로 대본
    assert not result.warnings, result.warnings
    out = result.out_dir
    script = (out / "script-longform.md").read_text(encoding="utf-8")
    assert "## 콜드오픈" in script and list(out.glob("longform-chapters_*.csv"))
    notes = (out / "production-notes.md").read_text(encoding="utf-8")
    assert "── 챕터 ──" in notes and "이번 주" in notes
    assert (out / "thumb-longform.svg").exists() and not (out / "thumb-shorts.svg").exists()

    # 대본 프롬프트에는 결산 글이 통째로 실린다
    review = made[0].generate_weekly([{"date": "2026-09-06", "headline": "h"}], result.week)
    _, user = build_weekly_longform_messages(cfg, [{"date": "2026-09-06", "issues": []}], result.week, review)
    assert review.title in user and review.body_markdown in user and "■ 롱폼" in user

    # 사이트의 주간 결산 칸에 롱폼 대본이 「자막만 복사」 버튼과 함께 올라간다
    site = build_site(cfg, cfg.repo_root / "site")
    page = (site / "weekly" / result.week / "script-longform.html").read_text(encoding="utf-8")
    assert 'id="tts-copy"' in page and (site / "weekly" / result.week / "production-notes.html").exists()


def test_weekly_longform_failure_keeps_the_review(cfg, monkeypatch):
    from rebrief.weekly import run_weekly

    class Broken(FakeGenerator):
        def generate_weekly_longform(self, *a, **k):
            raise RuntimeError("대본 한도 초과")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.weekly.ContentGenerator", Broken)
    _seed_days(cfg, ["2026-09-02", "2026-09-04", "2026-09-06"])
    result = run_weekly(cfg, end_date="2026-09-06", use_llm=True)
    assert (result.out_dir / "weekly.md").exists() and not (result.out_dir / "script-longform.md").exists()
    assert any("주간 롱폼 대본 생성 실패" in w for w in result.warnings)


# ── 쇼츠 음성 ────────────────────────────────────────────────


def test_no_key_means_no_voice_and_no_warning(cfg, monkeypatch):
    monkeypatch.setattr(voice, "_post", lambda *a, **k: pytest.fail("열쇠 없이 일레븐랩스를 불렀다"))
    result = _run(cfg, monkeypatch)
    out = cfg.output_dir / RUN_DATE
    assert not list(out.glob("*.mp3")) and not (out / voice.SIDECAR).exists()
    assert result.voice_file == "" and not [w for w in result.warnings if "음성" in w]


def test_daily_run_reads_the_shorts_script_into_an_mp3(cfg, monkeypatch, eleven):
    result = _run(cfg, monkeypatch)
    out = cfg.output_dir / RUN_DATE
    mp3 = out / result.voice_file
    assert result.voice_file.startswith("script-shorts_") and mp3.read_bytes() == MP3

    assert len(eleven) == 1
    sent = eleven[0]
    assert sent["url"].endswith("/v1/text-to-speech/" + cfg.get("voice.voice_id"))
    assert sent["headers"]["xi-api-key"] == KEY
    text = sent["json"]["text"]
    assert "3주째 하락" in text and "낙폭은 오히려 줄었습니다" in text
    assert "자막 카드" not in text and "항공샷" not in text           # 화면 지시는 읽히지 않는다
    assert "%" not in text and "퍼센트" in text                        # 기호는 말로 풀어 읽힌다

    side = json.loads((out / voice.SIDECAR).read_text(encoding="utf-8"))
    assert side["file"] == result.voice_file and side["voice_name"] == "Theo" and side["chars"] == len(text)
    assert KEY not in (out / voice.SIDECAR).read_text(encoding="utf-8")


def test_same_script_and_voice_is_not_read_twice(cfg, monkeypatch, eleven):
    _run(cfg, monkeypatch)
    out = cfg.output_dir / RUN_DATE
    name = json.loads((out / voice.SIDECAR).read_text(encoding="utf-8"))["file"]

    again = voice.make_shorts_voice(cfg, out, name)
    assert again.ok and again.reused and len(eleven) == 1            # 다시 그려도 글자 수가 또 나가지 않는다

    other = voice.make_shorts_voice(cfg, out, name, pick="Hunmin")    # 목소리를 바꾸면 다시 읽힌다
    assert other.ok and not other.reused and len(eleven) == 2
    assert eleven[1]["url"].endswith("MpbDJfQJUYUnp0i1QvOZ") and other.remaining == 55000
    assert json.loads((out / voice.SIDECAR).read_text(encoding="utf-8"))["voice_name"] == "Hunmin"


def test_a_refusal_is_explained_and_never_breaks_the_run(cfg, monkeypatch, eleven):
    monkeypatch.setattr(voice, "_post", lambda url, **kw: Reply(
        401, b"", {"detail": {"status": "quota_exceeded", "message": "quota"}}))
    result = _run(cfg, monkeypatch)
    out = cfg.output_dir / RUN_DATE
    assert (out / "script-shorts.md").exists() and not list(out.glob("*.mp3"))
    assert any("글자 수를 다 썼습니다" in w for w in result.warnings)
    assert KEY not in " ".join(result.warnings)

    def boom(url, **kw):
        raise ConnectionError("net down")

    monkeypatch.setattr(voice, "_post", boom)
    made = voice.make_shorts_voice(cfg, out, "x.mp3")
    assert not made.ok and "닿지 못했습니다" in made.note


def test_an_overlong_script_is_not_sent(cfg, monkeypatch, eleven, tmp_path):
    (tmp_path / voice.SCRIPT).write_text(
        "| # | 시각 | 자막 (읽는 말) | 화면 |\n| --- | --- | --- | --- |\n"
        + "".join(f"| {i} | `00:{i:02d}` | 같은 말을 되풀이합니다 | 화면 |\n" for i in range(1, 200)),
        encoding="utf-8")
    made = voice.make_shorts_voice(cfg, tmp_path, "x.mp3")
    assert not made.ok and "한도" in made.note and not eleven


def test_the_kept_voice_wins_over_the_settings_default(cfg):
    assert voice.resolve(cfg) == (cfg.get("voice.voice_id"), "Theo")
    voice.save_choice(cfg, "MpbDJfQJUYUnp0i1QvOZ", "Hunmin")
    assert voice.resolve(cfg) == ("MpbDJfQJUYUnp0i1QvOZ", "Hunmin")
    assert voice.resolve(cfg, "Lee")[1] == "Lee"                      # 그 한 번만 고른 것이 가장 세다
    assert voice.resolve(cfg, "abcDEF123") == ("abcDEF123", "abcDEF123")   # 목록에 없는 voice_id 도 그대로


def test_voice_form_lists_the_same_voices_as_the_settings(cfg):
    form = yaml.safe_load((ROOT / ".github/workflows/voice.yml").read_text(encoding="utf-8"))
    inputs = form[True]["workflow_dispatch"]["inputs"]          # yaml 은 on: 을 True 로 읽는다
    names = [v["name"] for v in voice.choices(cfg)]
    assert inputs["voice"]["options"] == names and len(set(names)) == len(names)
    assert inputs["voice"]["default"] == voice.resolve(cfg)[1]


def test_voice_command_remakes_a_day_with_another_voice(cfg, monkeypatch, eleven, capsys):
    from rebrief.cli import main

    _run(cfg, monkeypatch)
    monkeypatch.setattr("rebrief.cli.load_config", lambda *a, **k: cfg)
    assert main(["voice", "--date", RUN_DATE, "--voice", "Jiyoung", "--keep", "--force"]) == 0
    out = capsys.readouterr().out
    assert "Jiyoung" in out and KEY not in out
    assert voice.resolve(cfg)[1] == "Jiyoung"
    assert len(list((cfg.output_dir / RUN_DATE).glob("script-shorts*.mp3"))) == 1


def test_site_offers_the_voice_for_download_and_the_alert_links_it(cfg, monkeypatch, eleven):
    from rebrief.notify import build_run_message

    result = _run(cfg, monkeypatch)
    site = build_site(cfg, cfg.repo_root / "site")
    page = (site / RUN_DATE / "script-shorts.html").read_text(encoding="utf-8")
    assert (site / RUN_DATE / result.voice_file).read_bytes() == MP3
    assert f'href="{result.voice_file}" download' in page and "<audio" in page
    assert "actions/workflows/voice.yml" in page and "Hunmin" in page

    text = build_run_message(date=RUN_DATE, headline="h", issues=3, articles=30,
                             site_url="https://example.com/x/", warnings=[], llm_used=True,
                             voice_file=result.voice_file)
    assert f"https://example.com/x/{RUN_DATE}/{result.voice_file}" in text

def test_shorts_prompt_teaches_the_motion_studio_direction_shape(cfg):
    """쇼츠 화면 지시에 motion-studio 가 읽는 그래픽 모양을 붙이게 안내하는지 (2026-09-18).

    motion-studio(scripts/template-suggest.mjs parseDirection)가 ` + ` 뒤의 「종류: 제목 / 내용」과
    `/ 출처: 기관` 을 읽어 템플릿을 채웁니다. 종류 이름을 바꾸면 그쪽도 같이 바꿔야 합니다.
    """
    from rebrief.models import CaptionLine
    from rebrief.prompts import build_video_user

    prompt = build_video_user(cfg)
    for kind in ("큰 숫자:", "막대:", "추세:", "순위:", "비교:", "흐름:", "목록:", "체크:"):
        assert kind in prompt, kind
    assert "/ 출처: 기관" in prompt and "위 자료에 있는 것만" in prompt
    example = next(line for line in prompt.splitlines() if line.strip().startswith("예:"))
    assert " + " in example and "/ 출처: " in example
    assert "출처: 기관" in CaptionLine.model_json_schema()["properties"]["visual"]["description"]


def test_subtitles_carry_every_line_the_voice_reads():
    """음성에만 있고 자막 파일·컷 CSV 에는 없는 말이 없게 (2026-09-18 사용자 결정 A).

    9/18 부동산 쇼츠는 마무리가 표 밖에만 있어 음성에만 읽혔고, motion-studio 가 음성 끝을 표의 마지막
    줄로 알고 맞춰 마지막 화면이 안 생겼다. 훅(자막이 00:03 부터일 때)도 같은 규칙이다.
    """
    from rebrief.models import CaptionLine, ShortsScript
    from rebrief.render import shorts_cut_csv, spoken_caption_lines, to_srt
    from rebrief.tts import spoken_extras

    rows = [CaptionLine(at="00:03", text="강남 거래가 줄었고", visual="막대"),
            CaptionLine(at="00:06", text="확인될 예정입니다", visual="달력")]
    shorts = ShortsScript.model_construct(title_candidates=[], hook="강남 거래 반토막, 무슨 일일까요?", lines=rows,
                                          cta="다음주 발표될 통계 부돌보 브리핑에서 확인하세요.", hashtags=[],
                                          estimated_seconds=15)
    lines = spoken_caption_lines(shorts)
    texts = [line.text for line in lines]
    assert texts[0] == "강남 거래 반토막, 무슨 일일까요?"           # 00:03 부터라 훅이 앞에
    assert texts[1:3] == ["강남 거래가 줄었고", "확인될 예정입니다"]
    tail = texts[3:]
    assert len(tail) == 2 and all(len(t) <= 16 for t in tail)     # 긴 마무리는 화면 한 줄씩으로
    assert " ".join(tail) == "다음주 발표될 통계 부돌보 브리핑에서 확인하세요"
    assert all(l.visual.startswith("마무리") for l in lines[3:])
    srt, csv = to_srt(lines, 15), shorts_cut_csv(shorts)
    for t in texts:
        assert t in csv
    assert "브리핑에서 확인하세요" in srt

    # 표가 00:00 부터이고 마무리가 이미 끝줄에 있으면 아무것도 붙지 않는다 — 음성도 같은 판단
    rows0 = [CaptionLine(at="00:00", text="서울 아파트값이", visual=""),
             CaptionLine(at="00:02", text="구독과 알림 부탁드립니다", visual="")]
    plain = shorts.model_copy(update={"lines": rows0, "cta": "구독과 알림 부탁드립니다."})
    assert [l.text for l in spoken_caption_lines(plain)] == [r.text for r in rows0]
    assert spoken_extras([r.text for r in rows0], False, plain.hook, plain.cta) == ("", "")


def test_voice_reads_listed_words_as_they_sound(tmp_path):
    """「신고가」는 [신고까]로 읽힌다 — 설정 voice.say_as 의 낱말만, 음성에 보낼 글에서만 (2026-09-18 사용자 요청)."""
    from rebrief import voice as voice_mod
    from rebrief.tts import say_as, speakable

    table = {"신고가": "신고까"}
    assert say_as("노원은 신고가였고 신고가 거래가", table) == "노원은 신고까였고 신고까 거래가"
    assert say_as("신고가", None) == "신고가"                      # 표가 없으면(정치) 그대로
    assert speakable("41.3㎡ 신고가", table) == "41.3제곱미터 신고까"
    (tmp_path / voice_mod.SCRIPT).write_text("## 자막\n\n| 1 | `00:00` | 상계주공12 신고가 | 화면 |\n", encoding="utf-8")
    assert "신고까" in voice_mod.spoken_text(tmp_path, True, table)
    assert "신고까" in voice_mod.spoken_text(tmp_path, False, table)
    assert "신고가" in voice_mod.spoken_text(tmp_path, True)
