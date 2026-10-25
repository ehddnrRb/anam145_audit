"""scan.py parse_candidates() 를 실제 SDK 타입(anthropic.types.Message)으로 시험한다 (DECISIONS D-17).

scan.py --selftest 는 SimpleNamespace 가짜 객체로 같은 케이스를 돌린다. 여기는 SDK 가
실제로 만드는 객체에서도 속성 접근이 맞는지 본다. API 는 호출하지 않는다.
"""
import json
import time

import pytest

from adapters.attacktrail import load_scan_module

scan = load_scan_module()
CAND = {"title": "t", "file": "V.sol", "line": 4, "code_excerpt": "x", "category": "c",
        "severity_guess": "High", "why": "w", "poc_sketch": "p"}
CANDS = [CAND]
GOOD = json.dumps({"candidates": CANDS})
THINK = {"type": "thinking", "thinking": "", "signature": "sig"}  # display 기본값 omitted


def text(s):
    return {"type": "text", "text": s}


def test_wait_result_raises_a_task_timeout_error_instead_of_waiting():
    # D-22: wait_result used FutureTimeout from f.result(step) to mean "not done yet". FutureTimeout is the builtin
    # TimeoutError, so a task that itself raised TimeoutError looked unfinished forever (with until=None, a loop).
    from concurrent.futures import ThreadPoolExecutor

    def task():
        raise TimeoutError("inner")
    with ThreadPoolExecutor(1) as pool:
        f = pool.submit(task)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="inner"):
            scan.wait_result(f, until=time.monotonic() + 3)   # old code: spun to the 3 s limit, then an empty one
        assert time.monotonic() - t0 < 1.5


@pytest.fixture(scope="module")
def msg():
    types = pytest.importorskip("anthropic.types")

    def make(content, stop_reason="end_turn", stop_details=None, usage=None):
        d = {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
             "content": content, "stop_reason": stop_reason, "stop_sequence": None,
             "usage": usage or {"input_tokens": 1, "output_tokens": 1}}
        if stop_details is not None:
            d["stop_details"] = stop_details
        return types.Message.model_validate(d)
    return make


def test_scan_py_selftest_passes():
    scan.selftest()  # SDK 없이도 도는 쪽. 위치 검증 6건 + 가짜 응답 + check_candidates + usage_of


def test_thinking_block_first_reproduces_old_bug_and_is_fixed(msg):
    m = msg([THINK, text(GOOD)])
    assert type(m.content[0]).__name__ == "ThinkingBlock"
    with pytest.raises(AttributeError):
        m.content[0].text                      # 옛 코드: json.loads(r.content[0].text)
    assert scan.parse_candidates(m) == CANDS


def test_plain_text_response(msg):
    assert scan.parse_candidates(msg([text(GOOD)])) == CANDS


def test_empty_candidates_is_valid_not_error(msg):
    assert scan.parse_candidates(msg([THINK, text('{"candidates": []}')])) == []


def test_text_split_across_blocks_is_joined(msg):
    assert scan.parse_candidates(msg([text(GOOD[:15]), text(GOOD[15:])])) == CANDS


def test_redacted_thinking_block_is_skipped(msg):
    redacted = {"type": "redacted_thinking", "data": "xxx"}
    assert scan.parse_candidates(msg([redacted, THINK, text(GOOD)])) == CANDS


@pytest.mark.parametrize("details, want", [
    ({"type": "refusal", "category": "cyber", "explanation": "x"}, "stop_reason=refusal category=cyber"),
    ({"type": "refusal", "category": None, "explanation": None}, "stop_reason=refusal category=null"),
    (None, "stop_reason=refusal category=null"),   # stop_details 자체가 없는 refusal
])
def test_refusal_reports_category(msg, details, want):
    m = msg([THINK], stop_reason="refusal", stop_details=details)
    with pytest.raises(scan.ScanError) as e:
        scan.parse_candidates(m)
    assert str(e.value) == want


@pytest.mark.parametrize("reason", ["max_tokens", "pause_turn", "model_context_window_exceeded",
                                    "stop_sequence", "tool_use"])
def test_non_end_turn_raises_with_reason(msg, reason):
    # 텍스트가 멀쩡해 보여도 end_turn 이 아니면 믿지 않는다 (max_tokens 는 JSON 이 잘렸을 수 있다)
    with pytest.raises(scan.ScanError, match="stop_reason=" + reason):
        scan.parse_candidates(msg([THINK, text(GOOD)], stop_reason=reason))


def test_no_text_block_raises(msg):
    with pytest.raises(scan.ScanError, match=r"text 블록 없음.*thinking"):
        scan.parse_candidates(msg([THINK]))


