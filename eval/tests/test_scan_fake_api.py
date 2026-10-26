"""scan.py 를 진짜 anthropic SDK 로 돌리되 서버만 가짜로 바꾼 통합 시험 (DECISIONS D-17, D-19, D-23).

127.0.0.1 임의 포트에 가짜 Messages API 를 띄우고 ANTHROPIC_BASE_URL 로 SDK 를 거기 붙인다.
실제 API 는 호출하지 않는다. 키는 더미다. 프록시 환경변수는 죽은 로컬 포트로 막아서,
base URL 이 무시되더라도 바깥으로 나가는 연결은 생길 수 없다.
scan.py 는 beta 엔드포인트(client.beta.messages)를 쓴다. SDK 는 거기에 POST /v1/messages?beta=true 를 보내서
가짜 서버는 쿼리 문자열을 뗀 경로로 라우팅하고, 쿼리는 그대로 기록한다 (D-23).
"""
import _thread
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest

from adapters.attacktrail import (DEADLINE_MARGIN_S, SCAN_PY, SDK_HOME_KEYS, AttackTrailAgent,
                                  fallback_usage, load_scan_module, parse_scan_jsonl, partial_rows, sum_usage)
from adapters.base import OS_ENV_KEYS

anthropic = pytest.importorskip("anthropic")

scan = load_scan_module(SCAN_PY)
# Time limits in the tests below come from the constants, not from literals, so a slow machine or a changed
# constant moves them together (D-21, M-5). The harness kills scan.py at deadline + DEADLINE_MARGIN_S (what the
# adapter leaves between --deadline-s and the harness timeout); the watchdog waits scan.HARD_GRACE_S past the
# deadline for a call the SDK timeout cannot cut. SLACK_S is for process start/teardown on top of that.
HARNESS_S = DEADLINE_MARGIN_S
SLACK_S = 5.0
SOL = ("pragma solidity ^0.8.0;\n"
       "contract V {\n"
       "  function w() external {\n"
       "    (bool ok,) = msg.sender.call{value: 1}('');\n"
       "  }\n"
       "}\n")
CAND = {"title": "reentrancy", "file": "Vault.sol", "line": 4, "code_excerpt": "msg.sender.call",
        "category": "reentrancy", "severity_guess": "High", "why": "w", "poc_sketch": "p"}
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
         "output_tokens": 5,
         "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0}}
THINK = {"type": "thinking", "thinking": "", "signature": "sig"}


MODEL = "claude-fable-5-1"   # scan.py --model 기본값 (D-24 는 Mythos 5.1, D-25 에서 Fable 5.1). 응답의 model 도 같다. 폴백 응답만 아래 FALLBACK_MODEL
# _usage.call 의 키. D-25 에서 first_event_s·max_gap_s 가 늘었다
CALL_KEYS = {"started_at_s", "elapsed_s", "timeout_s", "max_retries", "first_event_s", "max_gap_s"}


