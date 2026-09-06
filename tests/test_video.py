"""쇼츠 초안 영상 — 도구 없이 검증할 수 있는 부분(타이밍·카드·건너뜀)만 본다."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from rebrief import video
from rebrief.models import CaptionLine, ShortsScript
from rebrief.render import caption_timings, to_srt


def _lines():
    return [
        CaptionLine(at="00:00", text="2030년 서울 25개구 중", visual="서울 항공샷"),
        CaptionLine(at="00:03", text="22개구가 종부세 대상", visual="지도 그래픽"),
        CaptionLine(at="이상함", text="타임코드 없음", visual=""),
        CaptionLine(at="00:02", text="거꾸로 간 시각", visual=""),
    ]


def test_타이밍은_SRT_와_같은_규칙이다():
    t = caption_timings(_lines(), total_seconds=12)
    assert [round(a, 1) for a, _ in t] == [0.0, 3.0, 5.5, 6.0]     # 못 읽으면 +2.5, 역행이면 +0.5
    assert t[-1][1] == 12.0
    assert "00:00:05,500 --> 00:00:06,000" in to_srt(_lines(), 12)


def test_전체_길이가_없으면_마지막은_3초():
    t = caption_timings(_lines()[:1])
    assert t == [(0.0, 3.0)]
    assert caption_timings([]) == []


def test_카드는_자막과_진행률을_담는다():
    svg = video.frame_svg("22개구가 종부세 대상", 2, 20, channel="부동산 브리핑", visual="지도 그래픽")
    root = ET.fromstring(svg)
    assert root.get("width") == "1080" and root.get("height") == "1920"
    assert "22개구가 종부세 대상" in svg and ">2/20<" in svg
    assert "지도 그래픽" in svg                    # 그림이 없으면 화면 지시를 자리 표시로


def test_그림이_있으면_데이터_URI_로_넣는다(tmp_path):
    png = tmp_path / "img-1.png"
    png.write_bytes(b"\x89PNG\r\n")
    svg = video.frame_svg("자막", 1, 1, image=png)
    assert 'href="data:image/png;base64,' in svg


def test_도구가_없으면_아무것도_만들지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "ffmpeg_path", lambda: None)
    shorts = ShortsScript(title_candidates=["a"], hook="h", lines=_lines(), cta="c",
                          hashtags=[], estimated_seconds=12)
    assert video.build_shorts_draft(tmp_path, shorts, [], channel="x") is None
    assert not list(tmp_path.iterdir())