@pytest.mark.parametrize("body, want", [
    ("not json", "JSON 파싱 실패"),
    ('{"candidates": {}}', "candidates 가 list 아님"),
    ('{"other": []}', "candidates 가 list 아님"),
    ('[]', "candidates 가 list 아님"),
    ('{"candidates": ["x"]}', "candidates 항목이 dict 아님"),
])
def test_bad_json_shape_raises(msg, body, want):
    with pytest.raises(scan.ScanError, match=want):
        scan.parse_candidates(msg([text(body)]))


@pytest.mark.parametrize("field, value, want", [
    ("line", "5", r"line 이 정수 아님 \(str\)"),
    ("line", None, r"line 이 정수 아님 \(NoneType\)"),
    ("line", 1.5, r"line 이 정수 아님 \(float\)"),
    ("line", True, r"line 이 정수 아님 \(bool\)"),
    ("code_excerpt", 5, r"code_excerpt 가 문자열 아님 \(int\)"),
    ("code_excerpt", None, r"code_excerpt 가 문자열 아님 \(NoneType\)"),
    ("why", ["w"], r"why 가 문자열 아님 \(list\)"),
])
def test_candidate_field_type_violation_raises_scan_error(msg, field, value, want):
    # 옛 코드는 여기서 verify() 가 TypeError 로 프로세스를 죽였다. 이제 그 파일만 ERROR.
    # 둘째 후보가 나쁘다: 앞의 정상 후보가 있어도 파일 전체를 믿지 않는다.
    body = json.dumps({"candidates": [CAND, dict(CAND, **{field: value})]})
    with pytest.raises(scan.ScanError, match=r"candidates\[1\]\." + want):
        scan.parse_candidates(msg([text(body)]))


def test_check_candidates_accepts_valid_and_ignores_file_field():
    assert scan.check_candidates([]) == []
    cs = [CAND, dict(CAND, file=None)]   # file 은 main() 이 덮어쓴다
    assert scan.check_candidates(cs) is cs


def test_verify_would_have_crashed_on_bad_line(tmp_path):
    # check_candidates 가 막는 이유: verify() 에 그대로 가면 TypeError
    (tmp_path / "V.sol").write_text("a\nb\n", encoding="utf-8")
    for bad in ("5", None):
        with pytest.raises(TypeError):
            scan.verify(str(tmp_path), dict(CAND, line=bad))


def test_usage_of_reads_sdk_usage_including_cache_split(msg):
    plain = scan.usage_of(msg([text(GOOD)]))
    # D-23: 모든 행에 응답 모델(model)과 폴백 여부(fallback)가 붙는다. iterations 가 없으면 fallback 은 False
    assert plain == {"input_tokens": 1, "cache_creation_input_tokens": None,
                     "cache_read_input_tokens": None, "output_tokens": 1,
                     "model": "claude-sonnet-5", "fallback": False}
    full = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 20,
            "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0}}
    # refusal 이어도 usage 는 읽힌다 (비용이 나간 호출)
    got = scan.usage_of(msg([THINK], stop_reason="refusal", usage=full))
    assert got == dict(full, model="claude-sonnet-5", fallback=False)


def test_error_message_is_one_line(msg):
    # ERROR 는 "  [i/n] <file>  ERROR <msg>" 한 줄로 찍힌다. 메시지에 줄바꿈이 있으면 로그가 갈라진다.
    for body in ("line1\nline2", "{\n"):
        with pytest.raises(scan.ScanError) as e:
            scan.parse_candidates(msg([text(body)]))
        assert "\n" not in str(e.value)


# ---------------------------------------------------------------------------------------------
# D-20
# ---------------------------------------------------------------------------------------------

def test_usage_of_adds_service_tier_speed_and_geo_only_when_present(msg):
    # SDK Usage 의 service_tier·speed·inference_geo. 값이 없으면(None) 키를 만들지 않는다
    got = scan.usage_of(msg([text(GOOD)], usage={"input_tokens": 1, "output_tokens": 2, "service_tier": "priority",
                                                  "speed": "fast", "inference_geo": "us"}))
    assert got == {"input_tokens": 1, "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
                   "output_tokens": 2, "service_tier": "priority", "speed": "fast", "inference_geo": "us",
                   "model": "claude-sonnet-5", "fallback": False}
    only_tier = scan.usage_of(msg([text(GOOD)], usage={"input_tokens": 1, "output_tokens": 2,
                                                        "service_tier": "standard"}))
    assert "speed" not in only_tier and "inference_geo" not in only_tier and only_tier["service_tier"] == "standard"


@pytest.mark.parametrize("extra", [{"_usage": {"input_tokens": 10 ** 6}}, {"_run": {}}, {"_status": "triage"},
                                   {"confidence": 0.9}])
