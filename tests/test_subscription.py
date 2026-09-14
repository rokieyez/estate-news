"""구독 호출이 나뉜 답·안전 검사·틀린 형식에서 다시 묻는지 (2026-09-14, rebrief/subscription.py).

두 저장소(estate-news · politics-news)에 똑같이 둔다 — tests/test_shared_files.py 의 SHARED.
가짜 claude 는 부를 때마다 answers.json 의 다음 답을 내놓고, 받은 한도와 요청문 끝을 calls.log 에 적는다.
"""

import json
import os
import stat
import sys

import pydantic
import pytest

from rebrief.subscription import SubscriptionError, ask, extract_json, looks_cut

_FAKE = r'''#!{python}
import json, os, sys
d = os.environ["FAKE_SEQ_DIR"]
prompt = sys.stdin.read()
log = os.path.join(d, "calls.log")
n = len(open(log, encoding="utf-8").read().splitlines()) if os.path.exists(log) else 0
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"cap": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), "tail": prompt[-300:],
                         "api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}}, ensure_ascii=False) + "\n")
answers = json.load(open(os.path.join(d, "answers.json"), encoding="utf-8"))
answer = answers[min(n, len(answers) - 1)]
print(json.dumps(answer, ensure_ascii=False))
sys.exit(1 if answer.get("is_error") else 0)
'''


class Item(pydantic.BaseModel):
    label: str


class Card(pydantic.BaseModel):
    title: str
    items: list[Item]


class _Cfg:
    def get(self, key, default=None):
        return default


GOOD = {"title": "오늘", "items": [{"label": "하나"}, {"label": "둘"}]}
# 한도에 닿아 두 턴으로 나뉘면 result 에 뒤 턴만 온다 — 목록 한가운데부터 시작하는 조각
SPLIT_TAIL = {"type": "result", "is_error": False, "num_turns": 2, "stop_reason": "end_turn",
              "result": '"}, {"label": "둘"}]}\n```', "usage": {"output_tokens": 17000}}
REFUSAL = {"type": "result", "is_error": True, "num_turns": 2, "stop_reason": "refusal",
           "result": "API Error: Opus 5's safeguards flagged this message. Details: `[reasoning_extraction]`",
           "usage": {"output_tokens": 16500}}


def _ok(obj, turns=1):
    return {"type": "result", "is_error": False, "num_turns": turns, "stop_reason": "end_turn",
            "result": "네, 정리했습니다.\n```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```",
            "usage": {"output_tokens": 900}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake-seq"
    (d / "bin").mkdir(parents=True)
    exe = d / "bin" / "claude"
    exe.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SEQ_DIR", str(d))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-남은키")      # 넘기면 구독 대신 API 로 청구된다

    def use(*answers):
        (d / "answers.json").write_text(json.dumps(list(answers), ensure_ascii=False), encoding="utf-8")
        return d
    return use


def _calls(d):
    return [json.loads(x) for x in (d / "calls.log").read_text(encoding="utf-8").splitlines()]


def _ask(max_tokens=16000):
    return ask(_Cfg(), system="시스템", user="정리해 주세요", output_format=Card, model="claude-opus-5",
               max_tokens=max_tokens, timeout=60)


def test_split_answer_is_asked_again_with_twice_the_budget(fake):
    d = fake(SPLIT_TAIL, _ok(GOOD))
    assert _ask().parsed_output == Card.model_validate(GOOD)
    calls = _calls(d)
    assert [c["cap"] for c in calls] == ["16000", "32000"]
    assert not any(c["api_key"] for c in calls), "ANTHROPIC_API_KEY 가 claude 에 넘어갔다"


def test_split_answer_that_still_holds_the_whole_json_is_used(fake):
    d = fake(_ok(GOOD, turns=2))
    assert _ask().parsed_output.title == "오늘"
    assert len(_calls(d)) == 1


def test_refused_continuation_is_retried_with_a_bigger_budget(fake):
    d = fake(REFUSAL, _ok(GOOD))
    assert _ask(32000).parsed_output.items[0].label == "하나"
    assert [c["cap"] for c in _calls(d)] == ["32000", "64000"]


def test_wrong_shape_is_asked_again_saying_what_was_wrong(fake):
    d = fake(_ok({"title": "오늘"}), _ok(GOOD))
    assert _ask().parsed_output.items[1].label == "둘"
    calls = _calls(d)
    assert [c["cap"] for c in calls] == ["16000", "16000"]          # 형식 문제는 한도를 올리지 않는다
    assert "앞선 답" not in calls[0]["tail"]
    assert "형식에 맞지 않았습니다" in calls[1]["tail"] and "items 없음" in calls[1]["tail"]


def test_gives_up_after_three_tries_and_says_why(fake):
    d = fake(SPLIT_TAIL)
    with pytest.raises(SubscriptionError) as err:
        _ask()
    assert "두 턴으로 나뉘어" in err.value.message and not err.value.retryable
    assert [c["cap"] for c in _calls(d)] == ["16000", "32000", "64000"]


def test_refused_every_time_lets_the_fallback_model_try(fake):
    fake(REFUSAL)
    with pytest.raises(SubscriptionError) as err:
        _ask()
    assert err.value.retryable and "안전 검사" in err.value.message


def test_extract_json_skips_inner_pieces_when_fields_are_required():
    tail = SPLIT_TAIL["result"]
    assert extract_json(tail) == {"label": "둘"}                    # 예전에는 이 조각을 답으로 집었다
    assert extract_json(tail, required={"title", "items"}) is None
    assert extract_json("앞말 " + json.dumps(GOOD, ensure_ascii=False), required={"title", "items"}) == GOOD
    assert looks_cut('```json\n{"title": "오') and not looks_cut("```json\n{}\n```")