def message(content, stop_reason="end_turn", stop_details=None, usage=USAGE, model=MODEL):
    d = {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
         "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}
    if stop_details:
        d["stop_details"] = stop_details
    return d


def ok(cands):
    # 버그 재현: 빈 thinking 블록이 text 앞에 온다
    return message([THINK, {"type": "text", "text": json.dumps({"candidates": cands})}])


def api_error(status, type_, retry=False):
    """오류 응답. 기본은 x-should-retry: false 라 SDK 가 재시도하지 않는다(시험이 빨라진다).
    retry=True 면 헤더 없이 보내 SDK 기본 재시도(429·5xx 는 재시도)를 탄다."""
    headers = {} if retry else {"x-should-retry": "false"}
    return status, {"type": "error", "error": {"type": type_, "message": "fake %s" % type_}}, headers


DRIP = "drip"   # 200 헤더를 보내고 본문을 0.2초마다 한 바이트씩 흘려 끝내지 않는다 (SDK read timeout 이 안 걸린다)


class Script:
    """스트림 응답 대본 (D-24). 단계: ("ev", 이름, 데이터) SSE 이벤트 / ("ping",) / ("sleep", 초) / ("hold",) 끝없이 대기 /
    ("pings",) 끝없이 0.2초마다 ping(이벤트는 없고 바이트만 온다) / ("drop",) 끝 청크 없이 연결을 끊음(RemoteProtocolError) /
    ("end",) 정상 종료(청크 마무리). 대본이 끝나면 정상 종료한다. sleep·hold·pings 는 시험이 끝나면(srv.release) 풀린다."""

    def __init__(self, *steps):
        self.steps = list(steps)


def sse_events(m):
    """메시지 dict m(message()·ok()·fallback_ok() 가 만든 것)를 진짜 API 가 보내는 모양의 SSE 이벤트 목록으로 푼다.
    message_start 는 content 가 비고 stop_reason 이 없는 메시지(usage 의 output_tokens 는 1), 블록마다
    content_block_start/delta/stop, 끝에 message_delta(누적 usage·iterations·stop_reason)와 message_stop 이다.
    fallback 블록은 델타 없이 start/stop 만 온다(SDK 도 그렇게 읽는다)."""
    u = m["usage"]
    start_usage = {k: v for k, v in u.items() if k != "iterations"}
    start_usage["output_tokens"] = 1
    evs = [("ev", "message_start", {"type": "message_start", "message": dict(
        m, content=[], stop_reason=None, stop_details=None, usage=start_usage)})]
    for i, b in enumerate(m["content"]):
        empty = dict(b, **({"text": ""} if b["type"] == "text" else
                           {"thinking": "", "signature": ""} if b["type"] == "thinking" else {}))
        evs.append(("ev", "content_block_start", {"type": "content_block_start", "index": i, "content_block": empty}))
        if b["type"] == "text":
            half = len(b["text"]) // 2   # 두 조각으로 보내서 SDK 의 text 누적을 거친다
            for part in (b["text"][:half], b["text"][half:]):
                evs.append(("ev", "content_block_delta", {"type": "content_block_delta", "index": i,
                                                          "delta": {"type": "text_delta", "text": part}}))
        elif b["type"] == "thinking":
            evs.append(("ev", "content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                "type": "signature_delta", "signature": b["signature"]}}))
        evs.append(("ev", "content_block_stop", {"type": "content_block_stop", "index": i}))
    delta = {"stop_reason": m["stop_reason"], "stop_sequence": None}
    if m.get("stop_details"):
        delta["stop_details"] = m["stop_details"]
    cumulative = {k: u[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                    "output_tokens", "iterations") if k in u}
    evs.append(("ev", "message_delta", {"type": "message_delta", "delta": delta, "usage": cumulative}))
    evs.append(("ev", "message_stop", {"type": "message_stop"}))
    return evs


def cut_after(m, n, *tail):
    """메시지 m 의 SSE 이벤트 앞 n 개만 보내고 tail 단계(("hold",), ("drop",), ("pings",) ...)로 이어지는 대본."""
    return Script(*sse_events(m)[:n], *tail)


# 파일 이름 -> 응답. 요청의 "File: <rel> (" 로 고른다.
REPLIES = {
    "Vault.sol": ok([CAND]),
    "Blocked.sol": message([], "refusal", {"type": "refusal", "category": "cyber", "explanation": None}),
    "Cut.sol": message([THINK], "max_tokens"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        first = body["messages"][0]["content"].split("\n", 1)[0]       # "File: Vault.sol (solidity)"
        name = first.split()[1]
        with srv.lock:
            idx = len(srv.seen)      # 도착 순서. --workers 1 이면 파일 순서와 같다
            # path 는 쿼리 문자열까지 그대로(beta 호출은 /v1/messages?beta=true). 라우팅은 쿼리를 뗀 경로로 한다
            srv.seen.append({"path": self.path, "api_key": self.headers.get("x-api-key"),
                             "body": body, "peer": self.client_address[0], "file": name,
                             "headers": {k.lower(): v for k, v in self.headers.items()}})
            srv.active += 1
            srv.peak = max(srv.peak, srv.active)
        try:
            # 지연(응답 헤더 전). 시험이 끝나면 release 로 깨워 매달린 핸들러 스레드를 정리한다
            srv.release.wait(srv.delays.get(name, 0))
            reply = None
            if urlsplit(self.path).path == "/v1/messages":
                reply = srv.replies(idx, name) if callable(srv.replies) else srv.replies.get(name)
            if reply == DRIP:
                return self.drip()
            if isinstance(reply, Script):
                return self.stream(reply.steps)
            if isinstance(reply, dict) and body.get("stream"):     # scan.py 는 항상 스트리밍이다 (D-24)
                return self.stream([*sse_events(reply), ("end",)])
            # reply: dict(200 본문) / (status, payload) / (status, payload, headers).
            # 429·5xx 는 api_error 가 x-should-retry: false 를 붙여 SDK 재시도를 끈다.
            status, payload, headers = (reply + ({},))[:3] if isinstance(reply, tuple) else (
                (200, reply, {}) if reply else api_error(404, "not_found_error"))
            raw = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)
            except OSError:   # 클라이언트(scan.py)가 먼저 끊었다: 데드라인·종료
                pass
        finally:
            with srv.lock:
                srv.active -= 1

    def stream(self, steps):
        """SSE 응답(HTTP/1.1 chunked). 대본의 단계를 차례로 보낸다. 클라이언트가 먼저 끊으면 조용히 끝낸다."""
        self.protocol_version = "HTTP/1.1"     # send_response 가 이 값으로 상태 줄을 쓴다. chunked 는 1.1 에서만 맞다
        self.close_connection = True
        rel = self.server.release

        def chunk(b):
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
            self.wfile.flush()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for step in steps:
                kind = step[0]
                if kind == "ev":
                    chunk(("event: %s\ndata: %s\n\n" % (step[1], json.dumps(step[2]))).encode())
                elif kind == "ping":
                    chunk(b'event: ping\ndata: {"type": "ping"}\n\n')
                elif kind == "sleep":
                    rel.wait(step[1])
                elif kind == "hold":
                    rel.wait(300)
                    return
                elif kind == "pings":
                    while not rel.is_set():
                        chunk(b'event: ping\ndata: {"type": "ping"}\n\n')
                        rel.wait(0.2)
                    return
                elif kind == "drop":
                    return                      # 끝 청크(0\r\n\r\n) 없이 연결을 닫는다
                elif kind == "end":
                    break
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:   # 클라이언트(scan.py)가 먼저 끊었다: 데드라인·종료
            pass

    def drip(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            end = time.time() + 120
            while time.time() < end and not self.server.release.is_set():
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass

    def log_message(self, *a):
        pass


def dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # 닫은 직후라 아무도 안 듣는다


class W:
    """scan.py 복사본과 대상 저장소를 둔 임시 작업 폴더."""

    def __init__(self, base, names):
        self.base = base
        self.tools, self.docs, self.repo, self.home = (base / d for d in ("tools", "docs", "repo", "home"))
        for d in (self.tools, self.docs, self.repo, self.home):
            d.mkdir()
        # scan.py 는 결과 JSONL 을 (--out 이 없으면) 자기 옆에 쓰고 기준 문서를 ../docs 에서 읽는다.
        # 복사본으로 돌려 레포를 안 더럽힌다.
        shutil.copy(SCAN_PY, self.tools / "scan.py")
        for name in scan.STD_BASE:
            shutil.copy(SCAN_PY.parent.parent / "docs" / name, self.docs / name)
        for name in names:
            (self.repo / name).write_text(SOL, encoding="utf-8")

    def patch_scan(self, **consts):
        """복사본 scan.py 의 모듈 상수를 바꾼다(레포의 scan.py 는 그대로). 시간 상수를 시험 크기로 줄일 때 쓴다."""
        path = self.tools / "scan.py"
        text = path.read_text(encoding="utf-8")
        for name, value in consts.items():
            text, n = re.subn(r"^%s = [^\n#]*" % name, "%s = %r" % (name, value), text, count=1, flags=re.M)
            assert n == 1, name
        path.write_text(text, encoding="utf-8")

    def order(self):
        """scan.py 가 파일을 처리하는 순서 (select_files 결과)."""
        return [os.path.basename(p) for p in scan.select_files(str(self.repo))[0]]

    def default_jsonl(self):
        return list(self.tools.glob("scan-*.jsonl"))


def start_server(replies, delays=None):
    """가짜 Messages API. replies 는 {파일: 응답} 이거나 (도착 순번, 파일) -> 응답 함수."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.replies, srv.delays, srv.seen = replies, delays or {}, []
    srv.lock, srv.active, srv.peak = threading.Lock(), 0, 0
    srv.release = threading.Event()   # 세우면 지연 중인 핸들러가 깨어난다
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def stop_server(srv):
    srv.release.set()
    srv.shutdown()
    srv.server_close()


def scan_env(w, url):
    env = {k: os.environ[k] for k in OS_ENV_KEYS if k in os.environ}
    dead = "http://127.0.0.1:%d" % dead_port()
    # SDK 1.15.0 은 Anthropic() 생성 때 설정 폴더(%APPDATA%\Anthropic)를 읽는다. 홈이 없으면
    # RuntimeError 라서 빈 임시 홈을 준다. API 어댑터와 같은 변수(SDK_HOME_KEYS)만 쓴다.
    env.update({k: str(w.home) for k in SDK_HOME_KEYS})
    env.update(ANTHROPIC_BASE_URL=url, ANTHROPIC_API_KEY="dummy-not-a-real-key",
               PYTHONIOENCODING="utf-8",
               # 로컬은 프록시를 건너뛰고(Windows 는 시스템 프록시 레지스트리도 읽는다), 그 밖은 막힌 포트로 보낸다
               NO_PROXY="127.0.0.1,localhost", HTTP_PROXY=dead, HTTPS_PROXY=dead, ALL_PROXY=dead)
    return env


def run_scan(w, replies, args=(), delays=None, limit=180):
    """가짜 서버를 띄우고 scan.py 를 서브프로세스로 한 번 돌린다. limit 초 안에 안 끝나면 TimeoutExpired
    (하네스가 죽이는 것과 같다. 데드라인 시험은 limit 을 데드라인 가까이로 줘서 "그 전에 끝남"을 확인한다)."""
    srv, url = start_server(replies, delays)
    t = time.monotonic()
    try:
        p = subprocess.run([sys.executable, str(w.tools / "scan.py"), str(w.repo), *args], cwd=str(w.base),
                           env=scan_env(w, url), capture_output=True, text=True, encoding="utf-8",
                           timeout=limit)
    finally:
        stop_server(srv)
    return {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "seen": srv.seen,
            "peak": srv.peak, "url": url, "w": w, "elapsed": time.monotonic() - t}


@pytest.fixture(scope="session")
def import_s():
    """이 환경에서 `import anthropic` 까지 걸리는 초(인터프리터 시작 포함). 하네스 시계와 scan.py 의 예산·
    데드라인 시계는 프로세스 시작부터 돌아서 이 시간이 그 안에 들어간다. 시간 시험의 한도는 이 값에
    여유를 더해 잡아서, 느린 환경에서도 "첫 호출이 시작도 못 하고 끝나는" 일이 없게 한다."""
    t = time.monotonic()
    subprocess.run([sys.executable, "-c", "import anthropic"], check=True, capture_output=True)
    return time.monotonic() - t


def read_rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def candidates(rows):
    return [r for r in rows if "_run" not in r and "_usage" not in r]


def progress(stdout):
    """진행 줄 -> [(i, n, 파일, 나머지)]"""
    return [(int(m[1]), int(m[2]), m[3], m[4]) for m in
            (re.match(r"^  \[(\d+)/(\d+)\] (\S+)  (.*)$", l) for l in stdout.splitlines()) if m]


def assert_progress_matches_fatal_line(row, fatal_line):
    """치명 오류를 만난 파일의 진행 줄(ERROR ...)이 stderr 의 치명 줄과 같은 문구다 (D-25 재검증 3차).
    예전엔 진행 줄이 SSE error 이벤트의 원문 dict(`ERROR {'type': 'error', ...}`)로만 나왔다."""
    want = fatal_line.split("치명적 API 오류로 스캔을 중단했습니다: ", 1)[1]
    assert row[3] == "ERROR " + want, (row[3], want)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    w = W(tmp_path_factory.mktemp("scanrun"), REPLIES)
    r = run_scan(w, REPLIES)
    jsonl = w.default_jsonl()
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(jsonl) == 1, jsonl
    return dict(r, rows=read_rows(jsonl[0]), repo=w.repo)


def line_for(run, name):
    hits = [l for l in run["stdout"].splitlines() if "] " + name + "  " in l]
    assert len(hits) == 1, (name, run["stdout"])
    return hits[0]


def test_thinking_first_response_records_candidate(run):
    assert line_for(run, "Vault.sol").endswith("후보 1")
    cands = candidates(run["rows"])
    assert [(c["file"], c["_status"], c["_verify"]) for c in cands] == [("Vault.sol", "triage", "OK")]


def test_refusal_file_is_error_with_stop_reason_and_category(run):
    assert line_for(run, "Blocked.sol").endswith("ERROR stop_reason=refusal category=cyber")


def test_max_tokens_file_is_error_not_silent(run):
    assert "ERROR stop_reason=max_tokens" in line_for(run, "Cut.sol")


def test_error_in_one_file_does_not_stop_the_others(run):
    # 3개 파일 모두 요청이 갔고, 한 파일의 ERROR 가 나머지 처리를 막지 않았다
    assert len(run["seen"]) == 3
    assert len(re.findall(r"^  \[\d+/\d+\] \S+  ERROR ", run["stdout"], re.M)) == 2


def test_request_body_shape(run):
    assert len(run["seen"]) == 3
    for r in run["seen"]:
        b = r["body"]
        # D-23: beta 엔드포인트. SDK 는 쿼리 문자열 beta=true 를 붙인다
        assert r["path"] == "/v1/messages?beta=true"
        # D-24: 스트리밍 호출, max_tokens 64000, 기본 effort xhigh. 기본 모델은 D-25 부터 Fable 5.1
        assert b["stream"] is True
        assert b["max_tokens"] == 64000
        assert b["model"] == "claude-fable-5-1"
        assert b["fallbacks"] == "default"          # 배열이 아니라 "default" 형태
        assert b["output_config"]["effort"] == "xhigh"
        assert b["output_config"]["format"]["type"] == "json_schema"
        assert b["output_config"]["format"]["schema"]["required"] == ["candidates"]
        role, std = b["system"]
        assert "cache_control" not in role and role["text"].startswith("You are the Step-01")
        assert std["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert "AUD-SEV-01" in std["text"]
        # 안 바꾼 것: thinking 은 보내지 않는다(Fable 5.1·Opus 5.5·Sonnet 5.5 는 항상 켜짐, disabled 는 400), 도구 없음
        assert "thinking" not in b and "tools" not in b and "tool_choice" not in b
        # "default" 형태의 헤더는 -2026-07-01 하나뿐이다. -2026-06-01 은 배열 형태 전용이고 섞으면 400
        assert r["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"


def test_traffic_stayed_on_localhost(run):
    assert run["url"].startswith("http://127.0.0.1:")
    assert {r["peer"] for r in run["seen"]} == {"127.0.0.1"}
    # 보낸 키는 더미뿐이다. 이 시험 환경에는 진짜 키가 없다
    assert {r["api_key"] for r in run["seen"]} == {"dummy-not-a-real-key"}


def test_no_bogus_sdk_precedence_warning(run):
    # D-19: SDK_HOME_KEYS 에 ANTHROPIC_CONFIG_DIR 를 넣으면 SDK 가 "ANTHROPIC_API_KEY is set and takes
    # precedence ... unset" 이라는 잘못된 경고를 매 실행 stderr 에 낸다
    assert "takes precedence" not in run["stderr"], run["stderr"]


def test_default_run_header_records_workers_and_no_budget(run):
    header = run["rows"][0]["_run"]
    assert (header["workers"], header["time_budget"]) == (4, None)


def test_default_run_header_records_model_effort_and_fallbacks(run):
    header = run["rows"][0]["_run"]
    assert (header["model"], header["effort"], header["fallbacks"]) == ("claude-fable-5-1", "xhigh", "default")
    assert (header["max_tokens"], header["stream"]) == (64000, True)


def test_usage_row_per_response_including_failed_parses(run):
    # refusal·max_tokens 파일도 응답을 받았으니 비용이 나갔다. 요청 body 와 달리 usage 는 응답에서 온다.
    usage = {r["_usage"]["file"]: r["_usage"] for r in run["rows"] if "_usage" in r}
    # D-21 (H-3): every row also carries a "call" timing dict (checked in test_usage_row_has_the_call_timing_log)
    # D-23: 모든 행에 응답 모델과 폴백 여부도 있다. 일반 응답(iterations 없음)은 fallback False
    assert {n: {k: v for k, v in u.items() if k != "call"} for n, u in usage.items()} == {
        n: dict(USAGE, file=n, model=MODEL, fallback=False) for n in REPLIES}
    assert all(set(u["call"]) == CALL_KEYS for u in usage.values())
    # _usage 행은 후보가 아니다: 후보 행은 Vault.sol 의 하나뿐
    assert len(candidates(run["rows"])) == 1


def test_bad_candidate_field_types_fail_only_that_file(tmp_path):
    # D-19: line "5"/null/실수/bool, code_excerpt 숫자는 옛 코드에서 verify() TypeError 로 프로세스가 죽었다.
    bad = {"BadStr.sol": ({"line": "5"}, "candidates[0].line 이 정수 아님 (str)"),
           "BadNull.sol": ({"line": None}, "candidates[0].line 이 정수 아님 (NoneType)"),
           "BadFloat.sol": ({"line": 1.5}, "candidates[0].line 이 정수 아님 (float)"),
           "BadBool.sol": ({"line": True}, "candidates[0].line 이 정수 아님 (bool)"),
           "BadExcerpt.sol": ({"code_excerpt": 5}, "candidates[0].code_excerpt 가 문자열 아님 (int)")}
    replies = {"Good1.sol": ok([CAND]), "Good2.sol": ok([CAND]),
               # 좋은 후보가 앞에 있어도 한 후보라도 위반이면 그 파일 전체를 ERROR 로 돌린다
               "Mixed.sol": ok([CAND, dict(CAND, line="5")])}
    replies.update({n: ok([dict(CAND, **patch)]) for n, (patch, _) in bad.items()})
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    by_file = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by_file["Good1.sol"] == by_file["Good2.sol"] == "후보 1"
    for name, (_, want) in bad.items():
        assert by_file[name] == "ERROR " + want, (name, by_file[name])
    assert by_file["Mixed.sol"] == "ERROR candidates[1].line 이 정수 아님 (str)"
    rows = read_rows(w.default_jsonl()[0])
    assert sorted(c["file"] for c in candidates(rows)) == ["Good1.sol", "Good2.sol"]   # 앞선·뒤선 파일 결과 유지
    assert "검토 대기 2 · 위치검증 실패로 폐기 0" in r["stdout"]
    # 응답은 받았으니 usage 는 모든 파일(8개) 몫이 남는다
    assert sorted(x["_usage"]["file"] for x in rows if "_usage" in x) == sorted(replies)


def test_non_fatal_4xx_is_a_per_file_error_not_an_abort(tmp_path):
    # 400 은 파일마다 반복될 오류가 아니다. 즉시 중단 대상은 401·402·403·404 뿐이고, 400 은 연속 8건일 때만 차단기에 닿는다.
    replies = {"A.sol": ok([CAND]), "B.sol": api_error(400, "invalid_request_error"), "C.sol": ok([CAND])}
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 3
    by_file = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by_file["A.sol"] == by_file["C.sol"] == "후보 1" and by_file["B.sol"].startswith("ERROR ")
    assert "400" in by_file["B.sol"] and "치명" not in r["stderr"]


@pytest.mark.parametrize("status, etype, cls", [
    (401, "authentication_error", "AuthenticationError"),
    (402, "billing_error", "APIStatusError"),     # SDK 에 402 전용 클래스가 없다 (D-20). error-codes.md: 재시도 불가
    (403, "permission_error", "PermissionDeniedError"),
    (404, "not_found_error", "NotFoundError"),
])
def test_fatal_api_error_stops_remaining_requests_and_exits_2(tmp_path, status, etype, cls):
    w = W(tmp_path, ["F1.sol", "F2.sol", "F3.sol", "F4.sol"])
    # 첫 요청은 성공, 둘째부터 치명적 오류. --workers 1 이면 도착 순서 = 파일 순서
    r = run_scan(w, lambda i, name: ok([CAND]) if i == 0 else api_error(status, etype),
                 args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 2                      # 셋째·넷째 파일은 요청조차 안 나갔다
    fatal_lines = [l for l in r["stderr"].splitlines() if "치명적" in l]
    assert len(fatal_lines) == 1 and "%s (HTTP %d)" % (cls, status) in fatal_lines[0]
    prog = progress(r["stdout"])
    assert [p[3].split(" ")[0] for p in prog] == ["후보", "ERROR", "ERROR", "ERROR"]
    assert [p[3] for p in prog[2:]] == ["ERROR skipped: aborted after fatal API error"] * 2
    assert_progress_matches_fatal_line(prog[1], fatal_lines[0])
    # 중단돼도 앞선 파일 결과는 남고 파일은 정상으로 닫혔다(요약 출력)
    rows = read_rows(w.default_jsonl()[0])
    assert [c["file"] for c in candidates(rows)] == [prog[0][2]]
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == [prog[0][2]]
    assert "검토 대기 1" in r["stdout"]


def test_fatal_error_with_parallel_workers_starts_no_new_requests(tmp_path):
    names = ["P%d.sol" % i for i in range(8)]
    w = W(tmp_path, names)
    r = run_scan(w, lambda i, name: api_error(401, "authentication_error"), args=["--workers", "4"])
    assert r["rc"] == 2
    # 이미 나간 요청은 workers 개까지다. 나머지 파일은 요청 없이 skipped
    assert 1 <= len(r["seen"]) <= 4 < len(names)
    prog = progress(r["stdout"])
    assert len(prog) == 8
    skipped = [p for p in prog if p[3] == "ERROR skipped: aborted after fatal API error"]
    assert len(skipped) == 8 - len(r["seen"])


def test_parallel_run_keeps_file_order_and_actually_overlaps(tmp_path):
    names = ["O%d.sol" % i for i in range(6)]
    results = {}
    for workers in (1, 4):
        sub = tmp_path / ("w%d" % workers)
        sub.mkdir()
        w = W(sub, names)
        order = w.order()
        # 앞 파일일수록 응답이 늦다: 병렬이면 완료 순서는 파일 순서의 역순에 가깝다
        delays = {order[0]: 1.0, order[1]: 0.5}
        r = run_scan(w, {n: ok([CAND]) for n in names}, args=["--workers", str(workers)], delays=delays)
        assert r["rc"] == 0, r["stdout"] + r["stderr"]
        rows = read_rows(w.default_jsonl()[0])
        results[workers] = (order, r, rows)
        # 진행 줄 번호와 파일 순서
        assert [(p[0], p[2]) for p in progress(r["stdout"])] == list(enumerate(order, 1))
        # JSONL: 헤더 다음에 파일 순서대로 (usage, 후보) 쌍. 행 번호가 finding id 가 되므로 순서가 같아야 한다
        seq = [x["_usage"]["file"] if "_usage" in x else x["file"] for x in rows[1:]]
        assert seq == [n for n in order for _ in (0, 1)]
    assert results[1][1]["peak"] == 1
    assert results[4][1]["peak"] >= 2          # 정말로 겹쳐서 호출했다
    # 순차 실행과 같은 결과: 같은 파일 순서, 같은 행 구성
    def strip(rows):   # 헤더는 빼고, 호출 시간 로그("call", D-21)는 실행마다 달라서 뺀다
        return [{k: ({a: b for a, b in v.items() if a != "call"} if k == "_usage" else v) for k, v in x.items()}
                for x in rows[1:]]
    assert strip(results[1][2]) == strip(results[4][2])


def test_time_budget_skips_remaining_files_and_exits_normally(tmp_path, import_s):
    names = ["T1.sol", "T2.sol", "T3.sol"]
    w = W(tmp_path, names)
    # 예산은 프로세스 시작부터 잰다. 그 안에 `import anthropic` 이 들어가므로(이 환경에서 약 3초, import_s 로
    # 잼) 예산을 import_s + 2.5, 파일당 지연을 5초로 잡는다. 첫 파일이 시작하는 시각 s 가 import_s ± 2.5초
    # 안이면 "첫 파일은 시작하고(s < 예산), 예산을 넘겨서 끝나며(s + 5 > 예산), 둘째 파일은 시작 못 한다".
    # 예전에는 고정 5초 예산이라 import 가 5초를 넘는 환경에서 첫 파일이 시작도 못 해 흔들렸다.
    budget = import_s + 2.5
    r = run_scan(w, {n: ok([CAND]) for n in names}, args=["--workers", "1", "--time-budget", "%.1f" % budget],
                 delays={n: 5.0 for n in names})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]            # 예산 소진은 오류 종료가 아니다
    prog = progress(r["stdout"])
    assert [p[0] for p in prog] == [1, 2, 3]
    done = [p for p in prog if p[3].startswith("후보")]
    skipped = [p for p in prog if p[3] == "ERROR skipped: time budget reached"]
    assert len(done) + len(skipped) == 3 and len(done) >= 1 and len(skipped) >= 1
    assert prog[:len(done)] == done                          # 처리한 파일이 앞, 건너뛴 파일이 뒤(파일 순서 그대로)
    assert len(r["seen"]) == len(done)                       # 건너뛴 파일은 요청이 안 나갔다
    assert re.search(r"^  \[3/3\] \S+  ERROR skipped: time budget reached$", r["stdout"], re.M)
    rows = read_rows(w.default_jsonl()[0])
    assert rows[0]["_run"]["time_budget"] == pytest.approx(budget, abs=0.06)
    assert len([x for x in rows if "_usage" in x]) == len(done)
    assert "검토 대기 %d" % len(done) in r["stdout"]          # 요약까지 출력하고 파일을 닫았다


def test_out_path_is_used_and_nothing_is_written_next_to_scan_py(tmp_path):
    w = W(tmp_path, REPLIES)
    out = tmp_path / "elsewhere" / "result.jsonl"
    out.parent.mkdir()
    r = run_scan(w, REPLIES, args=["--out", str(out)])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert w.default_jsonl() == []                           # scan.py 옆에는 안 썼다
    rows = read_rows(out)
    assert rows[0]["_run"]["target"] == str(w.repo)
    assert [c["file"] for c in candidates(rows)] == ["Vault.sol"]
    assert str(out) in r["stdout"]


def test_jsonl_is_flushed_per_file_so_a_kill_keeps_processed_files(tmp_path):
    # D-19: 파일 하나 처리할 때마다 flush 한다. 하네스가 타임아웃으로 죽여도 처리분이 디스크에 남는다.
    w = W(tmp_path, ["K1.sol", "K2.sol"])
    order = w.order()
    srv, url = start_server({n: ok([CAND]) for n in order}, {order[1]: 120})   # 둘째 파일은 사실상 안 끝난다
    proc = subprocess.Popen([sys.executable, str(w.tools / "scan.py"), str(w.repo), "--workers", "1"],
                            cwd=str(w.base), env=scan_env(w, url),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            jl = w.default_jsonl()
            if jl and '"_status"' in jl[0].read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        else:
            pytest.fail("첫 파일 결과가 JSONL 에 flush 되지 않았다")
        assert proc.poll() is None            # 아직 둘째 파일을 기다리는 중이다
        proc.kill()
        proc.wait(timeout=30)
    finally:
        proc.kill()
        stop_server(srv)
    rows = read_rows(jl[0])
    assert [c["file"] for c in candidates(rows)] == [order[0]]
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == [order[0]]


# ---------------------------------------------------------------------------------------------
# D-20: 하드 데드라인, 연속 오류 차단기, 헤더 해시, 후보 안의 내부 키
# ---------------------------------------------------------------------------------------------

def test_default_run_header_and_sdk_defaults(run):
    header = run["rows"][0]["_run"]
    assert (header["deadline_s"], header["breaker_n"], header["call_timeout_s"]) == (None, 8, 1800)
    # M3: 헤더 prompt_sha256 은 매니페스트의 prompt_sha256(ROLE + "\n\n" + 기준 문서) 앞 16자와 같다
    assert header["prompt_sha256"] == AttackTrailAgent().fingerprints()["prompt_sha256"][:16]
    # --deadline-s 가 없어도 호출마다 read timeout 을 CALL_TIMEOUT_S(1800초)로 명시한다. SDK 기본(600초)이면 생각 중인 호출을 끊는다 (D-25)
    assert {float(r["headers"]["x-stainless-read-timeout"]) for r in run["seen"]} == {1800.0}


def test_git_commit_failure_warns(run):
    # L7: 대상이 git 저장소가 아니면 stderr 에 한 줄 경고(조용히 unknown 으로 넘기지 않는다).
    # 진짜 저장소에서는 경고가 없고 커밋이 기록되는 쪽은 test_candidate_with_internal_key... 가 본다.
    warn = [l for l in run["stderr"].splitlines() if l.startswith("경고: 대상의 git 커밋을 알 수 없어")]
    assert len(warn) == 1 and "commit=unknown" in warn[0]
    assert run["rows"][0]["_run"]["commit"] == "unknown"


def test_hard_deadline_hanging_first_file_does_not_hold_back_finished_files(tmp_path, import_s):
    # 블랙박스 u2. 첫 파일이 매달리고 나머지는 즉시 응답한다. 옛 코드는 파일 순서로 쓰다가 첫 파일에서 막혀
    # 둘째·셋째 결과가 메모리에만 있다 하네스에 죽었다(raw 에 0행). 데드라인이 첫 호출을 끊고, 이미 끝난
    # 결과는 raw 에 남으며, 프로세스는 하네스 한계(데드라인 + 8초) 전에 정상 종료한다.
    w = W(tmp_path, ["U1.sol", "U2.sol", "U3.sol"])
    order = w.order()
    deadline = import_s + 5
    r = run_scan(w, {n: ok([CAND]) for n in order}, args=["--deadline-s", "%.1f" % deadline],
                 delays={order[0]: 600}, limit=deadline + HARNESS_S)   # 넘으면 TimeoutExpired = 하네스가 죽였다
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    prog = progress(r["stdout"])
    assert [p[2] for p in prog] == order
    assert prog[0][3].startswith("ERROR timeout: hard deadline reached (call limited to ")
    assert [p[3] for p in prog[1:]] == ["후보 1", "후보 1"]
    rows = read_rows(w.default_jsonl()[0])
    assert [c["file"] for c in candidates(rows)] == order[1:]                    # 뒤 파일 결과가 raw 에 있다
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == order[1:]
    assert rows[0]["_run"]["deadline_s"] == pytest.approx(deadline, abs=0.06)
    assert "파일 3개: 스캔 2 · 실패 1 · 건너뜀 0" in r["stdout"] and "검토 대기 2" in r["stdout"]
    assert "버리고 종료" not in r["stdout"]          # SDK timeout 이 호출을 끝냈다. 강제 종료 경로가 아니다
    assert r["elapsed"] < deadline + scan.HARD_GRACE_S + SLACK_S   # 데드라인 + 감시 여유 + 종료 여유 안에 끝났다


RETRY_AFTER = (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "fake"}},
               {"retry-after": "600"})   # x-should-retry 없음: SDK 가 429 를 재시도하며 Retry-After 만큼(여기서는 600초) 잔다


def retry_after_scan(tmp_path, names):
    """Retry-After 시험용 작업 폴더. 데드라인이 작으면 call_limits 가 재시도를 0 으로 줄여서 SDK 가 잠들 일이 없다. 복사본의
    시간 상수를 시험 크기로 줄여(재시도 하나당 최소 1초, 백오프 가정 0.5초) 데드라인 import_s + 5 에서도 재시도 2회가 남게 한다."""
    w = W(tmp_path, names)
    w.patch_scan(MIN_RETRY_ATTEMPT_S=1, RETRY_DELAY_S=0.5)
    return w


def test_watchdog_abandons_a_call_stuck_in_a_retry_after_sleep(tmp_path, import_s):
    # 스트림이 열리기 전의 SDK 재시도 대기(Retry-After, 상한 없음. call_limits 도 못 묶는다)에 걸린 호출은 닫을 스트림이 없다.
    # 그래도 main() 이 데드라인 + 2초까지만 기다리고, 끝난 파일의 결과를 쓰고, 안 끝난 호출을 버리고 종료한다.
    # 워커가 데몬이 아니라서 os._exit 가 없으면 종료가 막힌다 (D-20, D-24: 옛 시험은 응답을 흘리는 서버였다. 이제 그건 스트림이라
    # 아래 시험처럼 타이머가 닫는다).
    w = retry_after_scan(tmp_path, ["D1.sol", "D2.sol", "D3.sol"])
    order = w.order()
    deadline = import_s + 5
    replies = {order[0]: RETRY_AFTER, order[1]: ok([CAND]), order[2]: ok([CAND])}
    r = run_scan(w, replies, args=["--deadline-s", "%.1f" % deadline], limit=deadline + HARNESS_S)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    prog = progress(r["stdout"])
    assert prog[0][3] == "ERROR " + scan.STILL_RUNNING
    assert scan.STILL_RUNNING.startswith("timeout: hard deadline reached (call still running)")   # D-20 문구 그대로 + 꼬리
    assert [p[3] for p in prog[1:]] == ["후보 1", "후보 1"]
    # D-21 (M-1): 버린 호출 수와 "토큰 미집계"가 진행 줄과 요약 양쪽에 남는다. 그 호출은 응답을 못 읽어 usage 행이 없다
    assert "토큰 미집계" in prog[0][3]
    assert ("데드라인 뒤에도 끝나지 않은 호출 1개를 버리고 종료합니다. 응답을 못 읽어 그 호출의 토큰은 "
            "집계되지 않았다(usage 과소 집계 가능)") in r["stdout"]
    assert "파일 3개: 스캔 2 · 실패 1 · 건너뜀 0" in r["stdout"]
    rows = read_rows(w.default_jsonl()[0])
    assert [c["file"] for c in candidates(rows)] == order[1:]
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == order[1:]
    # 데드라인 + 여유 뒤에 끝났다(그 전에는 기다렸다). 위쪽 한도는 감시 여유에 종료 여유를 더한 값이다
    assert deadline + scan.HARD_GRACE_S - 0.5 < r["elapsed"] < deadline + scan.HARD_GRACE_S + SLACK_S


def test_hard_deadline_closes_an_open_stream_that_never_starts_the_message(tmp_path, import_s):
    # 헤더는 오고 본문은 공백을 흘리는 서버(message_start 가 안 온다). httpx read timeout 은 바이트마다 리셋돼서 안 걸린다.
    # 열린 스트림이라 데드라인 타이머가 닫는다: 감시가 버리는 경로(위 시험)가 아니다. message_start 가 없어 usage 는 없다.
    w = W(tmp_path, ["D1.sol", "D2.sol", "D3.sol"])
    order = w.order()
    deadline = import_s + 5
    replies = {order[0]: DRIP, order[1]: ok([CAND]), order[2]: ok([CAND])}
    r = run_scan(w, replies, args=["--deadline-s", "%.1f" % deadline], limit=deadline + HARNESS_S)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    prog = progress(r["stdout"])
    assert prog[0][3].startswith("ERROR timeout: hard deadline reached (call limited to ") and "usage 없음" in prog[0][3]
    assert [p[3] for p in prog[1:]] == ["후보 1", "후보 1"]
    assert "버리고 종료" not in r["stdout"] and "토큰 미집계" not in r["stdout"]       # 감시가 아니라 타이머가 끝냈다
    assert "연속" not in r["stderr"]                                                  # 데드라인이 끊은 호출은 차단기에 안 센다
    rows = read_rows(w.default_jsonl()[0])
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == order[1:]
    assert r["elapsed"] < deadline + scan.HARD_GRACE_S + SLACK_S


def test_only_calls_that_were_running_count_as_abandoned_not_queued_files(tmp_path, import_s):
    # D-21 (L-1): 워커 2개가 둘 다 매달리면 나머지 4개 파일은 시작도 못 한 채 대기열에 있다. 그것들은 요청이 안 나갔으니
    # "버린 호출"이 아니다. 버린 호출은 실행 중이던 둘뿐이고, 대기열 파일은 skipped 로 집계된다.
    w = retry_after_scan(tmp_path, ["Q%d.sol" % i for i in range(6)])
    order = w.order()
    deadline = import_s + 5
    replies = {n: RETRY_AFTER if i < 2 else ok([CAND]) for i, n in enumerate(order)}
    r = run_scan(w, replies, args=["--workers", "2", "--deadline-s", "%.1f" % deadline], limit=deadline + HARNESS_S)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    got = [p[3] for p in progress(r["stdout"])]
    assert got == ["ERROR " + scan.STILL_RUNNING] * 2 + ["ERROR skipped: hard deadline reached"] * 4
    assert "데드라인 뒤에도 끝나지 않은 호출 2개를 버리고 종료합니다" in r["stdout"]
    assert "파일 6개: 스캔 0 · 실패 2 · 건너뜀 4" in r["stdout"]
    assert len(r["seen"]) == 2                               # 대기열 파일 4개는 요청이 안 나갔다


def test_hard_deadline_already_passed_sends_no_request(tmp_path):
    w = W(tmp_path, ["E1.sol", "E2.sol"])
    r = run_scan(w, {"E1.sol": ok([CAND]), "E2.sol": ok([CAND])}, args=["--deadline-s", "0.5"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert r["seen"] == []                         # 남은 시간 1초 미만이면 요청 없이 skipped
    assert [p[3] for p in progress(r["stdout"])] == ["ERROR skipped: hard deadline reached"] * 2
    assert "파일 2개: 스캔 0 · 실패 0 · 건너뜀 2" in r["stdout"]
    assert read_rows(w.default_jsonl()[0])[0]["_run"]["deadline_s"] == 0.5


def test_deadline_must_be_positive(tmp_path):
    w = W(tmp_path, ["V1.sol"])
    r = subprocess.run([sys.executable, str(w.tools / "scan.py"), str(w.repo), "--deadline-s", "0"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2 and "--deadline-s 는 0 보다 커야 합니다" in r.stderr


def test_deadline_sizes_each_calls_timeout_and_retries(tmp_path, import_s):
    # 항상 500(재시도 대상, x-should-retry 없음)인 파일 하나. 남은 시간이 작으면 재시도 0, read timeout 도 남은
    # 시간으로 줄어든다. 시간이 넉넉하면 재시도 2회(백오프 0.5·1초)에 CALL_TIMEOUT_S(1800초)다.
    # 1800 이 걸리려면 남은 시간이 (1800 × 3 + 16)초 이상이라 데드라인을 20000초로 준다.
    always500 = lambda i, name: api_error(500, "api_error", retry=True)
    small = tmp_path / "small"
    small.mkdir()
    r = run_scan(W(small, ["R1.sol"]), always500, args=["--deadline-s", "%.1f" % (import_s + 30)])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 1                                   # max_retries=0
    # 남은 시간(데드라인 import_s + 30 에서 scan.py 가 호출 전까지 쓴 시간을 뺀 값)이 read timeout 이다. 600 이 아니다
    assert 20 < float(r["seen"][0]["headers"]["x-stainless-read-timeout"]) <= import_s + 30
    assert r["seen"][0]["headers"]["x-stainless-retry-count"] == "0"
    assert "takes precedence" not in r["stderr"]                 # with_options 복사본도 SDK 경고를 내지 않는다
    big = tmp_path / "big"
    big.mkdir()
    r = run_scan(W(big, ["R1.sol"]), always500, args=["--deadline-s", "20000"])
    assert [x["headers"]["x-stainless-retry-count"] for x in r["seen"]] == ["0", "1", "2"]
    assert {float(x["headers"]["x-stainless-read-timeout"]) for x in r["seen"]} == {1800.0}


ERRS = {"400": (400, "invalid_request_error"), "413": (413, "request_too_large"),
        "429": (429, "rate_limit_error"), "500": (500, "api_error"), "529": (529, "overloaded_error")}
REFUSAL = message([], "refusal", {"type": "refusal", "category": "cyber", "explanation": None})


def chain(seq):
    """도착 순번 i 의 응답을 seq[i] 로 정하는 replies 함수. --workers 1 이면 도착 순서 = 파일 순서다."""
    def reply(i, name):
        k = seq[i]
        return ok([CAND]) if k == "ok" else REFUSAL if k == "refusal" else api_error(*ERRS[k])
    return reply


def test_api_errors_stay_per_file_and_a_response_resets_the_streak(tmp_path):
    # M8: "429 를 치명으로 바꾸는 변이"가 잡히게 429·5xx(500, 529)·400·413 이 각각 파일 하나의 ERROR 로 끝나고 다음
    # 파일 호출이 계속 나가는지 본다. 뒤쪽 두 묶음은 연속 7건에서 응답(refusal, 정상)이 끼면 카운터가 리셋돼
    # 차단기(8)에 닿지 않는 것을 본다. 413 은 파일 하나의 문제라 중립이다.
    seq = (["ok", "400", "ok", "429", "ok", "500", "ok", "529", "413", "ok"]
           + ["429"] * 7 + ["refusal"] + ["500"] * 7 + ["ok"])
    w = W(tmp_path, ["C%02d.sol" % i for i in range(len(seq))])
    r = run_scan(w, chain(seq), args=["--workers", "1"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == len(seq)                        # 어떤 오류도 요청을 멈추지 않았다
    assert "치명" not in r["stderr"] and "연속" not in r["stderr"]
    prog = progress(r["stdout"])
    assert not [p for p in prog if p[3].startswith("ERROR skipped")]
    for want, p in zip(seq, prog):
        assert (want == "ok") == p[3].startswith("후보"), (want, p)
        if want in ERRS:
            assert "Error code: %s" % want in p[3], (want, p)
        if want == "refusal":
            assert "stop_reason=refusal" in p[3]
    assert "파일 %d개: 스캔 %d" % (len(seq), seq.count("ok")) in r["stdout"]


def test_breaker_stops_after_8_consecutive_errors_of_any_counted_kind(tmp_path):
    # 429, 5xx, 400 이 섞여 연속 8건이면(완료 순서 기준) 새 호출을 멈추고 exit 2. 413 은 중립이라 센 것에 안 들어간다
    # (413 을 세면 한 파일 일찍 멈추고, 413 이 카운터를 리셋하면 멈추지 않는다).
    seq = ["ok", "429", "500", "413", "400", "429", "529", "400", "500", "429"] + ["ok"] * 4
    w = W(tmp_path, ["B%02d.sol" % i for i in range(len(seq))])
    order = w.order()
    r = run_scan(w, chain(seq), args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 10                              # 8번째 센 오류(열 번째 파일)에서 멈췄다
    prog = progress(r["stdout"])
    why = "aborted after 8 consecutive API errors (429,5xx,400)"
    assert [p[3] for p in prog[10:]] == ["ERROR skipped: " + why] * 4
    assert [p[2] for p in prog] == order
    err = [l for l in r["stderr"].splitlines() if "연속 API 오류" in l]
    assert len(err) == 1 and "연속 API 오류로 스캔을 중단했습니다: " + why in err[0] and "치명" not in r["stderr"]
    rows = read_rows(w.default_jsonl()[0])
    assert [c["file"] for c in candidates(rows)] == [order[0]]   # 앞선 결과는 남는다
    assert "파일 14개: 스캔 1 · 실패 9 · 건너뜀 4" in r["stdout"]


def test_breaker_with_parallel_workers_counts_in_completion_order(tmp_path):
    w = W(tmp_path, ["Q%02d.sol" % i for i in range(20)])
    r = run_scan(w, lambda i, name: api_error(429, "rate_limit_error"), args=["--workers", "4"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    # 8건째 완료에서 멈춘다. 그 순간 나가 있던 호출은 workers - 1 = 3 개까지다
    assert 8 <= len(r["seen"]) <= 11
    why = "ERROR skipped: aborted after 8 consecutive API errors (429)"
    assert len([p for p in progress(r["stdout"]) if p[3] == why]) == 20 - len(r["seen"])


def test_candidate_with_internal_key_is_an_error_and_never_counts_as_usage(tmp_path):
    # 블랙박스 ad. 후보 안의 "_usage" 키가 어댑터 합산에 들어가 토큰이 100만 부풀었다(기대 (20,10)). 이제 스키마 밖
    # 키는 그 파일의 ERROR 이고, 어댑터는 키가 정확히 "_usage" 하나인 행만 usage 로 읽는다.
    evil = dict(CAND, _usage={"input_tokens": 10 ** 6, "output_tokens": 10 ** 6})
    w = W(tmp_path, ["AD1.sol", "AD2.sol"])
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(w.repo),
                                    check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")                      # 진짜 저장소: L7 경고가 없고 헤더에 커밋이 기록된다
    git("add", ".")
    git("commit", "-q", "-m", "c")
    r = run_scan(w, {"AD1.sol": ok([evil]), "AD2.sol": ok([CAND])})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert "경고" not in r["stderr"]
    assert read_rows(w.default_jsonl()[0])[0]["_run"]["commit"] == git("rev-parse", "HEAD")
    by = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by["AD1.sol"] == "ERROR candidates[0] 에 스키마 밖 키: '_usage'" and by["AD2.sol"] == "후보 1"
    jsonl = w.default_jsonl()[0]
    rows = read_rows(jsonl)
    assert [c["file"] for c in candidates(rows)] == ["AD2.sol"]
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == ["AD1.sol", "AD2.sol"]   # 응답은 받았으니 둘 다
    # USAGE 한 건 = 입력 10 + 30 + 20, 출력 5. 파일 둘이라 두 배
    assert sum_usage(jsonl) == (120, 10)
    assert [f.file for f in parse_scan_jsonl(jsonl)[1]] == ["AD2.sol"]


def test_ctrl_c_does_not_wait_for_calls_in_flight(tmp_path, monkeypatch):
    # M7: 옛 코드는 with ThreadPoolExecutor 가 shutdown(wait=True) 라 Ctrl-C 뒤에도 진행 중 호출이 끝날 때까지
    # 붙잡혔다(그리고 Windows 는 무기한 future.result() 가 Ctrl-C 로 안 깨어난다: wait_result 의 이유).
    # 가짜 클라이언트의 호출이 20초 매달려 있어도 main() 은 인터럽트 뒤 곧바로 KeyboardInterrupt 로 나온다.
    # 한계: 이 시험은 main() 이 KeyboardInterrupt 를 내보내는 데까지만 본다. 그 뒤 인터프리터 종료가 워커 스레드를
    # join 해서 프로세스가 호출이 끝날 때까지 사는 것(D-21 H-1)은 같은 프로세스 안에서는 안 보인다. 그건 아래
    # test_ctrl_c_ends_the_scan_process_without_waiting_for_calls 가 자식 프로세스로 본다.
    release, calls = threading.Event(), []

    class Hang:
        def __init__(self, *a, **k):
            self.beta = self.messages = self      # scan.py 는 client.beta.messages.stream 을 with 로 연다 (D-24)

        def with_options(self, **kw):
            return self

        def stream(self, **kw):
            return self

        def __enter__(self):
            calls.append(1)
            release.wait(20)
            raise RuntimeError("released")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(anthropic, "Anthropic", Hang)
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(6):
        (repo / ("H%d.sol" % i)).write_text(SOL, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["scan.py", str(repo), "--out", str(tmp_path / "o.jsonl"), "--workers", "2"])
    timer = threading.Timer(1.0, _thread.interrupt_main)      # Ctrl-C 와 같은 KeyboardInterrupt 를 메인 스레드에
    timer.start()
    t = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            scan.main()
        waited = time.monotonic() - t
    finally:
        timer.cancel()
        release.set()
    assert waited < 6, waited
    time.sleep(1.0)                                           # 풀린 워커가 큐에서 더 꺼내지 않는다
    assert len(calls) == 2                                    # workers=2: 진행 중이던 둘뿐


def test_calls_cut_by_the_deadline_are_not_counted_as_an_api_outage(tmp_path, import_s):
    # 데드라인이 끊은 호출 8건이 한꺼번에 timeout 오류로 끝나도 연속 API 오류 차단기(8)에 세지 않는다. 세면 끝에서
    # exit 2 가 되고, 어댑터는 시간 한도로 부분만 스캔한 정상 실행을 crashed 로 버린다.
    w = W(tmp_path, ["K%02d.sol" % i for i in range(10)])
    order = w.order()
    deadline = import_s + 5
    r = run_scan(w, {n: ok([CAND]) for n in order}, args=["--workers", "8", "--deadline-s", "%.1f" % deadline],
                 delays={n: 600 for n in order[:8]}, limit=deadline + HARNESS_S)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert "연속" not in r["stderr"] and "치명" not in r["stderr"]
    got = [p[3] for p in progress(r["stdout"])]
    assert all(t.startswith("ERROR timeout: hard deadline reached (call limited to ") for t in got[:8]), got
    assert got[8:] == ["ERROR skipped: hard deadline reached"] * 2        # 데드라인 뒤에는 요청이 안 나간다
    assert "파일 10개: 스캔 0 · 실패 8 · 건너뜀 2" in r["stdout"]
    assert len(r["seen"]) == 8


# ---------------------------------------------------------------------------------------------
# D-21: Ctrl-C 가 프로세스를 끝낸다 (H-1), 호출 시간 로그 (H-3)
# ---------------------------------------------------------------------------------------------

CTRL_C_S = 5.0   # 인터럽트를 받은 뒤 프로세스가 끝나기까지. 결과 대기 폴링이 0.5초(scan.POLL_S)이고 나머지는 느린 머신 여유다

# scan.py 를 __main__ 으로 돌리는 자식 프로세스의 진입점. runpy 라서 `if __name__ == "__main__":` 블록(KeyboardInterrupt 를
# 잡아 os._exit 하는 곳)이 그대로 실행된다. SIGBREAK(Windows 의 CTRL_BREAK_EVENT)를 KeyboardInterrupt 로 바꿔서 OS 가
# 보낸 신호가 Ctrl-C 와 같은 경로로 들어오게 하고, 환경변수로 파일 경로를 받으면 그 파일이 생길 때 같은 인터럽트를
# 스스로 넣는다(콘솔이 없는 환경에서도 도는 쪽).
CTRL_C_DRIVER = """
import _thread, os, runpy, signal, sys, threading, time
signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), signal.default_int_handler)
flag = os.environ.pop("AT_TEST_INTERRUPT_FLAG", "")
if flag:
    def watch():
        while not os.path.exists(flag):
            time.sleep(0.02)
        _thread.interrupt_main()
    threading.Thread(target=watch, daemon=True).start()
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.parametrize("where", ["before_headers", "mid_stream"])
@pytest.mark.parametrize("how", ["flag", "signal"])
def test_ctrl_c_ends_the_scan_process_without_waiting_for_calls(tmp_path, how, where):
    # D-21 (H-1): main() 이 KeyboardInterrupt 를 내보낸 뒤 인터프리터 종료가 워커 스레드를 join 해서, 프로세스가 진행 중
    # 호출이 끝날 때까지(최대 수백 초) 살아 있었다(3차 재리뷰: 인터럽트 뒤 54초). 위의 같은 프로세스 시험은 이걸 못 본다.
    # 자식 프로세스로 돌려 인터럽트를 보내고, 5초 안에 exit 130 으로 끝나는지, 새 호출이 없는지, 결과 파일이 온전한지 본다.
    #   flag:   자식이 플래그 파일을 보고 스스로 KeyboardInterrupt 를 넣는다(어느 환경에서나 돈다).
    #   signal: 진짜 OS 신호. Windows 는 CREATE_NEW_PROCESS_GROUP 자식에 CTRL_BREAK_EVENT 를 보낸다(자식이 SIGBREAK 를
    #           KeyboardInterrupt 로 바꿈. 이 머신에서 도달을 확인했다). 콘솔이 없는 환경이면 skip.
    # D-24: where 는 붙잡힌 호출이 어디에 있는가. before_headers 는 응답 헤더 전(스트림이 열리기 전), mid_stream 은 스트림이
    # 열려 message_start 를 받은 뒤 서버가 ping 만 보내는 상태다. 둘 다 5초 안에 exit 130 이어야 한다.
    w = W(tmp_path, ["C%d.sol" % i for i in range(6)])
    order = w.order()
    out, flag, win = tmp_path / "o.jsonl", tmp_path / "interrupt", sys.platform == "win32"
    # 첫 파일은 즉시, 나머지는 붙잡는다. workers 2: 요청은 3개(첫 파일, 둘째, 첫 워커가 이어 받은 셋째)가 나가고,
    # 메인은 첫 파일 결과를 쓴 뒤 둘째 파일을 기다린다
    if where == "mid_stream":
        replies = {n: cut_after(ok([CAND]), 2, ("pings",)) for n in order}
        replies[order[0]] = ok([CAND])
        srv, url = start_server(replies)
    else:
        srv, url = start_server({n: ok([CAND]) for n in order}, {n: 60 for n in order[1:]})
    env = scan_env(w, url)
    if how == "flag":
        env["AT_TEST_INTERRUPT_FLAG"] = str(flag)
    p = subprocess.Popen([sys.executable, "-c", CTRL_C_DRIVER, str(w.tools / "scan.py"), str(w.repo),
                          "--out", str(out), "--workers", "2"],
                         cwd=str(w.base), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if win else 0)
    try:
        end = time.monotonic() + 90
        while time.monotonic() < end:
            if len(srv.seen) == 3 and out.is_file() and '"_status"' in out.read_text(encoding="utf-8"):
                break
            assert p.poll() is None, p.communicate()
            time.sleep(0.1)
        else:
            pytest.fail("호출 3개가 나가고 첫 파일 결과가 쓰이기 전에 시간이 끝났다: 요청 %d개" % len(srv.seen))
        sent = time.monotonic()
        if how == "flag":
            flag.write_text("go", encoding="utf-8")
        else:
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT if win else signal.SIGINT)
            except OSError as e:   # 콘솔이 없는 실행 환경(서비스, 일부 CI)
                pytest.skip("OS 신호를 보낼 수 없다: %s" % e)
        try:
            p.wait(timeout=CTRL_C_S + 20)     # 옛 동작은 서버가 붙잡는 60초 동안 산다. 시험이 그만큼 기다리진 않는다
        except subprocess.TimeoutExpired:
            pytest.fail("인터럽트 후 %.0f초가 지나도 프로세스가 끝나지 않았다(진행 중 호출을 기다리는 중)" % (CTRL_C_S + 20))
        took = time.monotonic() - sent
        _, stderr = p.communicate()
    finally:
        if p.poll() is None:
            p.kill()
        stop_server(srv)
    assert took < CTRL_C_S, took
    assert p.returncode == 130, (p.returncode, stderr)
    assert "중단됨(Ctrl-C)" in stderr
    time.sleep(1.0)
    assert len(srv.seen) == 3                  # 인터럽트 뒤 새 호출이 없다(대기열 파일 셋은 요청이 안 나갔다)
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")                 # 마지막 행이 잘리지 않았다
    rows = read_rows(out)                      # 모든 행이 온전한 JSON 이다. 처리한 첫 파일의 결과는 남았다
    assert [c["file"] for c in candidates(rows)] == [order[0]]
    assert [x["_usage"]["file"] for x in rows if "_usage" in x] == [order[0]]


@pytest.mark.parametrize("extra", [[], ["--deadline-s"]])
def test_usage_row_has_the_call_timing_log(tmp_path, import_s, extra):
    # D-21 (H-3): 파일럿에서 호출별 timeout 설계를 정하려고 _usage 행에 "call" 을 남긴다. started_at_s 는 main() 진입
    # 기준 초, elapsed_s 는 호출(SDK 재시도 포함)이 걸린 초, timeout_s·max_retries 는 그 호출에 준 값이다. 실제 재시도
    # 횟수는 없다(messages.create 반환값에 없고, with_raw_response 는 요청 헤더가 늘어 호출 형태가 바뀐다).
    # --deadline-s 가 없으면 CALL_TIMEOUT_S(1800초)와 재시도 2회이고, 있으면 남은 시간으로 줄어든 값이다 (D-25).
    # D-25: first_event_s(스트림이 열린 뒤 첫 이벤트까지)와 max_gap_s(이벤트 사이 최대 간격)도 남는다. 둘 다 호출 시간 안의 값이다.
    w = W(tmp_path, ["L1.sol", "L2.sol"])
    order = w.order()
    args = ["--workers", "1"] + (extra + ["%.1f" % (import_s + 30)] if extra else [])
    r = run_scan(w, {n: ok([CAND]) for n in order}, args=args, delays={order[0]: 1.0})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    calls = [x["_usage"]["call"] for x in read_rows(w.default_jsonl()[0]) if "_usage" in x]
    assert len(calls) == 2 and all(set(c) == CALL_KEYS for c in calls)
    for c in calls:
        assert c["first_event_s"] is not None and 0 <= c["first_event_s"] <= c["elapsed_s"]
        assert 0 <= c["max_gap_s"] <= c["elapsed_s"]
    first, second = calls
    assert first["elapsed_s"] >= 0.9                           # 서버가 1초 붙잡았다
    assert 0 <= first["started_at_s"] and first["elapsed_s"] < 1.0 + SLACK_S
    assert second["started_at_s"] >= first["started_at_s"] + first["elapsed_s"] - 0.05   # workers 1: 순차
    assert second["started_at_s"] + second["elapsed_s"] < r["elapsed"]                    # main 기준 오프셋은 프로세스 시간 안
    for c, seen in zip(calls, r["seen"]):                       # 로그의 timeout 은 SDK 가 실제로 보낸 값과 같다
        assert c["timeout_s"] == pytest.approx(float(seen["headers"]["x-stainless-read-timeout"]), abs=0.001)
    if extra:
        assert [c["max_retries"] for c in calls] == [0, 0]     # 남은 시간이 작아 재시도를 포기했다
        assert 20 < first["timeout_s"] <= import_s + 30
    else:
        assert [(c["timeout_s"], c["max_retries"]) for c in calls] == [(1800.0, 2)] * 2
    # 합산은 이 필드 때문에 달라지지 않는다: 호출마다 USAGE 한 건 = 입력 60, 출력 5
    assert sum_usage(w.default_jsonl()[0]) == (120, 10)


# ---------------------------------------------------------------------------------------------
# D-23: --effort, 서버 측 폴백 응답, beta 엔드포인트 400 (모델 기본값은 D-24 에서 Mythos 5.1, D-25 에서 Fable 5.1 로)
# ---------------------------------------------------------------------------------------------

FALLBACK_MODEL = "claude-opus-5"   # Fable 5.1(= Mythos 5.1)의 폴백 대상은 claude-opus-4-8, claude-opus-5
FB_BLOCK = {"type": "fallback", "from": {"model": MODEL}, "to": {"model": FALLBACK_MODEL},
            "trigger": {"type": "refusal", "category": "cyber"}}
# 시도 둘. Sonnet 5.5 가 거절(입력 100, 출력 0), Sonnet 5 가 받아서 응답(fallback_message). 최상위 usage 는 응답을 낸 시도 하나
REFUSED_IT = {"type": "message", "model": MODEL, "input_tokens": 100, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
SERVED_IT = {"type": "fallback_message", "model": FALLBACK_MODEL, "input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20,
             "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30}}
FB_USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 20, "output_tokens": 5,
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 30},
            "iterations": [REFUSED_IT, SERVED_IT]}


def fallback_ok(cands):
    """폴백 모델이 응답한 정상 응답: fallback 블록이 text 앞에 오고, 최상위 model 은 폴백 모델이다."""
    body = {"type": "text", "text": json.dumps({"candidates": cands})}
    return message([FB_BLOCK, THINK, body], usage=FB_USAGE, model=FALLBACK_MODEL)


def chain_refusal():
    """폴백 모델까지 거절했다: 최종 stop_reason 이 refusal 이다 (체인 전체 거절)."""
    return message([], "refusal", {"type": "refusal", "category": "cyber", "explanation": None},
                   usage=FB_USAGE, model=FALLBACK_MODEL)


def test_fallback_response_is_read_and_recorded_per_file(tmp_path):
    replies = {"Plain.sol": ok([CAND]), "Fell.sol": fallback_ok([CAND]), "Both.sol": chain_refusal()}
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    by = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by["Plain.sol"] == by["Fell.sol"] == "후보 1"            # fallback 블록과 빈 thinking 이 앞에 있어도 후보를 읽는다
    assert by["Both.sol"] == "ERROR stop_reason=refusal category=cyber"   # 체인 전체 거절은 기존 refusal 처리
    rows = read_rows(w.default_jsonl()[0])
    assert {c["file"] for c in candidates(rows)} == {"Plain.sol", "Fell.sol"}
    usage = {x["_usage"]["file"]: x["_usage"] for x in rows if "_usage" in x}
    assert sorted(usage) == sorted(replies)                           # 거절한 파일도 과금됐으니 usage 가 남는다
    strip = lambda u: {k: v for k, v in u.items() if k not in ("call", "file")}
    assert strip(usage["Plain.sol"]) == dict(USAGE, model=MODEL, fallback=False)
    fell = strip(usage["Fell.sol"])
    assert (fell["model"], fell["fallback"]) == (FALLBACK_MODEL, True)
    assert fell["returned"] == {k: FB_USAGE[k] for k in scan.USAGE_KEYS}
    assert [(a["type"], a["model"]) for a in fell["attempts"]] == [("message", MODEL),
                                                                    ("fallback_message", FALLBACK_MODEL)]
    # 응답을 못 낸 체인 전체 거절은 fallback False. 그래도 두 시도의 토큰은 합산돼 있다
    both = strip(usage["Both.sol"])
    assert both["fallback"] is False and both["model"] == FALLBACK_MODEL and both["input_tokens"] == 110
    # 합산은 시도별 합계를 쓴다: 일반 60+5, 폴백 둘은 (110+30+20)+5. 최상위 값만 썼다면 입력이 (60, 60, 60)=180 이다
    assert sum_usage(w.default_jsonl()[0]) == (60 + 160 + 160, 15)
    assert fallback_usage(w.default_jsonl()[0]) == (1, [FALLBACK_MODEL])
    assert {x["body"]["fallbacks"] for x in r["seen"]} == {"default"}


@pytest.mark.parametrize("model, effort", [("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "xhigh")])
def test_other_models_and_effort_still_run_with_the_same_call_shape(tmp_path, model, effort):
    # D-24: 기본은 xhigh 로 올렸고(모델은 D-25 부터 Fable 5.1), 이전 기본(Sonnet 5.5 + high)과 Opus 5.5 는 플래그로 계속 돈다.
    # 요청 모양은 모델·effort 말고는 같다: 스트리밍, max_tokens, 폴백·베타·TTL. thinking 은 어느 모델에도 안 보낸다
    w = W(tmp_path, ["X1.sol", "X2.sol"])
    r = run_scan(w, {"X1.sol": ok([CAND]), "X2.sol": ok([CAND])}, args=["--model", model, "--effort", effort])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 2
    assert {(x["body"]["model"], x["body"]["output_config"]["effort"]) for x in r["seen"]} == {(model, effort)}
    header = read_rows(w.default_jsonl()[0])[0]["_run"]
    assert (header["effort"], header["model"]) == (effort, model)
    assert {(x["body"]["stream"], x["body"]["max_tokens"]) for x in r["seen"]} == {(True, 64000)}
    assert {x["body"]["fallbacks"] for x in r["seen"]} == {"default"}
    assert {x["headers"]["anthropic-beta"] for x in r["seen"]} == {"server-side-fallback-2026-07-01"}
    assert {x["body"]["system"][1]["cache_control"]["ttl"] for x in r["seen"]} == {"5m"}
    assert not [x for x in r["seen"] if "thinking" in x["body"]]


@pytest.mark.parametrize("bad", ["medium", "low", "HIGH", "maximum", ""])
def test_effort_accepts_only_high_xhigh_and_max(tmp_path, bad):
    w = W(tmp_path, ["V1.sol"])
    r = subprocess.run([sys.executable, str(w.tools / "scan.py"), str(w.repo), "--effort", bad],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2 and "--effort" in r.stderr and "invalid choice" in r.stderr   # 요청은 나가지 않는다


def test_model_flag_overrides_the_default_and_xhigh_stays_the_effort_default(tmp_path):
    w = W(tmp_path, ["M1.sol"])
    r = run_scan(w, {"M1.sol": ok([CAND])}, args=["--model", "claude-opus-5"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert {(x["body"]["model"], x["body"]["output_config"]["effort"]) for x in r["seen"]} == {("claude-opus-5", "xhigh")}


def test_beta_rejection_400_is_classified_like_any_400_not_fatal(tmp_path):
    # 폴백·베타를 허용하지 않는 계정이면 beta 엔드포인트가 모든 요청에 400 을 줄 수 있다. 기존 분류대로 400 은 치명(401~404)이
    # 아니라 파일별 ERROR 이고, 연속 8건이면 차단기가 멈춘다(exit 2). 치명으로 바꾸면 첫 요청에서 멈춰 요청 수가 1 이 된다.
    def reject(i, name):
        return (400, {"type": "error", "error": {"type": "invalid_request_error",
                                                  "message": "fallbacks: not supported for this organization"}},
                {"x-should-retry": "false"})
    w = W(tmp_path, ["R%02d.sol" % i for i in range(12)])
    r = run_scan(w, reject, args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 8                                        # 8번째 연속 400 에서 멈췄다
    assert all(x["path"] == "/v1/messages?beta=true" for x in r["seen"])
    err = [l for l in r["stderr"].splitlines() if "연속 API 오류" in l]
    assert len(err) == 1 and "aborted after 8 consecutive API errors (400)" in err[0] and "치명" not in r["stderr"]
    first = progress(r["stdout"])[0][3]
    assert first.startswith("ERROR ") and "fallbacks: not supported" in first


# ---------------------------------------------------------------------------------------------
# D-24: 스트리밍 호출. 서버 측 폴백 두 시나리오, 스트림 도중 끊김·멈춤·error 이벤트, 데드라인 타이머
# ---------------------------------------------------------------------------------------------

def usage_rows(rows):
    return {x["_usage"]["file"]: x["_usage"] for x in rows if "_usage" in x}


def mid_stream_fallback(cands, restart=False):
    """출력 도중 거절(mid-stream) 응답. 요청한 모델(MODEL)이 thinking 과 text 앞부분을 쓰다 거절당하고, 경계에 fallback 블록이 오고,
    폴백 모델(FALLBACK_MODEL)이 이어 쓴다(restart=False). 문서(model-migration.md)는 부분 출력의 text 블록만 폴백 모델에 이어쓰기
    문맥으로 간다고 적는다. restart=True 는 폴백 모델이 이어쓰지 않고 처음부터 다시 쓴 경우다.
    시도 둘: 거절된 시도(입력 100, 출력 40: 부분 출력은 정상 요율로 과금된다)와 응답한 시도(SERVED_IT)."""
    text = json.dumps({"candidates": cands})
    cut = len(text) // 2
    declined = {"type": "message", "model": MODEL, "input_tokens": 100, "output_tokens": 40,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    usage = dict(FB_USAGE, iterations=[declined, SERVED_IT])
    tail = text if restart else text[cut:]
    return message([THINK, {"type": "text", "text": text[:cut]}, FB_BLOCK, {"type": "text", "text": tail}],
                   usage=usage, model=MODEL)


def test_mid_stream_fallback_joins_the_partial_and_the_continuation(tmp_path):
    # 스트리밍에서 출력 도중 거절이면 부분 출력(경계 앞 text)이 content 에 남고 폴백 모델이 이어 쓴다. JSON 은 경계 앞 text +
    # 경계 뒤 text 다. 경계 뒤만 읽으면 JSON 조각이라 파싱이 실패한다 (D-24 결정).
    replies = {"Plain.sol": ok([CAND]), "Mid.sol": mid_stream_fallback([CAND])}
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    by = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by == {"Plain.sol": "후보 1", "Mid.sol": "후보 1"}
    jsonl = w.default_jsonl()[0]
    rows = read_rows(jsonl)
    assert {c["file"] for c in candidates(rows)} == {"Plain.sol", "Mid.sol"}
    mid = usage_rows(rows)["Mid.sol"]
    # SDK 가 fallback 블록에서 스냅샷의 model 을 폴백 모델로 바꾼다. 폴백 응답이라 fallback True
    assert (mid["model"], mid["fallback"]) == (FALLBACK_MODEL, True)
    # 토큰은 시도별 합계: 입력 100 + 10, 출력 40 + 5(거절된 부분 출력도 과금). 최상위 값(응답한 시도)은 returned
    assert (mid["input_tokens"], mid["cache_creation_input_tokens"], mid["cache_read_input_tokens"],
            mid["output_tokens"]) == (110, 30, 20, 45)
    assert mid["returned"] == {k: FB_USAGE[k] for k in scan.USAGE_KEYS}
    assert [a["type"] for a in mid["attempts"]] == ["message", "fallback_message"]
    assert "partial" not in mid                           # 스트림은 끝까지 왔다
    assert sum_usage(jsonl) == (60 + 160, 5 + 45)
    assert fallback_usage(jsonl) == (1, [FALLBACK_MODEL])


def test_mid_stream_fallback_that_restarts_is_a_visible_error_not_a_wrong_candidate(tmp_path):
    # 폴백 모델이 이어쓰지 않고 처음부터 다시 썼다면(문서에 명시가 없다) 이어 붙인 문자열은 JSON 이 아니다. 조용히 틀린 후보가
    # 되지 않고 그 파일의 ERROR 로 남고, 응답은 받았으니 usage 와 fallback True 는 기록된다. 파일럿에서 이 줄이 나오면 결정을 뒤집는다.
    replies = {"Restart.sol": mid_stream_fallback([CAND], restart=True)}
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    (line,) = progress(r["stdout"])
    assert line[3].startswith("ERROR JSON 파싱 실패")
    rows = read_rows(w.default_jsonl()[0])
    assert candidates(rows) == []
    assert usage_rows(rows)["Restart.sol"]["fallback"] is True


def test_pre_output_fallback_arrives_as_a_stream_with_the_fallback_block_first(tmp_path):
    # 출력 전 거절(D-23 의 fallback_ok): message_start 가 폴백 모델 이름을 담고 fallback 블록이 content 맨 앞에 온다. 이제 SSE 로 온다.
    evs = sse_events(fallback_ok([CAND]))
    assert evs[0][2]["message"]["model"] == FALLBACK_MODEL
    assert evs[1][2]["content_block"]["type"] == "fallback" and evs[1][2]["index"] == 0
    replies = {"Pre.sol": fallback_ok([CAND])}
    w = W(tmp_path, replies)
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert [p[3] for p in progress(r["stdout"])] == ["후보 1"]
    u = usage_rows(read_rows(w.default_jsonl()[0]))["Pre.sol"]
    assert (u["model"], u["fallback"], u["input_tokens"]) == (FALLBACK_MODEL, True, 110)


def test_a_stream_that_breaks_mid_way_fails_only_that_file_and_keeps_the_partial_usage(tmp_path):
    # 연결 끊김: 끝 청크 없이 서버가 닫는다. 스트림 도중의 httpx 오류는 SDK 가 감싸지 않는다(classify 가 이름으로 본다).
    # 그 파일은 ERROR 이고 재시도하지 않는다(SDK 재시도는 연결 단계뿐). 돈은 나갔으니 받은 만큼의 usage 를 partial 로 남긴다.
    w = W(tmp_path, ["B1.sol", "B2.sol", "B3.sol"])
    seq = [ok([CAND]), cut_after(ok([CAND]), 3, ("drop",)), ok([CAND])]
    r = run_scan(w, lambda i, name: seq[i], args=["--workers", "1"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 3                       # 끊긴 파일을 다시 요청하지 않았다
    prog = progress(r["stdout"])
    assert [p[3].split(" ")[0] for p in prog] == ["후보", "ERROR", "후보"]
    assert prog[1][3].startswith("ERROR stream interrupted: ") and "받은 만큼만 집계" in prog[1][3]
    jsonl = w.default_jsonl()[0]
    rows = read_rows(jsonl)
    u = usage_rows(rows)[prog[1][2]]
    # message_start 의 usage: 입력·캐시는 그 시도의 값이고 output_tokens 는 그 시점 값(1)이라 하한이다
    assert (u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"],
            u["output_tokens"], u["partial"], u["model"], u["fallback"]) == (10, 30, 20, 1, True, MODEL, False)
    assert set(u["call"]) == CALL_KEYS
    assert sum_usage(jsonl) == (60 * 3, 5 + 1 + 5)
    assert partial_rows(jsonl) == 1
    assert [x["_usage"].get("partial", False) for x in rows if "_usage" in x] == [False, True, False]


def test_a_response_whose_stream_ends_without_message_stop_is_an_error_with_its_usage(tmp_path):
    # 끝 청크는 정상으로 왔는데 message_delta·message_stop 이 없다(중간 장비가 자른 경우 등). stop_reason 이 None 이라 end_turn 이
    # 아니다. 조용히 빈 결과로 넘어가지 않고 ERROR 로 남고, 받은 usage 는 기록된다.
    w = W(tmp_path, ["N1.sol"])
    r = run_scan(w, {"N1.sol": cut_after(ok([CAND]), 3, ("end",))})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert progress(r["stdout"])[0][3] == "ERROR stop_reason=None (end_turn 아님)"
    assert usage_rows(read_rows(w.default_jsonl()[0]))["N1.sol"]["input_tokens"] == 10


BREAK_KINDS = {
    # 연결 끊김은 connection, 스트림 안의 error 이벤트(과부하)는 body 의 error.type 으로 5xx
    "drop": (lambda: cut_after(ok([CAND]), 3, ("drop",)), "connection"),
    "overloaded_event": (lambda: cut_after(ok([CAND]), 3, ("ev", "error", {
        "type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})), "5xx"),
}


@pytest.mark.parametrize("kind", sorted(BREAK_KINDS))
def test_streams_that_keep_breaking_trip_the_consecutive_error_breaker(tmp_path, kind):
    # 스트림 도중 실패도 연속 오류 차단기에 센다(옛 classify 는 httpx 예외·200 의 error 이벤트를 중립으로 보고 영원히 안 멈췄다).
    make, category = BREAK_KINDS[kind]
    w = W(tmp_path, ["K%02d.sol" % i for i in range(12)])
    r = run_scan(w, lambda i, name: make(), args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 8                       # 8번째 연속 실패에서 멈췄다
    why = "aborted after 8 consecutive API errors (%s)" % category
    assert [p[3] for p in progress(r["stdout"])[8:]] == ["ERROR skipped: " + why] * 4
    assert why in r["stderr"] and "치명" not in r["stderr"]
    rows = read_rows(w.default_jsonl()[0])
    assert len(usage_rows(rows)) == 8 and all(u["partial"] for u in usage_rows(rows).values())


@pytest.mark.parametrize("extra", [["--deadline-s", "120"], []])
def test_a_stalled_stream_is_a_timeout_and_counts_for_the_breaker(tmp_path, extra):
    # 스트림이 열린 뒤 바이트가 안 온다(ping 도 없음): read timeout 이 걸린다. 스트리밍에서 그 값은 총시간이 아니라 무응답의
    # 상한이다. 복사본의 CALL_TIMEOUT_S 를 2초로 줄여 시험한다. D-25: 호출마다 명시하는 값이라 --deadline-s 가 없어도 같다
    # (없을 때 SDK 기본 600초가 걸리면 이 시험은 limit 100초에서 TimeoutExpired 로 실패한다).
    w = W(tmp_path, ["S%02d.sol" % i for i in range(10)])
    w.patch_scan(CALL_TIMEOUT_S=2)
    r = run_scan(w, lambda i, name: cut_after(ok([CAND]), 2, ("hold",)),
                 args=["--workers", "4", *extra], limit=100)
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert "aborted after 8 consecutive API errors (timeout)" in r["stderr"]
    first = progress(r["stdout"])[0][3]
    assert first.startswith("ERROR timeout: stream stalled, no data for 2s") and "받은 만큼만 집계" in first
    rows = read_rows(w.default_jsonl()[0])
    assert len(usage_rows(rows)) >= 8 and all(u["partial"] for u in usage_rows(rows).values())


def test_deadline_closes_a_stream_that_only_sends_pings_and_keeps_the_partial_usage(tmp_path, import_s):
    # 가짜 서버가 message_start 뒤에 ping 만 보낸다(바이트는 계속 오는데 호출자가 보는 이벤트는 없다). 데드라인은 이벤트가 없어도
    # 걸려야 한다. SDK 는 ping 을 이벤트로 내주지 않아서 이벤트 루프의 시각 검사로는 못 막는다. 타이머가 데드라인에 스트림을 닫고,
    # 받은 만큼(message_start)의 usage 를 partial 로 남기고, 차단기에는 안 센다. 감시가 버리는 경로("버리고 종료", usage 없음)가
    # 아니다. 이 시험은 "서버가 ping 만 보내는 경우"를 흉내 낼 뿐이다. 실제 모델이 생각하는 동안 ping 이나 바이트를 보내는지는
    # 미확인이다(D-25, 파일럿에서 first_event_s·max_gap_s 로 잰다).
    w = W(tmp_path, ["T1.sol", "T2.sol", "T3.sol"])
    order = w.order()
    deadline = import_s + 5
    replies = {order[0]: cut_after(ok([CAND]), 2, ("pings",)), order[1]: ok([CAND]), order[2]: ok([CAND])}
    r = run_scan(w, replies, args=["--deadline-s", "%.1f" % deadline], limit=deadline + HARNESS_S)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    prog = progress(r["stdout"])
    assert prog[0][3].startswith("ERROR timeout: hard deadline reached (call limited to ")
    assert "받은 만큼만 집계" in prog[0][3]
    assert [p[3] for p in prog[1:]] == ["후보 1", "후보 1"]
    assert "버리고 종료" not in r["stdout"] and "토큰 미집계" not in r["stdout"]
    assert "연속" not in r["stderr"]
    rows = read_rows(w.default_jsonl()[0])
    u = usage_rows(rows)[order[0]]
    assert (u["input_tokens"], u["cache_creation_input_tokens"], u["cache_read_input_tokens"], u["partial"]) == (
        10, 30, 20, True)
    assert 0 < u["call"]["elapsed_s"] <= deadline                  # 호출은 데드라인 안에 끝났다(그때 닫았다)
    assert [c["file"] for c in candidates(rows)] == order[1:]      # 끝난 파일의 결과는 그대로
    assert "파일 3개: 스캔 2 · 실패 1 · 건너뜀 0" in r["stdout"]
    assert r["elapsed"] < deadline + scan.HARD_GRACE_S + SLACK_S    # 하네스 한계(데드라인 + 15초) 안이다


# ---------------------------------------------------------------------------------------------
# D-25: 이벤트 시각 로그, 끝난 응답을 닫는 경합, 출력 전 폴백 뒤의 끊김, error 이벤트의 치명 타입
# ---------------------------------------------------------------------------------------------

def test_call_log_records_the_first_event_and_the_largest_gap_between_events_the_caller_saw(tmp_path):
    # 서버가 첫 이벤트 전에 1.0초, message_start 와 다음 이벤트 사이에 2.5초 쉰다(쉬는 동안 ping 만 보낸다). first_event_s 는
    # 스트림이 열린 뒤 첫 이벤트까지, max_gap_s 는 이벤트 사이의 최대 간격이다. 둘 다 호출자가 보는 이벤트 기준이다(SDK 가 ping 을
    # 안 준다): ping 을 보내도 값이 같다. 파일럿에서 생각하는 동안 이벤트가 오는지 보는 값이다.
    # 첫 이벤트는 클라이언트가 처음 파싱하느라 0.2~0.3초 늦게 보인다(이 머신에서 실측: 서버가 0.83초에 쓴 message_start 를 1.03초에
    # 봄). 그 시간만큼 first_event_s 는 늘고 max_gap_s 는 준다. 둘의 합은 그대로라서 범위를 넉넉히 잡고 합도 본다.
    w = W(tmp_path, ["G1.sol"])
    evs = sse_events(ok([CAND]))
    r = run_scan(w, {"G1.sol": Script(("sleep", 1.0), evs[0], ("ping",), ("sleep", 2.5), ("ping",), *evs[1:], ("end",))})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    call = usage_rows(read_rows(w.default_jsonl()[0]))["G1.sol"]["call"]
    assert 0.95 <= call["first_event_s"] < 1.0 + SLACK_S
    assert 2.0 <= call["max_gap_s"] < 2.5 + SLACK_S
    assert call["max_gap_s"] > call["first_event_s"]
    assert abs(call["first_event_s"] + call["max_gap_s"] - 3.5) < 0.6
    assert call["elapsed_s"] >= call["first_event_s"] + call["max_gap_s"] - 0.05


def test_a_response_that_was_complete_up_to_message_stop_survives_the_connection_closing_right_after(tmp_path):
    # D-25 (리뷰 8): 데드라인 타이머가 응답의 마지막 읽기와 겹쳐 스트림을 닫으면, message_stop 까지 받은 end_turn 응답이 timeout 으로
    # 버려지고 과금만 남았다. 같은 모양을 결정적으로 만든다: 이벤트를 message_stop 까지 다 보내고 끝 청크 없이 연결을 끊는다. 이제
    # 그 응답은 정상 결과이고 usage 는 partial 이 아니다. message_stop 전에 끊긴 파일은 그대로 오류다.
    w = W(tmp_path, ["C1.sol", "C2.sol"])
    replies = {"C1.sol": Script(*sse_events(ok([CAND])), ("drop",)), "C2.sol": cut_after(ok([CAND]), 3, ("drop",))}
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    by = {p[2]: p[3] for p in progress(r["stdout"])}
    assert by["C1.sol"] == "후보 1"
    assert by["C2.sol"].startswith("ERROR stream interrupted: ")
    rows = read_rows(w.default_jsonl()[0])
    u = usage_rows(rows)
    assert "partial" not in u["C1.sol"] and u["C1.sol"]["output_tokens"] == USAGE["output_tokens"]   # message_delta 의 최종 값
    assert u["C2.sol"]["partial"] is True
    assert [c["file"] for c in candidates(rows)] == ["C1.sol"]
    assert "연속" not in r["stderr"] and partial_rows(w.default_jsonl()[0]) == 1


def test_a_stream_cut_after_the_fallback_model_started_is_marked_fallback(tmp_path):
    # D-25 (리뷰 4): 출력 전 폴백이면 message_start 가 폴백 모델 이름을 담는다. 그 뒤에 끊기면 partial 행에는 iterations 가 없어서
    # (message_delta 에만 실린다) 전에는 fallback False 였다. 이제 message_start 의 model 이 요청한 모델과 다르면 True 다.
    w = W(tmp_path, ["F1.sol", "F2.sol"])
    replies = {"F1.sol": cut_after(fallback_ok([CAND]), 2, ("drop",)), "F2.sol": cut_after(ok([CAND]), 2, ("drop",))}
    r = run_scan(w, replies)
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    jsonl = w.default_jsonl()[0]
    u = usage_rows(read_rows(jsonl))
    assert (u["F1.sol"]["partial"], u["F1.sol"]["model"], u["F1.sol"]["fallback"]) == (True, FALLBACK_MODEL, True)
    assert (u["F2.sol"]["partial"], u["F2.sol"]["model"], u["F2.sol"]["fallback"]) == (True, MODEL, False)
    assert fallback_usage(jsonl) == (1, [FALLBACK_MODEL])


@pytest.mark.parametrize("etype", ["authentication_error", "permission_error", "billing_error", "not_found_error"])
def test_a_fatal_error_event_inside_a_stream_stops_the_scan_like_the_http_status_does(tmp_path, etype):
    # D-25 (리뷰 7): 스트림 안의 error 이벤트는 200 응답이라 상태 코드로는 401·402·403·404 와 구별이 안 됐다. 인증·권한·결제·모델 없음은
    # 파일마다 반복될 오류라 그 type 이면 첫 발생에 멈춘다(exit 2). 차단기(연속 8건)까지 기다리지 않는다.
    w = W(tmp_path, ["F1.sol", "F2.sol", "F3.sol", "F4.sol"])
    event = ("ev", "error", {"type": "error", "error": {"type": etype, "message": "fake %s" % etype}})
    r = run_scan(w, lambda i, name: ok([CAND]) if i == 0 else cut_after(ok([CAND]), 2, event), args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 2                           # 셋째·넷째 파일은 요청조차 안 나갔다
    fatal_lines = [l for l in r["stderr"].splitlines() if "치명적" in l]
    assert len(fatal_lines) == 1 and "HTTP 200" in fatal_lines[0] and etype in fatal_lines[0]
    assert [p[3] for p in progress(r["stdout"])[2:]] == ["ERROR skipped: aborted after fatal API error"] * 2
    assert_progress_matches_fatal_line(progress(r["stdout"])[1], fatal_lines[0])
    assert "연속" not in r["stderr"]


def test_a_refusal_or_max_tokens_response_cut_after_message_stop_is_not_counted_as_an_api_outage(tmp_path):
    # D-25 재검증 (4): 끊김이 message_stop 뒤에 와도 end_turn 이 아니면(refusal·max_tokens) read_stream 은 예외를 그대로 낸다(위 시험은
    # end_turn 만 살린다). 그 예외가 classify 에서 TransportError 라 연결 오류로 분류돼 차단기에 셌다. 응답은 HTTP 200 으로 완결됐으니
    # API 장애가 아니다. 중립이어야 한다. 12개 파일이 전부 이 모양이어도(차단기 N 은 8) 멈추지 않고 파일마다 ERROR 로 남는다.
    refused = message([], "refusal", {"type": "refusal", "category": "cyber", "explanation": None})
    cut = message([THINK], "max_tokens")
    w = W(tmp_path, ["R%02d.sol" % i for i in range(12)])
    # 응답도 기대와 같은 기준(파일 이름의 번호)으로 고른다. 도착 순번 i 의 홀짝에 기대면 열거 순서가 바뀔 때 기대와 어긋난다 (D-25 재검증 3차)
    r = run_scan(w, lambda i, name: Script(*sse_events(refused if int(name[1:3]) % 2 else cut), ("drop",)), args=["--workers", "1"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 12                                  # 8번째에서 멈추지 않았다
    prog = progress(r["stdout"])
    assert len(prog) == 12 and all(p[3].startswith("ERROR stream interrupted: ") for p in prog)
    assert "연속" not in r["stderr"] and "aborted" not in r["stdout"] + r["stderr"]
    # D-25 재검증 2차 (A): 파일럿 확인 항목 1 이 "stop_reason=refusal category=" 줄로 거절률을 세므로 이 경로의 ERROR 에도 실린다.
    # 홀수 번호는 refusal(category=cyber), 짝수는 max_tokens(stop_details 없음 -> category=null)
    want = {1: "; stop_reason=refusal category=cyber", 0: "; stop_reason=max_tokens category=null"}
    assert all(p[3].endswith(want[int(p[2][1:3]) % 2]) for p in prog), [p[3] for p in prog]
    assert sum("stop_reason=refusal category=" in p[3] for p in prog) == 6


@pytest.mark.parametrize("reply", [
    pytest.param(message([], "refusal", {"type": "refusal", "category": "cyber", "explanation": None}), id="refusal"),
    pytest.param(message([THINK], "max_tokens"), id="max_tokens"),
    pytest.param(ok([CAND]), id="end_turn"),
])
def test_a_fatal_error_event_after_message_stop_still_stops_the_scan(tmp_path, reply):
    # D-25 재검증 2차 (B): message_stop 뒤의 예외를 무조건 중립으로 두면 끝난 refusal 응답 뒤에 온 인증 오류(SSE error 이벤트)까지
    # 묻힌다. 치명은 파일마다 반복될 오류라 그대로 첫 발생에 멈춘다(exit 2). 위 시험의 연결 끊김(TransportError)은 중립인 채다.
    # 3차: stop_reason 이 end_turn 이어도 같다. read_stream 이 end_turn 뒤의 예외를 삼키던 때는 응답이 정상으로 돌아가 scan_one 의
    # 치명 분기에 못 갔고, 다음 파일이 같은 오류로 계속 나갔다.
    event = ("ev", "error", {"type": "error", "error": {"type": "authentication_error", "message": "fake authentication_error"}})
    w = W(tmp_path, ["F1.sol", "F2.sol", "F3.sol"])
    r = run_scan(w, lambda i, name: Script(*sse_events(reply), event), args=["--workers", "1"])
    assert r["rc"] == 2, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 1                           # 둘째·셋째 파일은 요청조차 안 나갔다
    fatal_lines = [l for l in r["stderr"].splitlines() if "치명적" in l]
    assert len(fatal_lines) == 1 and "HTTP 200" in fatal_lines[0] and "authentication_error" in fatal_lines[0]
    prog = progress(r["stdout"])
    assert [p[3] for p in prog[1:]] == ["ERROR skipped: aborted after fatal API error"] * 2
    assert_progress_matches_fatal_line(prog[0], fatal_lines[0])
    assert "연속" not in r["stderr"]


# ---------------------------------------------------------------------------------------------
# D-28: --effort max, --max-tokens (and the no-data limit that follows it)
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5"])
def test_effort_max_and_max_tokens_128000_reach_the_request_and_the_run_header(tmp_path, model):
    # D-28 planned run: Fable 5.1 and Opus 5.5 at effort max, max_tokens 128000. The body carries both values, the _run row
    # records them, and the per-call no-data limit grows with max_tokens (3600, from 1800 at 64000). Nothing else in the
    # body changes: still streaming, beta endpoint, fallbacks, 5m TTL, and no thinking.
    w = W(tmp_path, ["X1.sol", "X2.sol"])
    r = run_scan(w, {"X1.sol": ok([CAND]), "X2.sol": ok([CAND])},
                 args=["--model", model, "--effort", "max", "--max-tokens", "128000"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert len(r["seen"]) == 2
    assert {(x["body"]["model"], x["body"]["output_config"]["effort"], x["body"]["max_tokens"]) for x in r["seen"]} == {
        (model, "max", 128000)}
    assert {x["body"]["stream"] for x in r["seen"]} == {True}
    assert {x["body"]["fallbacks"] for x in r["seen"]} == {"default"}
    assert {x["body"]["system"][1]["cache_control"]["ttl"] for x in r["seen"]} == {"5m"}
    assert not [x for x in r["seen"] if "thinking" in x["body"]]
    header = read_rows(w.default_jsonl()[0])[0]["_run"]
    assert (header["effort"], header["max_tokens"], header["call_timeout_s"]) == ("max", 128000, 3600)
    # no --deadline-s: every call is given the no-data limit itself, which is now 3600
    assert {float(x["headers"]["x-stainless-read-timeout"]) for x in r["seen"]} == {3600.0}


def test_default_max_tokens_and_no_data_limit_are_unchanged(tmp_path):
    w = W(tmp_path, ["X1.sol"])
    r = run_scan(w, {"X1.sol": ok([CAND])})
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert {x["body"]["max_tokens"] for x in r["seen"]} == {64000}
    header = read_rows(w.default_jsonl()[0])[0]["_run"]
    assert (header["max_tokens"], header["call_timeout_s"]) == (64000, 1800)
    assert {float(x["headers"]["x-stainless-read-timeout"]) for x in r["seen"]} == {1800.0}


def test_max_tokens_one_is_accepted_and_changes_nothing_but_the_body_value(tmp_path):
    # the lower bound is 1; below 64000 the no-data limit stays at its floor (CALL_TIMEOUT_S)
    w = W(tmp_path, ["X1.sol"])
    r = run_scan(w, {"X1.sol": ok([CAND])}, args=["--max-tokens", "1"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    assert {x["body"]["max_tokens"] for x in r["seen"]} == {1}
    assert read_rows(w.default_jsonl()[0])[0]["_run"]["call_timeout_s"] == 1800


def test_the_per_call_limit_with_a_deadline_is_capped_by_the_larger_no_data_limit(tmp_path, import_s):
    # call_limits(rem, cap): with plenty of time left a call gets the cap itself (3600 at 128000), and a shorter remaining time
    # still shrinks it as before. 20000 s left -> 3600 s and 2 retries; the first call's own timeout shows it.
    w = W(tmp_path, ["X1.sol"])
    r = run_scan(w, {"X1.sol": ok([CAND])}, args=["--max-tokens", "128000", "--deadline-s", "20000"])
    assert r["rc"] == 0, r["stdout"] + r["stderr"]
    call = [x["_usage"]["call"] for x in read_rows(w.default_jsonl()[0]) if "_usage" in x][0]
    assert (call["timeout_s"], call["max_retries"]) == (3600.0, 2)
    assert {float(x["headers"]["x-stainless-read-timeout"]) for x in r["seen"]} == {3600.0}


@pytest.mark.parametrize("bad", ["0", "-5", "128001", "1000000", "abc", "64000.5", ""])
def test_max_tokens_outside_1_to_128000_is_a_usage_error_and_sends_no_request(tmp_path, bad):
    w = W(tmp_path, ["V1.sol"])
    r = subprocess.run([sys.executable, str(w.tools / "scan.py"), str(w.repo), "--max-tokens", bad],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 2 and "--max-tokens" in r.stderr, r.stderr          # argparse error; no client, no request
    assert not w.default_jsonl()