def test_candidate_keys_outside_the_schema_are_a_violation(msg, extra):
    # 블랙박스 ad: 후보 안의 "_usage" 가 결과 JSONL 에서 usage 행으로 오인됐다. SCHEMA properties 밖 키는 전부 위반이다
    props = scan.SCHEMA["properties"]["candidates"]["items"]["properties"]
    assert not set(extra) & set(props)
    body = json.dumps({"candidates": [dict(CAND, **extra)]})
    with pytest.raises(scan.ScanError, match=r"candidates\[0\] 에 스키마 밖 키: '%s'" % next(iter(extra))):
        scan.parse_candidates(msg([text(body)]))
    # SCHEMA 자체는 그대로다: 8개 필드가 전부 properties 에 있고 required 와 같다
    assert set(props) == set(scan.SCHEMA["properties"]["candidates"]["items"]["required"]) == set(CAND)


def test_extra_key_message_is_one_line_and_bounded():
    with pytest.raises(scan.ScanError) as e:
        scan.check_candidates([dict(CAND, **{"x\ny" * 40: 1, "b": 1, "c": 1, "d": 1})])
    s = str(e.value)
    assert "\n" not in s and s.count("'") <= 12 and "'d'" not in s     # 3개까지만, 줄바꿈은 repr 로 가린다


def test_verify_returns_read_error_instead_of_raising(tmp_path, monkeypatch):
    (tmp_path / "V.sol").write_text("a\nb\n", encoding="utf-8")
    c = dict(CAND, file="V.sol", line=1, code_excerpt="a")
    assert scan.verify(str(tmp_path), c) == "OK"

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(scan.io, "open", boom)      # isfile 은 참인데 읽을 때 실패한다(권한·잠금·사라짐)
    assert scan.verify(str(tmp_path), c) == "READ_ERROR"
    assert not "READ_ERROR".startswith("OK")        # main() 은 이를 rejected_by_scanner 로 돌린다


@pytest.mark.parametrize("status, want", [
    (400, "400"), (401, "fatal"), (402, "fatal"), (403, "fatal"), (404, "fatal"), (408, None), (409, None),
    (413, None), (422, None), (429, "429"), (500, "5xx"), (503, "5xx"), (529, "5xx")])
def test_classify_real_sdk_status_errors(status, want):
    httpx2 = pytest.importorskip("httpx2")
    anthropic = pytest.importorskip("anthropic")
    req = httpx2.Request("POST", "http://127.0.0.1:1/v1/messages")
    resp = httpx2.Response(status, request=req, json={"type": "error", "error": {"type": "x", "message": "x"}})
    # 예외는 SDK 가 상태 코드로 직접 고른 클래스다(요청은 나가지 않는다). 키는 더미다.
    e = anthropic.Anthropic(api_key="dummy-not-a-real-key")._make_status_error_from_response(resp)
    assert e.status_code == status
    assert scan.classify(e) == want, type(e).__name__
    if status == 402:   # SDK 에 402 전용 클래스가 없다(error-codes.md 는 billing_error, 재시도 불가). 그래서 상태 코드로 본다
        assert type(e) is anthropic.APIStatusError


def test_classify_real_sdk_connection_and_timeout_errors():
    httpx2 = pytest.importorskip("httpx2")
    anthropic = pytest.importorskip("anthropic")
    req = httpx2.Request("POST", "http://127.0.0.1:1/v1/messages")
    assert scan.classify(anthropic.APITimeoutError(request=req)) == "timeout"
    assert scan.classify(anthropic.APIConnectionError(request=req)) == "connection"
    assert scan.classify(ValueError("local")) is None and scan.classify(scan.ScanError("x")) is None


def test_call_limits_fit_the_time_left_and_default_to_the_sdk_when_time_is_plentiful():
    assert scan.call_limits(10 ** 6) == (scan.CALL_TIMEOUT_S, scan.SDK_RETRIES) == (1800, 2)
    # 하네스 기본 1800초 → 데드라인 1785초 → 첫 호출은 약 1782초가 남아 있다: 재시도 2회가 남고 시도당 약 589초(CALL_TIMEOUT_S 아래)
    t, r = scan.call_limits(1782)
    assert r == 2 and 580 < t < 600
    # CALL_TIMEOUT_S 에 닿는 경계: 남은 시간이 (1800 × 3 + 2 × 8)초 이상이어야 시도당 1800초다 (D-25)
    assert scan.call_limits(5416) == (1800, 2) and scan.call_limits(5415)[0] < 1800
    for rem in [1, 1.0, 2, 30, 100, 127, 128, 200, 247, 248, 300, 375, 376, 377, 500, 1000, 1816, 1817, 1e5]:
        t, r = scan.call_limits(rem)
        worst = (r + 1) * t + r * scan.RETRY_DELAY_S          # 시도마다 timeout + 재시도 사이 최대 대기
        assert t <= scan.CALL_TIMEOUT_S and worst <= rem + 1e-9, (rem, t, r, worst)
        assert 0 <= r <= scan.SDK_RETRIES
        assert r == 0 or t >= scan.MIN_RETRY_ATTEMPT_S         # 재시도를 쓰면 시도 하나가 충분히 길다
    assert scan.call_limits(scan.MIN_CALL_S) == (scan.MIN_CALL_S, 0)


def test_breaker_counts_consecutive_counted_errors_only():
    b = scan.Breaker(8)
    seq = ["429"] * 7 + ["ok"] + ["5xx"] * 7 + [None] + ["400"]       # 응답이 끼면 리셋, 중립(None)은 건드리지 않는다
    assert [b.record(c) for c in seq] == [None] * 8 + [None] * 7 + [None] + ["aborted after 8 consecutive API errors (5xx,400)"]
    assert scan.BREAKER_N == 8


# ---------------------------------------------------------------------------------------------
# D-21: settle() 가짜 future 로 결정적으로 시험한다 (경합 4번, L-1)
# ---------------------------------------------------------------------------------------------

from concurrent.futures import Future  # noqa: E402
from concurrent.futures import TimeoutError as FutureTimeout  # noqa: E402

VAL = ("X.sol", [], None, {"input_tokens": 1})   # scan_one 의 (rel, 후보, 오류, usage)
PAST = lambda: time.monotonic() - 1          # 이미 지난 until: wait_result 가 한 번 보고 바로 FutureTimeout


class RacyFuture(Future):
    """result() 가 시간 초과를 내자마자 호출이 끝난다. 블랙박스 관찰 2의 경합: wait_result 가 FutureTimeout 을 낸
    직후, 감시가 f.running() 을 보기 전에 호출이 끝난다. 실제로는 타이밍 문제인데 여기서는 순서를 고정한다."""

    def result(self, timeout=None):
        if not self.done():
            self.set_result(VAL)
            raise FutureTimeout()
        return super().result(timeout)


class FlipFuture(Future):
    """running() 이 True 를 돌려준 직후 끝난다(감시가 running 을 읽고 done 을 읽는 사이에 끝나는 경합)."""

    def running(self):
        r = super().running()
        if not self.done():
            self.set_result(VAL)
        return r

    def result(self, timeout=None):
        if not self.done():
            raise FutureTimeout()
        return super().result(timeout)


def _running(cls=Future):
    f = cls()
    f.set_running_or_notify_cancel()
    return f


def test_settle_returns_the_result_of_a_call_that_ends_right_after_the_wait_gave_up():
    # 옛 코드는 FutureTimeout 뒤에 f.running() 만 봤다. 그 사이에 끝난 호출은 running() 이 False 라 "skipped: hard
    # deadline reached" 로 집계됐고, 이미 나가 과금된 호출의 결과·usage 가 사라졌다.
    assert scan.settle(_running(RacyFuture), "X.sol", PAST()) == (VAL, False)
    assert scan.settle(_running(FlipFuture), "X.sol", PAST()) == (VAL, False)   # running 을 읽은 직후 끝나도 같다


def test_settle_gives_up_only_on_calls_that_did_not_finish():
    assert scan.settle(_running(), "Y.sol", PAST()) == (("Y.sol", None, scan.STILL_RUNNING, None), True)
    # 시작도 못 한 대기열 파일: 요청이 안 나갔으니 skipped 이고 "버린 호출"로 세지 않는다 (L-1)
    assert scan.settle(Future(), "Z.sol", PAST()) == (("Z.sol", None, scan.HARD_SKIPPED, None), False)
    done = Future()
    done.set_result(VAL)
    assert scan.settle(done, "X.sol", PAST()) == (VAL, False)
    assert scan.settle(done, "X.sol", None) == (VAL, False)       # 데드라인이 없으면 끝날 때까지 기다린다(이미 끝남)
    assert "call still running" in scan.STILL_RUNNING and "토큰 미집계" in scan.STILL_RUNNING


# ---------------------------------------------------------------------------------------------
# D-23: beta 엔드포인트 응답(BetaMessage)과 서버 측 폴백. SDK 가 만드는 실제 타입으로 읽는다
# ---------------------------------------------------------------------------------------------

FB_BLOCK = {"type": "fallback", "from": {"model": "claude-sonnet-5-5"}, "to": {"model": "claude-sonnet-5"},
            "trigger": {"type": "refusal", "category": "cyber"}}
# 시도 둘: Sonnet 5.5 가 거절(message), Sonnet 5 가 응답(fallback_message). 최상위 usage 는 응답을 낸 시도뿐이다
REFUSED_IT = {"type": "message", "model": "claude-sonnet-5-5", "input_tokens": 100, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
SERVED_IT = {"type": "fallback_message", "model": "claude-sonnet-5", "input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
             "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30}}
TOP = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
       "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30}}


@pytest.fixture(scope="module")
def bmsg():
    beta = pytest.importorskip("anthropic.types.beta")

    def make(content, stop_reason="end_turn", stop_details=None, usage=None, model="claude-sonnet-5"):
        d = {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
             "content": content, "stop_reason": stop_reason, "stop_sequence": None,
             "usage": usage or {"input_tokens": 1, "output_tokens": 1}}
        if stop_details is not None:
            d["stop_details"] = stop_details
        return beta.BetaMessage.model_validate(d)
    return make


def test_beta_fallback_response_parses_candidates_and_records_model_and_attempts(bmsg):
    m = bmsg([FB_BLOCK, THINK, text(GOOD)], usage=dict(TOP, iterations=[REFUSED_IT, SERVED_IT]))
    assert [type(b).__name__ for b in m.content] == ["BetaFallbackBlock", "BetaThinkingBlock", "BetaTextBlock"]
    assert scan.parse_candidates(m) == CANDS           # fallback 블록은 text 가 아니라 읽히지 않는다
    u = scan.usage_of(m)
    assert (u["model"], u["fallback"]) == ("claude-sonnet-5", True)
    assert u["returned"] == {k: TOP[k] for k in scan.USAGE_KEYS}   # 최상위 usage = 응답을 낸 시도
    assert [(a["type"], a["model"], a["input_tokens"]) for a in u["attempts"]] == [
        ("message", "claude-sonnet-5-5", 100), ("fallback_message", "claude-sonnet-5", 10)]
    # 4개 토큰 키는 시도별 합계다: 거절한 시도의 입력 100 이 들어 있다(최상위 값만 쓰면 100 이 빠진다)
    assert (u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"],
            u["output_tokens"]) == (110, 30, 20, 5)
    assert u["cache_creation"] == {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30}


def test_beta_chain_refusal_is_a_refusal_error_and_not_counted_as_a_fallback_answer(bmsg):
    # 폴백 모델까지 거절하면 최종 stop_reason 이 refusal 이다. 응답을 낸 게 아니라 fallback 은 False 지만 과금 가능한 시도라 합산한다
    m = bmsg([], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"},
             usage=dict(TOP, iterations=[REFUSED_IT, SERVED_IT]))
    with pytest.raises(scan.ScanError) as e:
        scan.parse_candidates(m)
    assert str(e.value) == "stop_reason=refusal category=cyber"
    u = scan.usage_of(m)
    assert u["fallback"] is False and u["input_tokens"] == 110


@pytest.mark.parametrize("usage", [
    dict(TOP),                                                       # iterations 없음
    dict(TOP, iterations=None),
    dict(TOP, iterations=[{"type": "message", "model": "claude-sonnet-5", "input_tokens": 10, "output_tokens": 5,
                           "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20}]),   # 시도 하나
])
def test_beta_plain_response_is_not_a_fallback_and_keeps_the_top_level_usage(bmsg, usage):
    u = scan.usage_of(bmsg([text(GOOD)], usage=usage))
    assert u["fallback"] is False and u["model"] == "claude-sonnet-5"
    assert (u["input_tokens"], u["output_tokens"]) == (10, 5)
    assert "returned" not in u and "attempts" not in u


def test_a_fallback_served_turn_without_a_refused_attempt_still_counts_as_fallback(bmsg):
    # sticky 라우팅 턴: 요청한 모델이 돌지 않아 fallback 블록도 거절 시도 항목도 없고, fallback_message 하나뿐이다
    m = bmsg([text(GOOD)], usage=dict(TOP, iterations=[SERVED_IT]))
    assert not any(b.type == "fallback" for b in m.content)
    u = scan.usage_of(m)
    assert u["fallback"] is True and u["input_tokens"] == 10 and "attempts" not in u


def test_usage_tokens_are_never_below_the_top_level_value(bmsg):
    # 비용 상한을 과소 집계하지 않는다: 응답이 모순돼 시도별 합계가 최상위보다 작아도 큰 쪽을 쓴다
    small = dict(SERVED_IT, input_tokens=1, output_tokens=1)
    u = scan.usage_of(bmsg([text(GOOD)], usage=dict(TOP, iterations=[dict(REFUSED_IT, input_tokens=0), small])))
    assert (u["input_tokens"], u["output_tokens"]) == (10, 5)
    assert u["returned"]["input_tokens"] == 10


def test_stream_request_goes_through_the_sdk_beta_stream_helper_with_the_same_arguments():
    # scan.py 는 D-24 부터 client.beta.messages.stream(...) 을 with 로 연다. 인자 이름이 SDK 1.15.0 beta stream() 의 인자인지
    # (오타가 나면 TypeError 로 모든 파일이 ERROR 가 된다). stream() 도 betas·fallbacks·output_config·timeout 을 받는다
    import inspect

    anthropic = pytest.importorskip("anthropic")
    params = inspect.signature(anthropic.Anthropic(api_key="dummy-not-a-real-key").beta.messages.stream).parameters
    assert {"betas", "fallbacks", "output_config", "system", "max_tokens", "timeout"} <= set(params)
    assert scan.FALLBACK_BETA in anthropic.types.AnthropicBetaParam.__args__[1].__args__     # SDK 가 아는 베타 문자열
    assert (scan.FALLBACKS, scan.CACHE_TTL, scan.EFFORTS) == ("default", "5m", ("high", "xhigh", "max"))   # max: D-28
    assert scan.MAX_TOKENS == 64000                                  # D-24: 상수 하나. 사용자 결정(2026-10-21)


# ---------------------------------------------------------------------------------------------
# D-24: 스트리밍. 출력 도중 거절의 text 이어 붙이기, 끊긴 스트림의 부분 usage, 스트림 도중 오류의 분류
# ---------------------------------------------------------------------------------------------

MIDDLE = {"type": "fallback", "from": {"model": "claude-fable-5-1"}, "to": {"model": "claude-opus-5"},
          "trigger": {"type": "refusal", "category": "cyber"}}


def test_mid_stream_fallback_text_is_joined_across_the_boundary(bmsg):
    # content: [thinking(빈), text(부분 출력), fallback(경계), text(폴백 모델의 이어쓰기)]. 이어 붙여야 한 JSON 이다.
    cut = len(GOOD) // 2
    m = bmsg([THINK, text(GOOD[:cut]), MIDDLE, text(GOOD[cut:])], model="claude-opus-5")
    assert [b.type for b in m.content] == ["thinking", "text", "fallback", "text"]
    assert scan.parse_candidates(m) == CANDS
    # 경계 뒤만 읽으면 JSON 조각이라 안 읽힌다(그래서 앞 text 도 읽는다)
    tail_only = bmsg([THINK, text(GOOD[:cut]), MIDDLE, text(GOOD[cut:])]).content[3:]
    with pytest.raises(scan.ScanError, match="JSON 파싱 실패"):
        scan.parse_candidates(type(m).model_construct(stop_reason="end_turn", content=tail_only))


def test_mid_stream_fallback_that_rewrote_the_answer_from_scratch_is_an_error(bmsg):
    cut = len(GOOD) // 2
    m = bmsg([text(GOOD[:cut]), MIDDLE, text(GOOD)], model="claude-opus-5")
    with pytest.raises(scan.ScanError, match="JSON 파싱 실패"):   # 조용히 틀린 후보가 되지 않는다. 파일럿 확인 항목 (D-24)
        scan.parse_candidates(m)


def test_partial_usage_reads_the_snapshot_of_a_real_sdk_stream():
    # 실제 BetaMessageStream 을 가짜 raw 스트림으로 만들어 본다: message_start 전에는 스냅샷이 없고(AssertionError) None,
    # message_start 뒤에는 그 시점의 usage 가 partial 로 나온다(output_tokens 는 그 시점 값이라 하한)
    from types import SimpleNamespace

    anthropic = pytest.importorskip("anthropic")
    from anthropic._models import construct_type_unchecked
    from anthropic.lib.streaming._beta_messages import BetaMessageStream
    from anthropic.types.beta import BetaRawMessageStreamEvent

    start = construct_type_unchecked(type_=BetaRawMessageStreamEvent, value={
        "type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "claude-fable-5-1", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
                      "output_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                                             "ephemeral_5m_input_tokens": 30}}}})

    class Raw:
        response = SimpleNamespace(request=SimpleNamespace(headers={}))

        def __iter__(self):
            return iter([start])

        def close(self):
            pass

    st = BetaMessageStream(Raw(), output_format=anthropic.not_given)
    assert scan.partial_usage(st) is None and scan.partial_usage(None) is None     # message_start 전
    next(iter(st))
    assert scan.partial_usage(st) == {
        "input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20, "output_tokens": 1,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30},
        "model": "claude-fable-5-1", "fallback": False, "partial": True}
    # D-25: 요청한 모델과 같으면 fallback False. message_start 가 다른 모델을 담았으면(출력 전 폴백) True
    assert scan.partial_usage(st, "claude-fable-5-1")["fallback"] is False
    assert scan.partial_usage(st, "claude-opus-5-5")["fallback"] is True


def test_classify_errors_raised_while_reading_a_stream():
    # 스트림을 읽는 도중의 오류는 SDK 가 APITimeoutError·APIConnectionError 로 감싸지 않는다: httpx2 예외 그대로, 그리고 200 응답의
    # error 이벤트는 status_code 200 인 APIStatusError 다 (SDK 1.15.0 _streaming.py, _base_client.py 로 확인)
    httpx2 = pytest.importorskip("httpx2")
    anthropic = pytest.importorskip("anthropic")
    req = httpx2.Request("POST", "http://127.0.0.1:1/v1/messages")
    assert scan.classify(httpx2.ReadTimeout("t", request=req)) == "timeout"
    assert scan.classify(httpx2.RemoteProtocolError("peer closed", request=req)) == "connection"
    assert scan.classify(httpx2.ReadError("reset", request=req)) == "connection"
    client = anthropic.Anthropic(api_key="dummy-not-a-real-key")
    resp = httpx2.Response(200, request=req)
    for etype, want in [("overloaded_error", "5xx"), ("api_error", "5xx"), ("rate_limit_error", "429"),
                        ("invalid_request_error", None),
                        # D-25: 파일마다 반복될 오류는 error 이벤트여도 치명
                        ("authentication_error", "fatal"), ("permission_error", "fatal"),
                        ("billing_error", "fatal"), ("not_found_error", "fatal")]:
        body = {"type": "error", "error": {"type": etype, "message": "m"}}
        e = client._make_status_error(str(body), body=body, response=resp)
        assert type(e) is anthropic.APIStatusError and e.status_code == 200
        assert scan.classify(e) == want, etype


def _real_stream(events, boom=None):
    """진짜 BetaMessageStream 을 가짜 raw 스트림으로 만든다. events(raw 이벤트 dict 목록)를 낸 뒤 boom 이 있으면 그 예외를 낸다."""
    from types import SimpleNamespace

    anthropic = pytest.importorskip("anthropic")
    from anthropic._models import construct_type_unchecked
    from anthropic.lib.streaming._beta_messages import BetaMessageStream
    from anthropic.types.beta import BetaRawMessageStreamEvent

    evs = [construct_type_unchecked(type_=BetaRawMessageStreamEvent, value=e) for e in events]

    class Raw:
        response = SimpleNamespace(request=SimpleNamespace(headers={}))

        def __iter__(self):
            yield from evs
            if boom is not None:
                raise boom

        def close(self):
            pass

    return BetaMessageStream(Raw(), output_format=anthropic.not_given)


RAW_START = {"type": "message_start", "message": {
    "id": "m", "type": "message", "role": "assistant", "model": "claude-fable-5-1", "content": [],
    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}
RAW_BODY = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": GOOD}},
            {"type": "content_block_stop", "index": 0}]
RAW_END = [{"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 7}}, {"type": "message_stop"}]


def test_read_stream_returns_a_real_sdk_response_that_was_complete_when_the_connection_closed():
    # D-25 (리뷰 8): message_stop 까지 받은 end_turn 응답은 그 뒤의 읽기 오류(타이머가 닫는 경합, 연결 끊김)로 버리지 않는다.
    # 진짜 SDK 스트림으로 본다: 그 시점의 스냅샷이 최종 응답이다(message_delta 의 stop_reason 과 usage 가 들어 있다)
    httpx2 = pytest.importorskip("httpx2")
    ev = {"first": None, "gap": 0.0, "stop": False}
    m = scan.read_stream(_real_stream([RAW_START, *RAW_BODY, *RAW_END], boom=httpx2.ReadError("closed")), ev)
    assert (m.stop_reason, m.usage.output_tokens, ev["stop"]) == ("end_turn", 7, True)
    assert scan.parse_candidates(m) == CANDS
    assert ev["first"] is not None and ev["gap"] >= 0
    # message_stop 전에 닫히면 예외 그대로, 다 받았지만 end_turn 이 아니어도 그대로(그 파일은 어차피 ERROR 다)
    with pytest.raises(httpx2.ReadError):
        scan.read_stream(_real_stream([RAW_START, *RAW_BODY], boom=httpx2.ReadError("closed")),
                         {"first": None, "gap": 0.0, "stop": False})
    refused = [dict(RAW_END[0], delta={"stop_reason": "refusal", "stop_sequence": None}), RAW_END[1]]
    with pytest.raises(httpx2.ReadError):
        scan.read_stream(_real_stream([RAW_START, *refused], boom=httpx2.ReadError("closed")),
                         {"first": None, "gap": 0.0, "stop": False})


def test_call_timeout_is_set_equal_to_the_adapters_max_call_length():
    # D-25: 무응답 상한(scan.py CALL_TIMEOUT_S)을 최악 호출 길이(어댑터 MAX_CALL_S)와 같게 손으로 맞춘다. 한쪽만 바꾸면 여기서 걸린다
    from adapters.attacktrail import MAX_CALL_S

    assert scan.CALL_TIMEOUT_S == MAX_CALL_S == 1800


def test_max_tokens_constants_and_the_no_data_limit_that_follows_them():
    # D-28: --max-tokens 1..128000 (default 64000). The no-data limit is CALL_TIMEOUT_S up to 64000 tokens and grows in proportion
    # above it: 3600 s at 128000, which covers 128000 tokens at the D-24 pessimistic 36 tok/s (~3,556 s). Measured speed
    # (74 tok/s) needs ~1,730 s. The adapter's max_call_s is the same formula, kept equal by hand (this pins it).
    import math

    from adapters.attacktrail import max_call_s

    assert (scan.MAX_TOKENS, scan.MAX_TOKENS_LIMIT, scan.BASE_MAX_TOKENS) == (64000, 128000, 64000)
    assert scan.EFFORTS == ("high", "xhigh", "max")
    assert scan.call_cap(64000) == scan.call_cap(1) == 1800 and scan.call_cap(128000) == 3600
    assert scan.call_cap(128000) >= math.ceil(128000 / 36) and scan.call_cap(128000) > 128000 / 74
    for n in (1, 16000, 64000, 64001, 100000, 127999, 128000):
        assert scan.call_cap(n) == max_call_s(n)


def test_selftest_prints_the_number_of_checks_it_actually_ran(capsys):
    # D-25 (리뷰 9): 건수는 손으로 쓴 합이 아니라 실제로 실행되어 통과한 검사 수다(chk 가 센다). 소스의 chk() 호출은 각각
    # 한 번 이상 실행되고 반복문 안의 것은 더 실행되니, 찍힌 건수는 소스의 호출 수 이상이다
    import ast
    import inspect
    import re
    import textwrap

    scan.selftest()
    m = re.search(r"selftest OK \((\d+) checks\)", capsys.readouterr().out)
    tree = ast.parse(textwrap.dedent(inspect.getsource(scan.selftest)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "chk"]
    assert m and int(m[1]) >= len(calls) > 30, (m and m[0], len(calls))


def test_selftest_still_checks_under_python_O():
    # D-25 재검증: chk 가 assert 문이면 python -O 가 그것을 지워서 --selftest 가 아무것도 검사하지 않고 "OK" 를 찍었다. 지금은
    # raise 다. 두 가지를 본다. (1) 그대로 -O 로 돌려도 통과하고 건수를 찍는다. (2) msg.sender.call 발췌 항목의 기대값 "OK" 를
    # "EXCERPT_MISMATCH" 로 바꾼 소스 복사본을 -O 로 돌리면 AssertionError 로 실패한다(옛 코드는 여기서 통과했다). -O 에서만 도는 코드라 하위 프로세스로 본다.
    import subprocess
    import sys

    from adapters.attacktrail import SCAN_PY

    p = subprocess.run([sys.executable, "-O", str(SCAN_PY), "--selftest"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "selftest OK (" in p.stdout, p.stdout + p.stderr
    mutant = (
        "import sys\n"
        "src = open(sys.argv[1], encoding='utf-8').read()\n"
        "good = '\"code_excerpt\": \"msg.sender.call\"}, \"OK\"),'\n"
        "bad = src.replace(good, good.replace('\"OK\"', '\"EXCERPT_MISMATCH\"'), 1)\n"
        "if bad == src or sys.flags.optimize != 1:\n"          # assert 문은 -O 에서 지워지니 쓰지 않는다
        "    sys.exit('setup wrong: optimize=%s' % sys.flags.optimize)\n"
        "g = {'__name__': 'scan_mutant', '__file__': sys.argv[1]}\n"
        "exec(compile(bad, 'scan_mutant.py', 'exec'), g)\n"
        "g['selftest']()\n")
    p = subprocess.run([sys.executable, "-O", "-c", mutant, str(SCAN_PY)], capture_output=True, text=True, timeout=120)
    assert p.returncode != 0 and "AssertionError" in p.stderr and "selftest OK" not in p.stdout, p.stdout + p.stderr
