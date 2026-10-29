# eval 하네스

AttackTrail 의 내부 역량 측정과, 프롬프트·체크리스트를 바꿨을 때의 회귀 탐지용이다. 대외 홍보용 수치를 내는 도구가 아니다.

- 1차 지표는 recall. precision 은 2차(감사자가 오탐을 걸러내는 시간 비용). 비용은 참고값.
- 채점과 매칭은 직접 구현하지 않는다. Nethermind `scoring_algo` 를 그대로 쓴다.
- 결정 경위는 `DECISIONS.md` 에 있다.

## 설치

저장소 루트에서 데이터셋 서브모듈을 받는다. `scabench-org/scabench@eec0020939a47bbc06a98a7348a90908b96f4b4d` 에 고정돼 있다.

```powershell
git submodule update --init eval/datasets/scabench
```

eval/ 에서 venv 를 만들고 채점기를 커밋 고정으로 설치한다. 현재 venv 는 Python 3.12.10 이고 아래 명령과 같은 출처(`git+...@a62a15d...`)로 설치돼 있다.

```powershell
cd eval
python -m venv .venv
.venv\Scripts\python.exe -m pip install "git+https://github.com/NethermindEth/auditagent-scoring-algo@a62a15d1ea254158dd5cda4bea8ce8cf5791fb27" pytest pytest-asyncio
```

`scoring-algo` 0.1.0 이 `openai==2.24.0`, `pydantic==2.11.7`, `pydantic-settings==2.6.1`, `rich`, `typer`, `langfuse` 등을 고정 버전으로 끌고 온다.
다른 의존성은 추가하지 않는다. `pytest-asyncio` 가 없으면 채점기 자체 테스트 중 async 6건이 실패한다.

Windows 에서는 대상 저장소 경로가 260자를 넘는 경우가 실측됐다. 체크아웃 코드가 `core.longpaths=true` 를 직접 건다.

키는 환경변수로만 준다. 파일에 쓰지 않는다(`.env` 는 gitignore 대상이지만 그래도 쓰지 않는다).

```
OPENAI_API_KEY      Nethermind 채점(--judge nethermind)과 --baseline-mode live 에 필요
ANTHROPIC_API_KEY   --agent attacktrail 에 필요 (anthropic SDK 도 별도 설치 필요)
ATTACKTRAIL_ANTHROPIC_API_KEY   위 키의 별칭. 둘 다 있으면 이쪽이 우선
```

Claude Code 는 `ANTHROPIC_API_KEY` 만 읽고, 그 변수에 키가 있으면 그 키로 과금한다. setx 로 `ANTHROPIC_API_KEY` 를 저장하지 말 것. AttackTrail 키는 사용자 환경변수 `ATTACKTRAIL_ANTHROPIC_API_KEY` 로 저장한다(`setx ATTACKTRAIL_ANTHROPIC_API_KEY <키>`, 새 셸부터 적용). `eval run` 은 이 값을 먼저 읽고 없으면 `ANTHROPIC_API_KEY` 를 읽어서, scan.py 자식 프로세스에 `ANTHROPIC_API_KEY` 라는 이름으로 넘긴다(scan.py 와 SDK 는 그대로). 별칭 이름은 자식에게 넘어가지 않는다. 키 값은 manifest 에 쓰지 않는다(별칭 값도 같이 가린다). 별칭은 `eval run` 경유일 때만 읽는다. `AttackTrailAgent` 를 직접 부르면 `os.environ` 의 `ANTHROPIC_API_KEY` 만 본다(D-24 §7).

AttackTrail 의 실제 API 호출 상태: 선행 5파일 실행과 secondswap 36파일 본 실행을 마쳤다(2026-10-21). 결과와 시간 설계 결정은 DECISIONS D-25 끝의 "선행 5파일 실행 결과와 시간 설계 결정", "본 실행 결과" 소절에 있다. 선행 실행은 `eval run` 이 아니라 scan.py 직접 실행이었고, 본 실행은 `eval run --judge mock` 이라 TP·FP 수치는 배관용이다. 본 실행의 36파일은 D-26(아래)의 제외 규칙이 들어가기 전의 파일 집합이다.

`--agent attacktrail` 의 scan.py 는 응답을 `parse_candidates()` 로 읽는다. text 블록만 읽고 `stop_reason` 이 `end_turn` 이 아니면(refusal, max_tokens 등) 그 파일을 원인과 함께 `ERROR` 로 남긴다. 후보의 필드 타입이 스키마와 다르거나 스키마에 없는 키가 있으면 그 파일만 `ERROR` 다. `max_tokens` 는 기본 64000(`--max-tokens` 로 1~128000)이고 호출 형태는 `api-v6`(스트리밍)이다. effort `max` 와 `--max-tokens` 는 요청 본문의 값만 바꾸므로 `api-v6` 는 그대로다 (DECISIONS D-17, D-19, D-20, D-23, D-24, D-28). 어댑터가 하는 일:

- 기본 모델은 2026-10-29(D-34)부터 `claude-haiku-5-5` + effort `high` 다(사용자 결정: Opus·Mythos 제외, 실측 secondswap 재현율 0.40 에 회당 약 $0.05). Haiku 5.5 에는 서버 측 폴백이 없어 scan.py 가 `fallbacks`·beta 헤더를 보내지 않고, 거절은 그 파일의 ERROR 로 남는다. 프롬프트가 100K 토큰을 넘는 호출(`--web`, 기준 문서 약 11만 토큰)은 Haiku 의 두 번째 단가($0.50/$2.50 per MTok)로 과금된다. API 지출 상한은 `ATTACKTRAIL_BUDGET_USD`(기본 100)이고 `eval run` 이 시작 전에 `runs/spend_ledger.jsonl`(run 마다 프로젝트별 cost_usd 를 더함, git 밖)을 보고 상한에 닿았으면 거부한다(`runner/budget.py`). cost_usd 는 어댑터의 모델별 단가표(`adapters/attacktrail.py` PRICES)로 `_usage` 행마다 계산해 manifest 에 남긴다. 아래는 D-25~D-33 의 Fable 5.1 기준 설명이다.
- 그 전 기본 모델은 `claude-fable-5-1` 이었다(scan.py `--model` 기본값, 날짜 접미사 없음). 공개 모델이라 승인 없이 쓴다. 같은 모델의 `claude-mythos-5-1` 은 검증 프로그램(Cyber Verification Program 등) 승인 조직에 제공되고 Console 은 워크스페이스 단위로 배정된다. 이 회사 조직에서는 `claude-mythos-5-1`·`claude-mythos-5` 가 count_tokens 404 `not_found_error` 였고 `claude-fable-5-1` 은 통과했다(2026-10-21 실측). 번들 문서(claude-api 스킬 번들 `C:\Users\정동욱\AppData\Local\Temp\claude\bundled-skills\2.1.299\f01998021b84cff08d755411397bd05b\claude-api\shared\model-migration.md` 1686행. 세션 임시 디렉터리라 사라질 수 있고 2026-10-21 에 직접 열어 확인했다)는 404 가 보이면 보존 설정을 먼저 보라고 권하고, 최종 오류는 400 이다. 위 mythos 404 가 접근 배정 때문인지 보존 설정 때문인지는 구분하지 못했다. Fable 5.1 은 사이버보안 안전 분류기가 있어 정당한 보안 작업도 거절될 수 있고, 거절은 서버 측 폴백이 받는다. 선행 5파일(2026-10-21)에서는 거절과 폴백이 0건이었고 응답 `model` 이 요청 이름 `claude-fable-5-1` 과 같았다. 30일 데이터 보존 요건이 Fable 5.1 에 걸리는지는 문서로 확인하지 못했다. 걸리면 400 "...must have data retention enabled" 다. 선행 5파일 5건은 400 없이 응답을 받았다(D-24 §2, D-25 §1). `output_config.effort` 를 명시해서 보낸다. 기본 `xhigh`, 낮추려면 `./eval run --effort high`, 가장 높이려면 `--effort max`(scan.py `--effort`, 허용값은 `high`·`xhigh`·`max` 셋. D-28). Fable 5.1·Opus 5.5·Sonnet 5.5 는 low~max 를 지원한다(번들 claude-api 문서, 2026-10-22 확인). Sonnet 5.5 의 thinking `between_tools` 는 xhigh·max 에서 400 이지만 thinking 을 보내지 않아 해당 없다. `--max-tokens N`(1~128000. 128000 은 모델 출력 상한)은 요청 `max_tokens` 를 바꾼다. 이전 기본이던 Sonnet 5.5 는 `--model claude-sonnet-5-5 --effort high`, Opus 5.5 는 `--model claude-opus-5-5` 로 계속 돈다. 가격은 $10/$50 per MTok(캐시 5분 쓰기 $12.50, 읽기 $0.25)이다. 기준 문서 system 블록의 캐시 TTL 은 `5m` 다. `thinking` 은 보내지 않는다(항상 켜져 있고 disabled·budget_tokens 는 400). manifest `agent.model` 과 `agent.effort` 에 남는다. effort `max` 와 `max_tokens` 128000 을 서버가 받는지는 D-28 시점에는 확인하지 못했고, D-29 실측으로 확인됐다(Fable 5.1 max, 128000 요청 정상 처리. 48호출에 400·거절·폴백 0). Opus 5.5 도 같은 요청을 받았다(16호출, 400 없음, 폴백 0). 최대 출력 62,225 토큰이라 Opus 의 128K 출력 한도 자체는 미확인이다. 폴백이 일어났을 때 128000 이 유지되는지는 아직 모른다(DECISIONS D-29).
- 호출은 beta 엔드포인트의 스트리밍 helper(`client.beta.messages.stream(...)` 컨텍스트 매니저와 `get_final_message()`)로 간다. `max_tokens`(기본 64000), `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` 를 보낸다. 스트리밍인 이유: 64000 토큰 출력이 끝날 때까지 바이트가 없으면 유휴 연결이 끊길 수 있고, 문서도 큰 `max_tokens` 에는 스트리밍을 권한다. 서버는 거절한 요청을 `claude-opus-4-8` 또는 `claude-opus-5` 로 다시 보낸다(범주별 라우팅은 비공개고 일부 범주는 폴백이 없다). 최종 `stop_reason` 이 `refusal` 이면 체인 전체가 거절한 것이라 그 파일은 `ERROR` 다. 출력 도중 거절이면 부분 출력(경계 앞 text)과 폴백 모델의 이어쓰기(경계 뒤 text)를 이어 붙여 한 JSON 으로 읽는다. 이어쓰기인지 문서에 명시가 없다. 선행 5파일(2026-10-21)에는 거절이 없어 확인하지 못했고 본 실행의 확인 항목으로 남는다(D-24 §4). 폴백은 Claude API 전용이다. 계정이 폴백을 허용하지 않아 beta 요청이 400 을 받으면 기존 분류대로 파일별 `ERROR` 이고, 연속 8건이면 차단기가 멈춘다. 스트림 도중에 끊기면 그 파일은 `ERROR` 이고 다시 요청하지 않는다(SDK 재시도는 연결 단계뿐이다).
- 파일을 병렬로 부른다(`--workers`, 기본 4). 결과 행과 진행 줄은 파일 순서 그대로다.
- 결과는 `<project_dir>/raw/scan.jsonl` 에 직접 쓴다(`--out`).
- 시간 한도를 둘 넘긴다. 둘 다 scan.py 가 시작해서(main 진입) 센 초다.
  - `--time-budget`(소프트): 하네스 타임아웃에서 `max(1815초, 10%)` 를 뺀 값(하한은 타임아웃의 절반). 넘으면 새 파일 호출을 시작하지 않고 남은 파일을 `ERROR skipped: time budget reached` 로 남긴다. 이미 나간 호출은 끊지 않는다. 1815 는 하드 여유 15초에 가정한 최악 호출 길이 1800초(`MAX_CALL_S`)를 더한 값이라, 소프트 한도 직전에 시작한 호출이 최악 길이여도 하드 데드라인 안에 끝난다. 이 수치는 D-24 §5 의 추정이었고, 선행 5파일 실측(최장 호출 293.3초) 뒤 유지하기로 했다(DECISIONS D-25). 하네스 타임아웃이 3,630초(소프트 여유 1815초의 2배) 아래면 하한(타임아웃의 절반)이 먼저 걸리고, 소프트 한도 직전에 시작한 호출이 최악 길이여도 하드 데드라인 안에 끝난다는 보장이 없다. 어댑터는 그런 값이면 stderr 에 경고 한 줄을 내고 실행은 막지 않는다(D-25).
  - `--deadline-s`(하드): 하네스 타임아웃에서 15초를 뺀 값. 스트림이 열려 있으면 이 시각에 타이머가 스트림을 닫는다. 받은 만큼의 usage 는 `partial` 행으로 남기고 차단기에는 세지 않는다(`ERROR timeout: hard deadline reached (call limited to Ns; ...)`). 연결 단계(응답 헤더까지)의 timeout·재시도는 남은 시간으로 줄인다. 스트리밍에서 timeout 값은 총시간이 아니라 읽기 사이 간격(무응답)의 상한이다. `CALL_TIMEOUT_S` 는 1800초이고 `--deadline-s` 가 없어도 호출마다 이 값을 명시한다. 선행 5파일(2026-10-21)에서 `max_gap_s`(호출자가 본 이벤트 사이의 단일 최장 간격)는 호출 길이의 66~82%, 최대 234.2초였다. 가장 긴 무이벤트 구간이 그만큼이라는 뜻이고, 그 구간이 모델이 생각하는 구간이라는 해석은 추정이다. 그 구간이 그 실행의 시도당 timeout(1193.4초)보다 짧아서 ping 이나 바이트가 오는지는 여전히 모른다(공식 문서에도 없다). 1800초는 이 실측 뒤 유지하기로 했다(DECISIONS D-25). 근거: 선행 실측 74 tok/s 가 유지되면 호출 길이는 64000 토큰 ÷ 74 ≈ 865초를 넘기 어렵고 1800 은 그 2배 이상이다. 속도가 D-24 의 가정(약 36 tok/s)으로 떨어지면 64000 토큰 출력이 약 1,778초라 1800 과 거의 같다. 최장 이벤트 간격 기준(SDK 가 ping 을 숨긴 뒤의 간격)으로는 최장 234.2초의 7.7배다. 1800초 무응답은 장애로 보고 차단기에 센다. 남은 시간이 1초 미만이면 요청 없이 `ERROR skipped: hard deadline reached`. 닫을 스트림이 없는 호출(Retry-After 재시도 대기 등)은 데드라인 + 2초 뒤에 버리고, 끝난 파일 결과를 먼저 기록한 채 종료한다. 버린 호출은 응답을 못 읽어 `_usage` 행이 없고, 진행 줄·요약·`error` 메모에 토큰 과소 집계가 표시된다. 결과를 파일 순서로 쓰기 때문에 외부 강제 종료나 크래시 때는 앞 파일이 매달린 동안 뒤 파일의 완료 결과가 메모리에만 있다가 사라질 수 있다(데드라인 경로는 끝난 결과를 먼저 쓴다). 15초는 측정한 종료 지연(최대 약 2.7초)의 약 5.5배다(`adapters/attacktrail.py` 주석, DECISIONS D-20 §1). 다른 스레드의 `close()` 가 막힌 읽기를 깨우는 것은 이 머신(Windows 11)에서만 확인했다.
  - 프로젝트 기본 timeout: `--timeout` 을 안 주면 API 어댑터는 10800초(3시간), CLI 어댑터와 baseline 은 1800초다. 값과 근거(가정한 출력 속도 포함)는 D-24 §5 에 있는 추정이었고, 선행 5파일 실측(D-25) 뒤 10800 을 포함해 유지하기로 했다. 가정한 출력 속도 약 36 tokens/s 는 실측으로 약 74~77 tokens/s 였다. 쓴 값은 manifest `agent.timeout_s` 에 남는다.
  - `--max-tokens` 와 시간 상수(D-28): `MAX_CALL_S`(어댑터)와 `CALL_TIMEOUT_S`(scan.py 상수)는 `max_tokens` 64000 에 맞춘 1800초이고 값은 그대로다. 64000 보다 크게 주면 `max_tokens` 에 비례해 커지는 것은 두 상수가 아니라 scan.py 의 `call_cap()`(97~104행)과 어댑터의 `max_call_s()` 가 돌려주는 값이다(`ceil(1800 × max_tokens / 64000)`. 128000 에서 호출 상한 3600, 소프트 여유 3615, 짧은 타임아웃 경고 기준 7230). 36 tokens/s 가정에서 128000 토큰은 약 3,556초라 1800 을 넘기 때문이다(실측 74 tokens/s 면 약 1,730초). 64000 이하는 전과 같다. 기본 프로젝트 timeout 10800 은 그대로 둔다. 16파일·워커 4·4라운드에서 모든 호출이 128000 토큰을 다 쓰는 최악은 속도에 따라 갈린다. 36 tokens/s 면 호출당 3,556초라 2라운드가 7,111초에 끝나 소프트 한도(7,185초)보다 이르고, 3라운드까지 돈 뒤 4라운드 파일 4개가 `skipped: time budget reached` 로 커버리지에 남는다. 호출당 3,600초(약 35.6 tokens/s 이하)면 2라운드가 7,200초에 끝나 15초 늦어서 3라운드가 시작하지 않고 파일 8개가 건너뛰어진다. 둘 다 덮으려면 `--timeout 14500`(소프트 10885, 하드 14485)을 준다. 실측 속도 74 tokens/s 면 4라운드가 6,920초라 기본 소프트 한도(7185초) 안이다.
  - 둘 다 소진되면 그 프로젝트는 `ok` 이고 `error` 메모에 건너뛰거나 실패한 파일이 적힌다.
- 응답을 받은 파일마다 usage 를 JSONL `_usage` 행으로 남기고 `tokens_in`·`tokens_out` 으로 합산한다(거절·잘림 응답 포함). 행에는 `service_tier`·`speed`·`inference_geo` 가 응답에 있을 때 들어간다. 행마다 응답 모델 `model` 과 폴백 응답 여부 `fallback`(bool)이 있다. `fallback` 은 `usage.iterations` 에 `type == "fallback_message"` 항목이 있고 최종 `stop_reason` 이 `refusal` 이 아닐 때 `true` 다. 거절 뒤 폴백으로 시도가 둘 이상이면 네 토큰 키(`input_tokens` 등)는 시도별 합계(과소 집계 방지)이고 최상위 usage 는 `returned`, 시도별 값은 `attempts` 에 남는다(D-23). 행마다 `call`(호출 시작 오프셋 `started_at_s`, 소요 `elapsed_s`, 준 `timeout_s`, `max_retries`, 스트림이 열린 뒤 첫 이벤트까지 `first_event_s`, 이벤트 사이 최대 간격 `max_gap_s`)도 있다. 호출 시간 분포를 보려는 로그이고, 선행 5파일(2026-10-21)에서 처음 값이 나왔다(D-25). 뒤 둘은 SDK 가 ping 을 호출자에게 안 줘서 호출자가 보는 이벤트 기준이다. 응답을 받은 호출만 남고, 실제 재시도 횟수는 없다(D-21 H-3). 스트림이 도중에 끊기거나(데드라인, 연결 끊김, 무응답) 닫히면 message_start 까지 받은 usage 가 `partial: true` 행으로 남는다. 입력·캐시 토큰은 message_start 가 담은 시도의 값이고(끊기기 전에 폴백이 있었으면 다른 시도의 입력은 빠질 수 있다) `output_tokens` 는 하한이다(최종 출력 토큰은 스트림 끝의 message_delta 에만 실린다). message_start 의 model 이 요청한 모델과 다르면(출력 전 폴백) 그 행의 `fallback` 은 `true` 다. message_stop 까지 받은 end_turn 응답은 그 뒤에 스트림이 닫혀도 정상 결과로 읽는다. 다만 그 뒤의 오류가 인증·권한·결제·모델 없음이면 읽지 않고 멈춘다(D-25). 어댑터 `error` 메모에 그 수가 나온다(D-24 §3). `cost_usd` 는 단가 확정 전이라 `None` 이다. 비용 상한은 도구가 막지 않는다. Console 워크스페이스 지출 한도를 설정한다(D-24 §6).
- 401·402·403·404 는 파일마다 반복될 오류(키, 결제, 권한, 모델 이름)라 첫 발생에 남은 호출을 멈추고 exit 2 로 끝난다. 스트림 안의 error 이벤트(HTTP 200)도 type 이 `authentication_error`, `permission_error`, `billing_error`, `not_found_error` 면 첫 발생에 멈춘다. 429·5xx(529 포함)·400·연결 오류·호출 timeout 이 서로 다른 파일에서 연속 8건(완료 순서 기준) 나오면 같은 식으로 멈춘다. 응답을 받으면(파싱 실패 포함) 카운터는 0 으로 돌아간다. message_stop 까지 받은 뒤에 연결이 끊긴 refusal·max_tokens 응답은 그 파일만 `ERROR` 이고 카운터는 그대로다(응답이 HTTP 200 으로 끝났으므로 API 장애로 세지 않는다, D-25). 이 `ERROR` 끝에는 `stop_reason=<값> category=<값>` 이 붙는다(예: `stop_reason=refusal category=cyber`. category 가 없으면 `null`). 그래서 `stop_reason=refusal category=` 줄로 거절률을 세는 방식에 이 경로도 걸린다. 응답이 끝난 뒤에 온 인증·권한·결제·모델 없음 error 이벤트는 `stop_reason` 이 `end_turn`·`refusal`·`max_tokens` 어느 쪽이든 위처럼 치명이라 멈춘다(D-25 재검증 2차·3차). 치명으로 멈춘 파일의 진행 줄 `ERROR` 는 stderr 의 치명 줄과 같은 문구다(예: `APIStatusError (HTTP 200, 스트림 error 이벤트): ...`). 어댑터는 `crashed` 로 기록하되 그때까지 쓴 `raw/scan.jsonl` 은 남긴다.
- 프로젝트마다 커버리지 `files_total`·`files_scanned`·`files_failed`·`files_skipped` 를 manifest 에 기록하고 리포트의 프로젝트별 표에 `40/53` 으로 보인다. "Partial failures (부분 실패)" 절은 스캔하지 못한 파일이 있는 프로젝트(스캔 수 < 전체, 또는 실패·건너뜀 > 0)만 보이고, 메모에 실패한 파일(최대 5개)이 있다. 파일을 다 스캔했는데 메모만 있는 프로젝트(예: 후보 탈락 안내)는 같은 절의 "Agent notes" 목록에 따로 나온다. 일부 파일만 못 읽은 프로젝트는 `ok` 로 지표 분모에 들어간다(정책은 사용자 결정 대기). SCABench truth 에는 파일 위치가 없어 건너뛴 파일에 있던 truth 도 Recall 에서 미탐으로 계산된다.
- 실행에 쓴 scan.py 와 로드한 기준 문서를 `<run_dir>/agent_snapshot/`(`tools/scan.py`, `docs/<기준>.md`)에 한 번 복사한다. `internal/` 은 git 밖이라 나중에 되살릴 수 없기 때문이다. 이미 있으면 바이트가 같은지 확인하고 다르면 그 프로젝트는 `crashed`. manifest 의 `agent.snapshot_dir` 에 경로가 남는다. 이 폴더는 내부 문서를 담으므로 커밋하지 않는다(`eval/runs/*/*` 는 `manifest.json` 만 빼고 ignore). `--out` 을 `eval/runs/` 밖으로 주면 이 규칙이 안 닿으니 그 폴더는 직접 관리한다. manifest 의 `agent.scan_py_sha256`, `prompt_sha256`, `prompt_hash` 는 이 사본에서 계산하고, `--model`/`--effort` 를 안 준 때의 `agent.model`, `agent.effort` 도 사본 scan.py 의 기본값을 읽는다(D-26, 준 값이 우선). run 이 도는 동안 라이브 scan.py 를 고쳐도 manifest 는 실제로 시작한 코드를 가리킨다. 그전 run(2026-10-21 `pilot-secondswap-fable` 포함)의 `scan_py_sha256` 은 manifest 를 쓰는 시점의 라이브 파일 해시일 수 있어서 `agent_snapshot/tools/scan.py` 의 해시와 대조해야 한다. `eval rescore` 도 run 디렉터리에 `agent_snapshot/` 이 있으면 그 사본으로 같은 값을 계산하고 manifest `agent.fingerprint_source` 에 `snapshot` 을 적는다. 사본이 없거나 사본 scan.py 가 파싱되지 않으면 라이브 파일 기준(`live`)이고 rescore manifest 에는 `timeout_s` 가 없다(DECISIONS D-26 §5).
- 비웹 스캔은 디렉터리 이름이 `test`, `tests`, `ignition`, `script`, `scripts`, `deploy`, `mocks` 인 폴더의 파일을 보내지 않는다(D-26). root 기준 상대경로의 디렉터리 이름이 소문자로 정확히 같으면 깊이와 상관없이 뺀다(`contracts/test/` 도 뺀다. `Test/`, `testing/`, `test.sol` 은 안 뺀다). `--web` 은 이 폴더를 빼지 않는다. 같은 저장소라도 2026-10-21 이전 run 과 파일 집합이 다르다(secondswap 은 36개에서 16개). `./eval compare` 는 이 차이를 경고하지 않는다. 비교하기 전에 두 manifest 의 `projects[].files_total` 을 사람이 대조한다(`agent` 의 model, effort, max_tokens, call_shape 차이는 compare 가 경고한다, D-28 재검증 2). 선택 결과가 0개인 프로젝트는 `no_files` 로 기록하고 `crashed` 와 따로 센다(D-27, 아래 "지표 정의"). API 어댑터는 scan.py 가 exit 1 과 stderr 의 `스캔할 파일이 0개입니다` 로 끝난 것을 읽고(D-27 은 scan.py 를 안 바꿨고 D-28 은 이 메시지에 `--web` 의 크기 상한 사유만 더했다), CLI 어댑터는 `select_files` 결과가 비면 같은 상태를 기록한다. `--web` 에서 후보 파일이 전부 `MAX_BYTES_WEB`(100,000바이트)를 넘어 0개가 되면 `error` 가 `no files selected (크기 상한 초과: ...)` 로 시작해 "지원하는 확장자가 없음"과 구분된다(D-28). 데이터셋 29개 중 0개인 프로젝트는 둘이다. `code4rena_cabal-liquid-staking-token_2025_05`(정답 8건, D-26 의 제외 규칙으로 1개에서 0개가 됐다. in-scope 파일이 전부 `.move`)와 `code4rena_coded-estate-invitational_2024_12`(정답 33건, 원래 0개. 소스가 `.rs` 15개). 합 41건이 `no_files` 로 빠진다(DECISIONS D-26 §4, D-27).
- 스캐너는 파일만 후보로 집는다(`os.path.isfile`). Foundry 의 `out/KHYPE.sol/` 처럼 디렉터리 이름이 `.sol` 로 끝나도 제외한다. 비웹 스캔은 빌드 산출물 폴더 `out`, `artifacts`, `cache`, `build`, `dist`, `typechain`, `typechain-types` 도 뺀다(`--web` 은 `artifacts`, `cache`, `typechain`, `typechain-types` 를 더했고 `dist`, `build`, `out` 은 원래 뺐다). 2026-10-25 이전 run 의 `code4rena_kinetiq_2025_07` 은 `files_total` 25 에 `files_failed` 8(디렉터리를 읽다 `Permission denied`)이고 실제 소스는 17개다. 커버리지는 17/17 로 읽는다(D-30).

`--agent attacktrail-cli` 는 키 대신 로그인된 Claude Code CLI(`claude -p`)로 같은 스캔을 돌린다. scan.py 의 ROLE·SCHEMA·기준 문서·`verify()` 를 import 해서 쓰고, 모델 호출만 파일당 `claude -p` 1회로 바꾼다. 차이와 주의점:

- 툴·MCP·스킬·설정·세션 저장을 끈 상태로 호출한다(`adapters/attacktrail_cli.py` 의 `CLI_FLAGS`). 기본 설정이면 호출마다 Claude Code 시스템 프롬프트 약 5.5만 토큰이 붙는다(2026-10-13 실측, "OK" 한 마디에 $0.219). 끈 상태에서는 고정 오버헤드가 입력 약 700토큰이다.
- 하위 프로세스에는 API 키를 넘기지 않는다. CLI 로그인을 찾을 수 있게 사용자 프로필 경로 변수(`USERPROFILE`, `APPDATA` 등)만 넘기고, 빈 임시 디렉터리에서 실행한다.
- `cost_usd` 는 CLI 의 `total_cost_usd` 합계다. API 정가 환산값(`costBasis: list`)이고 구독 청구액이 아니다.
- scan.py 와 같은 조건이 아니다. system 블록 캐시가 없고, effort 와 서버 측 폴백을 보내지 않으며(`claude -p --model <모델>` 만. 모델은 scan.py `--model` 기본값을 따라 `claude-fable-5-1`. CLI 가 이 모델 이름을 받는지는 실제 호출로 확인하지 못했다), 구조화 출력은 CLI `--json-schema` 를 거친다. manifest `agent.effort` 는 `n/a` 이고 폴백 응답 수는 기록하지 않는다(미측정). `./eval run --agent attacktrail-cli --effort ...` 는 거부하고, 생성자에 `effort=` 를 넘겨도 `ValueError` 다. 기본 프로젝트 timeout 은 1800초다. API 경로(`attacktrail`)와 비교할 때는 `./eval compare` 로 따로 확인한다.
- 동시 호출은 4개로 고정(`WORKERS`)이다.
- 파일 읽기나 프롬프트 조립이 실패해도(`.sol` 이름의 디렉터리, 읽기 권한 오류) 그 파일만 `ERROR` 다. 프로젝트 전체가 crashed 가 되지 않는다(D-20).
- `_usage` 행의 `input_tokens` 는 세 종류를 더한 값이다(기존 그대로). 원값은 `input_tokens_uncached`, `cache_creation_input_tokens`, `cache_read_input_tokens` 로 따로 남는다. API 행의 `input_tokens` 는 캐시 미사용 입력만이라 뜻이 다르다(D-20 §6 표).
- 커버리지와 `agent_snapshot` 은 API 어댑터와 같다. 호출 방식은 그대로라 `call_shape` 는 `cli-v1` 이다.
- Ctrl-C(D-21 H-2): 호출은 `subprocess.run` 대신 `Popen` 으로 하지만 명령줄·stdin·env 는 그대로다. 결과는 0.5초 단위로 폴링하고, 인터럽트가 오면 큐의 파일을 취소하고 실행 중인 `claude` 프로세스를 죽인 뒤 KeyboardInterrupt 를 그대로 다시 올린다(eval 안에서 돌기 때문에 `os._exit` 는 쓰지 않는다). 한계: 러너(`runner/run.py` `_single`)가 에이전트를 스레드 풀에서 돌려서 `eval run` 으로 돌릴 때는 KeyboardInterrupt 가 어댑터에 닿지 않는다. 러너의 같은 문제는 아직 안 고쳤다(DECISIONS D-21 "아직 안 된 것").
- 시스템 프롬프트를 명령줄 인자(`--system-prompt`)로 넘긴다. 명령줄이 Windows 한도 32,767자를 넘으면 호출 전에 `crashed`로 멈춘다. 비-web 은 약 7,300자로 안쪽이다. 웹 기준 문서(AUD-WEB-01, AUD-CHK-02)를 넣은 `web=True` 는 약 22만 자라 이 어댑터로 돌릴 수 없고, `attacktrail`(API) 어댑터를 쓴다 (DECISIONS D-15). eval CLI 에는 아직 `--web` 플래그가 없다.

## 사용법

eval/ 에서 실행한다. Windows 는 `.\eval.cmd`, POSIX 셸(Git Bash 포함)은 `./eval`. 둘 다 `.venv` 파이썬으로 `cli.py` 를 부른다.

### run

```
./eval run --agent {attacktrail,attacktrail-cli,baseline} --dataset {scabench,holdout} --out DIR
           [--projects a,b] [--suite NAME] [--repeat N] [--jobs N] [--timeout S]
           [--judge {nethermind,mock}] [--judge-model M]
           [--baseline-mode {precomputed,live}] [--model M] [--effort {high,xhigh,max}] [--max-tokens N]
           [--i-understand-holdout]
```

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--projects` | 전체 | 쉼표로 구분한 project_id. `--suite` 보다 우선 |
| `--suite` | 없음 | `suites/<NAME>.txt`. 한 줄에 project_id 하나, `#` 줄은 무시 |
| `--repeat` | 1 | N>1 이면 `DIR/rep-<k>/` 로 N번 돌리고 평균과 표본 표준편차를 낸다 |
| `--jobs` | 2 | 프로젝트 병렬 수 |
| `--timeout` | 에이전트 기본값 | 프로젝트당 에이전트 제한 시간(초). 생략하면 `attacktrail` 10800, 그 밖 1800 (D-24) |
| `--judge` | nethermind | `mock` 은 배관 테스트 전용. 아래 참고 |
| `--judge-model` | gpt-5-nano-2025-08-07 (o4-mini 는 2026-10-24 에 폐기됨. D-31 에서 변경) | 채점기 `MODEL` |
| `--baseline-mode` | precomputed | `precomputed` 는 upstream 이 커밋해 둔 GPT-5 결과 파일을 읽는다. 네트워크·키 불필요 |
| `--model` | 에이전트 기본값 | 에이전트에 넘길 모델. AttackTrail 은 `claude-haiku-5-5` (D-34. `claude-fable-5-1`, `claude-sonnet-5-5`, `claude-opus-5-5` 도 된다) |
| `--effort` | scan.py 기본값(`xhigh`) | `output_config.effort`. `high`·`xhigh`·`max`. `--agent attacktrail` 에서만 쓸 수 있다 |
| `--max-tokens` | scan.py 기본값(64000) | 요청 `max_tokens`(thinking 포함 출력 상한). 1~128000. `--agent attacktrail` 에서만. manifest `agent.max_tokens` 와 리포트 Manifest 표에 남는다. 64000 보다 크면 호출별 시간 상수가 비례해 커진다(D-28) |

run 이 끝나면 stderr 에 요약이 나온다(stdout 은 run 경로 한 줄 그대로). 리포트의 "Partial failures" 절은 그대로 두고, 콘솔에는 따로 보인다(D-23. 네 번째는 D-27).

- 부분 실패 프로젝트: 프로젝트명, 실패/전체 파일 수(건너뜀이 있으면 함께), 커버리지. 커버리지를 기록하지 않은 프로젝트는 `커버리지 미측정`.
- agent 가 `ok` 로 끝나지 못한 프로젝트(`crashed`, `timeout`)와 사유 한 줄. 치명적 API 오류(401·402·403·404)나 연속 API 오류 차단기로 scan.py 가 멈추면 여기 `crashed` 로 나온다. 동작은 그대로다. `no_files` 는 여기 나오지 않는다.
- 폴백 응답이 있는 프로젝트: `폴백 응답 12 파일 (모델 claude-opus-5)`. 폴백 수를 기록하는 에이전트(API 어댑터)만 보이고, 리포트에는 "Fallback responses (폴백 응답)" 절로 같은 내용이 나온다.
- 채점 불완전(D-28): `채점 불완전: <프로젝트> 미평가 정답 N건(채점 로그 확인)` 한 줄씩. 채점 결과에서 `truths_unevaluated` 가 0 보다 크고 에이전트 지적(manifest `n_findings`)이 1건 이상인 프로젝트가 있을 때만 나온다. 지적이 0건인 프로젝트는 judge 호출이 없어서 켜지지 않는다. 리포트에는 Headline 표 바로 아래에 같은 줄이 있고 `scores.json`·manifest 에 `judge_incomplete: true` 와 `judge_incomplete_projects` 가 남는다. 채점기 OpenAI 호출이 `Connection error`·`Request timed out` 으로 실패하면 그 정답이 건너뛰어져 생기고(`scoring_output/scorer_stdout.log` 에서 센다), 지적을 매칭으로 다 쓴 프로젝트에도 같은 줄이 나온다. 그래서 줄은 단정이 아니라 로그를 보라는 신호다. 매칭 소진이면 rescore 해도 결과가 같고 OpenAI 비용만 드니 먼저 `scorer_stdout.log` 의 오류 줄을 센다. 복구는 아래 "rescore" 절.
- 지원 밖 프로젝트(`no_files`): `지원 밖 프로젝트: <프로젝트> (정답 8)` 한 줄. 있을 때만 나온다(D-27). 리포트에는 헤드라인 바로 아래 "Unsupported projects (지원 밖 프로젝트)" 절이 생기고, 프로젝트별 표의 Status 는 `no_files (지원 밖)` 로 보인다(HTML 은 주황 이탤릭, `crashed`·`timeout` 은 빨강).

예시. 키 없이 끝까지 돌아가는 배관 테스트:

```powershell
.\eval.cmd run --agent baseline --dataset scabench --projects sherlock_axion_2025_01 --judge mock --out runs\plumbing
```

실제 채점(유료 호출 발생, `OPENAI_API_KEY` 필요. 키가 없으면 클론이나 에이전트 호출 전에 바로 멈춘다):

```powershell
.\eval.cmd run --agent baseline --dataset scabench --projects sherlock_axion_2025_01,sherlock_tally_2024_12 --out runs\baseline-p1
```

holdout 은 기본 실행에 절대 들어가지 않는다. `--dataset holdout` 에 `--i-understand-holdout` 이 없으면 다음 문구를 찍고 exit 2 로 끝난다.

```
holdout 셋은 마일스톤 평가용입니다. 튜닝에 쓰면 오염됩니다. 진행하려면 --i-understand-holdout 을 붙이세요.
```

플래그를 붙이면 같은 문구를 stderr 에 찍고 진행하며, manifest 에 `holdout_used: true` 가 남는다.

출력 디렉터리:

```
DIR/manifest.json                         실행 조건 전체 (키·환경변수 덤프는 남기지 않는다)
DIR/scores.json                           채점 결과와 지표 정의
DIR/report.html, DIR/report.md            리포트
DIR/projects/<id>/findings.json           에이전트 출력 (내부 스키마)
DIR/projects/<id>/checkout.json           체크아웃 결과
DIR/agent_snapshot/                       AttackTrail 어댑터가 쓴 scan.py 와 기준 문서 사본 (내부 문서, 커밋 안 됨)
DIR/scoring_data/source_of_truth/<id>.json
DIR/scoring_data/<agent>/<id>_results.json  채점기 입력
DIR/scoring_output/                       채점기 원출력
```

`runs/` 는 `manifest.json` 만 커밋 대상이다. `agent_snapshot/` 은 `internal/` 의 기준 문서 사본이라 커밋되면 안 되고, `.gitignore` 의 `eval/runs/*/*` 규칙으로 막힌다(`tests/test_adapters.py` 가 `git check-ignore` 로 확인한다).

### rescore

```
./eval rescore RUN --agent {attacktrail,attacktrail-cli,baseline} --dataset scabench
               [--projects a,b] [--suite NAME] [--judge {nethermind,mock}] [--judge-model M]
               [--model M] [--effort {high,xhigh,max}] [--max-tokens N]
```

에이전트 단계는 끝났는데 채점이 끊기거나 불완전한 run 을 마무리한다. 에이전트는 다시 돌지 않고 비용은 채점기의 OpenAI 호출뿐이다(`OPENAI_API_KEY` 필요). `RUN/manifest.json` 이 이미 있으면 거부한다. `projects/<id>/{checkout,findings}.json` 과 `scoring_data/` 를 읽고, `scoring_output/<id>_results.json` 이 없는 프로젝트만 채점한다. `--model`·`--effort`·`--max-tokens` 는 manifest 기록용이다. attacktrail API 어댑터에서는 원래 run 이 `projects/<id>/raw/scan.jsonl` 첫 줄 `_run` 에 남긴 model, effort, max_tokens 와 대조한다. 다른 값을 주면 `재채점 중단: --effort xhigh 는 원래 run 의 effort 값(max)과 다르다 ...` 처럼 사유를 내고 exit 1 로 거부하고, 아무것도 쓰지 않는다. 안 준 항목은 `_run` 값을 manifest 에 적는다(scan.py 기본값 xhigh, 64000 이 아니다). 프로젝트마다 `_run` 이 서로 다른 값을 기록했으면 라벨을 정할 수 없어 이것도 거부한다. `_run` 행이나 `scan.jsonl` 이 없는 run(CLI 어댑터, baseline, 옛 scan.py)은 전과 같다. 준 값을 쓰고, 안 주면 run 의 `agent_snapshot` 사본 scan.py 기본값이 적힌다(D-28 재검증 1). attacktrail API 어댑터인데 `_run` 에서 model, effort, max_tokens 중 못 찾은 항목이 있으면(`scan.jsonl` 이 없거나 `_run` 행에 그 키가 없는 산출물일 때. 선택한 프로젝트 중 하나라도 그 키를 기록했으면 그 값으로 채우고 경고하지 않는다. 현재 `runs/` 의 run 은 세 키가 다 있어 이 줄이 뜨지 않는다) stderr 에 `rescore: <run 폴더 이름> 의 _run 행에서 max_tokens 를 찾지 못해 인자 또는 기본값으로 기록한다` 한 줄을 낸다. 그 항목의 manifest 라벨은 확인되지 않은 값이다(D-29 리뷰 후 수정 R1). 끝에 `eval run` 과 같은 요약을 stderr 에 낸다(D-28).

채점 불완전 시 복구 절차. 위 "채점 불완전" 줄이 나왔고 `scorer_stdout.log` 에 `Connection error` 같은 오류가 있으면 run(또는 `--repeat` 의 `rep-k`) 사본에서 다시 채점한다. 원본은 건드리지 않는다.

```powershell
cd eval
Copy-Item -Recurse runs\<run> runs\<run>-r1            # repeat 면 runs\<run>\rep-3 처럼 불완전한 rep 만 복사
Remove-Item runs\<run>-r1\manifest.json, runs\<run>-r1\scores.json, runs\<run>-r1\report.html, runs\<run>-r1\report.md
Remove-Item -Recurse runs\<run>-r1\scoring_output       # 이게 남아 있으면 rescore 가 그 프로젝트를 건너뛴다
.\eval.cmd rescore runs\<run>-r1 --agent attacktrail --dataset scabench --projects <원래 run 의 --projects> --judge nethermind
```

`--projects`(또는 `--suite`)는 원래 run 과 같은 범위로 준다. 일부만 주면 manifest, overall, 리포트에 그 프로젝트만 들어가고, 아예 빼면 데이터셋 전체가 선택돼 `projects/` 에 결과가 없는 프로젝트가 `crashed`(`rescore: no findings.json`)로 기록된다. `--model`, `--effort`, `--max-tokens` 는 안 줘도 된다. 사본의 `projects/<id>/raw/scan.jsonl` 첫 줄 `_run` 값이 manifest 에 들어간다(위 rescore 절). 주면 그 값과 같아야 한다.

`projects/`, `scoring_data/`, `agent_snapshot/` 은 그대로 둔다. `--repeat` 합본 `scores.json` 의 평균·표준편차는 불완전한 rep 를 그대로 품고 있으니 쓰지 않는다. 정상 rep 와 재채점한 rep 의 값으로 평균과 표본 표준편차를 다시 낸다.

### compare

```
./eval compare RUN_A RUN_B
```

마크다운을 출력하고 `RUN_B/compare-vs-<RUN_A 이름>.md` 에도 쓴다.

- 한쪽만 mock judge 면 비교를 거부한다(exit 1).
- 데이터셋 sha, 채점 모델, 채점기 커밋, batch size 가 다르면 크게 경고한다.
- 양쪽에서 채점된 프로젝트 집합이 다르면(예: B 에서 에이전트가 crash 해 분모에서 빠짐) 차이를 나열해 경고하고, 양쪽 모두 채점된 프로젝트만으로 계산한 Recall/High Recall 표를 따로 보인다.
- 두 run 의 `no_files` 프로젝트 집합이 다르면(예: 한쪽은 D-26 제외 규칙 전의 run 이라 cabal 이 `ok` 로 스캔됨) 경고 한 줄을 더한다(D-27). repeat run 은 각 `rep-k/manifest.json` 의 합집합으로 본다.
- REGRESSIONS: A 에서 엄격 매칭됐는데 B 에서 안 된 truth. `(project_id, finding_id)` 로 짝짓는다. 채점된 프로젝트에서 채점기가 판정하지 않은 truth(지적 0건 등)는 매칭 안 됨으로 센다.
- 어느 쪽이든 repeat 표준편차가 있으면, 큰 쪽 표준편차보다 작은 변화는 노이즈 범위로 표시한다.
- 두 manifest 의 `agent.model`, `agent.effort`, `agent.max_tokens`, `agent.call_shape` 가 다르면 항목마다 경고 한 줄(`agent.effort differs: A=xhigh B=max`)(D-28 재검증 2). repeat run 은 합본 manifest 의 `agent` 를 읽는다. 한쪽이 그 키를 기록하지 않았으면 그쪽이 `미기록` 으로 나오고, 실제 값이 같아도 경고한다(D-28 전 manifest 에는 `max_tokens` 가 없다). 양쪽 다 기록하지 않았어도 `agent.max_tokens: 양쪽 미기록` 처럼 키마다 한 줄을 낸다(같게 설정됐다는 증거가 없어서다. baseline 은 effort, max_tokens, call_shape 를 안 적으므로 baseline 끼리 비교해도 세 줄이 뜬다, D-29 리뷰 후 수정 R2). 요청 본문의 구조가 바뀌면 `call_shape` 를 올리고 model, effort, max_tokens 는 manifest 필드에 기록하는 규칙이라 이 넷을 같이 본다.
- 경고하지 않는 것: `prompt_sha256` 이 달라도, 에이전트가 스캔한 파일 집합이 달라도 경고가 없다. D-26 전후의 secondswap run 은 36파일과 16파일이다. 두 manifest 의 `agent.prompt_sha256` 과 `projects[].files_total` 을 사람이 대조한다.

### render

```
./eval render RUN
```

`manifest.json` 과 `scores.json` 으로 `report.html`(외부 자원 없음)과 `report.md` 를 다시 만든다.

## 지표 정의

채점기 `RepoStats` 가 계산한 값을 그대로 쓴다. 우리가 다시 계산하지 않는다.

- 엄격(strict): 정확 매칭(`is_match`)만 센다. `precision`, `recall`, `f1`.
- 관대(lenient): 정확 매칭 + 부분 매칭(`is_partial_match`). `precision_with_partial`, `recall_with_partial`, `f1_with_partial`.
- Recall (1차 지표): 채점기가 매칭한 truth / 데이터셋 truth 파일의 truth 수. 엄격·관대 두 벌(`recall_vs_dataset`, `recall_with_partial_vs_dataset`). 채점기는 비교할 지적이 남지 않았거나(지적 0건, 또는 매칭으로 다 소진) judge 호출이 전부 실패한 truth 를 건너뛰고 자기 분모에서도 뺀다. 리포트는 이런 truth 를 미탐으로 세고 `Truths not judged by scorer` 행에 개수를 보인다. 채점기 자신의 `recall` 은 `scores.json` 의 per_project 에 그대로 남는다. 어느 truth 가 건너뛰어졌는지는 `scoring/truth_trace.py` 가 채점기 호출을 관찰만 해서 `scoring_output/<id>_truth_trace.json` 에 기록한다.
- High Recall: High truth 에 대한 recall. 채점기가 critical 을 High 로 접으며, 이 데이터셋에는 critical 이 0건이다(high 114건). 분모는 데이터셋 truth 파일에서 채점기의 `normalize_severity` 로 High 인 truth 수이고 함께 표시한다. 분모가 0이면 값은 `None`, High truth 가 있는데 하나도 판정되지 않았으면 0 이다. 분자는 채점기가 평가한 항목 중 `severity_from_truth == "High"` 이면서 매칭된 것만 센다.
- Precision (2차 지표, 감사자 시간 비용): 매칭된 에이전트 지적 / 에이전트 지적. Info, Best Practices 등급 지적은 채점기가 FP 계산에서 뺀다. 이런 지적 수는 `info_findings` 로 따로 보고한다.
- 비용: 프로젝트당 평균 USD, 평균 벽시계 초. 측정되지 않은 값은 `null` 이고 리포트에는 `미측정` 으로 나온다. 0으로 채우지 않는다.
- 분모 규칙: 체크아웃 실패, 에이전트 crashed/timeout, `pinned_commit_missing` 으로 빠진 프로젝트는 지표 분모에서 제외하고 사유와 함께 따로 나열한다.
- 에이전트 상태(`agent_status`): `ok` 는 채점되고 분모에 들어간다. `crashed`·`timeout` 은 실패라 분모에서 빠지고 "Excluded / failed projects" 에 사유와 함께 나온다(빠진 프로젝트의 정답 수는 따로 세지 않는다. `no_files` 만 센다). `no_files`(D-27)는 스캐너가 지원하는 확장자(`.sol .ts .js .py .go`)의 파일을 하나도 못 찾은 프로젝트다. 실패가 아니라 도구의 한계라서 `crashed` 와 따로 센다. `build_failed` 는 값만 정의돼 있고 쓰는 어댑터가 없다.
- 헤드라인과 전체 포함(D-27): 리포트 Headline 과 `scores.json` 의 `overall` 은 `ok` 로 채점된 프로젝트만의 값이다. `no_files` 프로젝트의 정답은 그 분모에 없다. 따로 `scores.json`·manifest 의 `unsupported` 에 지원 밖 프로젝트 수, 정답 수, 그 정답을 전부 미탐으로 넣은 Recall 이 있다. 엄격 `matched / (채점된 프로젝트의 정답 + 지원 밖 정답)`, 관대 `(matched + partial) / 같은 분모`(`recall_vs_dataset_incl_unsupported`, `recall_with_partial_vs_dataset_incl_unsupported`, 분모 `truths_in_dataset_incl_unsupported`)이고 `line` 에 사람이 읽는 한 줄이 있다. High Recall 은 전체 포함 값을 만들지 않았다. 채점된 프로젝트가 없으면 `overall` 은 `None` 이고 전체 포함 Recall 은 0 이다. 분모는 채점된 프로젝트의 정답과 지원 밖 정답의 합이고, 체크아웃 실패·`pinned_commit_missing`·`crashed`·`timeout` 프로젝트의 정답은 들어가지 않는다(`line` 에도 적힌다). `--repeat` 합본 폴더의 리포트에는 이 절이 없고 각 `rep-k/` 에 있다.
- repeat: 헤드라인 지표마다 평균과 표본 표준편차. 반복 1회면 표준편차는 `None`.

채점기는 truth 하나에 대해 지적을 `BATCH_SIZE`(기본 10)개씩 묶어 LLM 판정을 3회(`ITERATIONS=3`, 모듈 상수라 바꿀 수 없다) 받는다. 판정이 LLM 이므로 같은 입력에도 결과가 흔들릴 수 있다. 비교할 때는 `--repeat` 를 쓴다.

## 위협 모델

평가 대상은 남의 저장소다. 악의적일 수 있다고 가정한다.

현재 하네스는 대상 파일을 읽기만 하고, 대상 코드를 실행하지 않는다. `npm install`, `forge build`, `forge test`, `cargo build`, `hardhat` 등 어떤 빌드·테스트도 돌리지 않는다.
PoC 실행이나 forge 실행을 붙이기 전에 Docker 격리가 반드시 먼저 있어야 한다. `runner/executor.py` 의 `DockerExecutor` 는 그때까지 `NotImplementedError` 를 던진다. 격리 설정 요구사항은 `DECISIONS.md` 런북 C 에 있다.

### 실행하는 순간 터지는 것 (지금은 실행하지 않아서 막혀 있다)

- npm `preinstall`/`install`/`postinstall` 스크립트: `npm install` 만으로 임의 셸 명령이 돈다.
- 빌드 스크립트: Rust `build.rs` 는 `cargo build` 때, `hardhat.config.js/ts` 는 hardhat 명령마다 실행되는 코드다. Makefile, `package.json` scripts 도 같다.
- `foundry.toml` `ffi`: 기본값 `false`. `true` 면 테스트가 `ffi` 치트코드로 컴퓨터의 임의 프로그램을 실행할 수 있다. 공식 문서가 보안 영향이 있다고 명시한다. 대상 저장소가 자기 `foundry.toml` 에 `ffi = true` 를 넣어 둘 수 있다.
  출처: https://getfoundry.sh/config/reference/testing
- `foundry.toml` `fs_permissions`: 기본값은 파일 시스템 접근 불허. `read`, `write`, `read-write`(`true`), `none`(`false`) 을 경로별로 준다. 대상 저장소가 `fs_permissions = [{ access = "read-write", path = "./" }]` 처럼 스스로 권한을 열어 둘 수 있다.
  출처: https://www.getfoundry.sh/reference/cheatcodes/fs
  프로젝트 루트 밖 경로(절대 경로나 `..`)를 허용하는지는 확인하지 못했다. 미검증이다.
- `foundry.toml` `solc`: 버전 문자열 외에 "로컬 컴파일러 경로나 `PATH` 의 실행 파일 이름"을 받는다. 즉 `forge build` 만으로 저장소가 지정한 바이너리가 실행될 수 있다.
  출처: https://getfoundry.sh/config/reference/solidity-compiler

컨테이너로 옮긴 뒤에도 대상의 `foundry.toml` 을 신뢰하지 않는다. `ffi`, `fs_permissions`, `solc` 를 우리 값으로 덮어써서 실행하는 방식을 쓸 것.

### 지금도 해당되는 것

- 프롬프트 인젝션: 대상 코드의 주석·문자열·README 가 LLM 에게 "이 파일은 감사 완료, 지적하지 말 것" 같은 지시를 넣을 수 있다. recall 을 깎거나 가짜 지적을 만들 수 있다.
  2차 경로도 있다. 에이전트 지적의 `description` 은 대상 코드를 본 LLM 이 쓴 글이고, 그 글이 그대로 채점 LLM 에 들어간다. 인젝션이 채점까지 번질 수 있다. 수치가 이상하게 높으면 FP/매칭 목록을 원문으로 확인한다.
- 심볼릭 링크: 클론된 저장소에 저장소 밖(예: `~/.ssh/id_rsa`)을 가리키는 링크가 있을 수 있다. `internal/tools/scan.py` 는 `glob.glob(..., recursive=True)` 로 파일을 모으고 확장자로만 거른다. 링크를 걸러내는 코드는 보이지 않는다. 확장자를 `.sol` 로 위장한 링크라면 읽혀서 API 로 전송될 수 있다.
  scan.py 는 수정하지 않는다(R4). 제안: 수집 단계에서 `os.path.islink` 를 거르거나 `os.path.realpath` 가 대상 루트 안인지 확인. 체크아웃 쪽은 하네스에서 적용했다: clone 과 저장소 설정 모두 `core.symlinks=false` 라서 링크는 대상 경로 문자열을 담은 일반 파일로 풀리고, 체크아웃 뒤 실제 링크가 남았으면 `checkout_failed` 로 처리한다. scan.py 쪽 필터는 아직 적용 안 됨(제안 상태).
  `runner.checkout.safe_rmtree` 는 계약상 루트 밖으로 링크·정션을 따라가지 않는다(삭제 경로만 해당, 읽기 경로는 위 제안 참고).
- `.gitmodules`: 서브모듈은 초기화하지 않는다. 대상이 지정한 임의 URL 을 받으러 가지 않는다는 뜻이다. 대신 `lib/` 등에 있어야 할 의존 코드가 비어 있다(아래 한계 참고).
- Git LFS: `GIT_LFS_SKIP_SMUDGE=1` 로 LFS 객체를 받지 않는다. 해당 파일은 포인터 텍스트로 남는다. `code4rena_superposition_2025_01` 은 기본 브랜치 HEAD 의 LFS 객체가 서버에서 404 라서 이 설정 없이는 클론이 실패한다(실측).
- git hooks: `git clone` 은 원격 저장소의 `.git/hooks` 를 가져오지 않는다. 저장소 쪽 `.git/config` 도 복사되지 않는다. 그래서 scan.py 가 대상에서 `git rev-parse HEAD` 를 부르는 것은 대상이 심은 훅이나 설정을 실행하지 않는다. 우리 쪽 전역 git 설정(`core.hooksPath` 등)은 대상과 무관하다.
- 에이전트 서브프로세스 환경변수: executor 는 환경변수를 상속하지 않는다. AttackTrail 에는 `ANTHROPIC_API_KEY`(값은 `ATTACKTRAIL_ANTHROPIC_API_KEY` 가 있으면 그것, 없으면 `ANTHROPIC_API_KEY`. 별칭 이름은 안 넘긴다)와 최소 OS 변수(`PATH`, `SYSTEMROOT`, `TEMP`/`TMP`, `PYTHONIOENCODING`)만, baseline live 에는 `OPENAI_API_KEY` 만 넘긴다. manifest 에는 키와 환경변수 덤프를 쓰지 않는다.

## 알려진 한계

- SCABench 는 이미 알려진(공개 감사 리포트로 확정된) 버그만 truth 로 갖는다. 에이전트가 찾은 진짜 신규 버그는 FP 로 집계된다. precision 은 하한으로 읽는다.
  또 공개 리포트가 모델 학습 데이터에 들어갔을 수 있다. 이 경우 recall 이 부풀려진다. holdout 셋이 필요한 이유다.
- truth 에 파일·라인 위치가 없다. 항목은 `finding_id, severity, title, description` 뿐이다. 그래서 채점기 입력의 truth `Contracts` 는 `[]`, `Category` 는 `Other` 다. 매칭은 제목·본문 의미로만 이뤄지고, FN 목록에도 위치가 없다. 위치를 알려면 truth 본문을 읽어야 한다.
- 31개 프로젝트 중 29개만 쓴다. `code4rena_initia-move_2025_04`, `code4rena_starknet-perpetual_2025_06` 은 데이터셋의 `commit` 이 빈 문자열이다. 고정 커밋이 없으면 기본 브랜치 HEAD 를 보게 되고 재현이 안 되므로 `pinned_commit_missing` 으로 기록하고 제외한다. 수치를 낼 때 29개 기준임을 밝힌다.
  남은 29개도 전부 고정 커밋은 아니다. 데이터셋 `commit` 값을 직접 확인했다.

  ```
  40자 전체 sha     21개
  7자 짧은 sha       3개   sherlock_oku_2024_12, sherlock_idle-finance_2024_12, sherlock_symmio_2025_03
  브랜치 이름 main   5개   code4rena_fenix-finance-invitational_2024_10, code4rena_lambowin_2025_02,
                          code4rena_loopfi_2025_02, code4rena_bakerfi-invitational_2025_02,
                          code4rena_blackhole_2025_07
  빈 문자열          2개   (위의 제외 2건)
  ```

  `main` 5개는 기본 브랜치 HEAD 를 보므로 빈 커밋과 같은 재현성 문제가 있다. 짧은 sha 3개는 `rev-parse HEAD == commit` 비교가 40자 대 7자라서 접두사 비교를 하지 않으면 체크아웃 검증에서 떨어진다.
  이 8건을 어떻게 다룰지(제외, 접두사 허용, sha 확정 후 기록)는 아직 정하지 않았다.
- 서브모듈을 초기화하지 않으므로 의존 라이브러리 코드가 없다. truth 버그가 그 안에 있으면 에이전트는 원천적으로 못 찾는다. scan.py 는 비웹 스캔에서 `lib` 디렉터리를 아예 제외한다(D-26 부터 `test`, `script(s)` 등도 같이 제외).
- mock judge 수치는 의미가 없다. `--judge mock` 은 진짜 `scoring_algo` 파이프라인을 돌리되 LLM 판정만 지적 내용과 무관한 결정적 값으로 바꾼다. 배관 테스트용이다. 리포트 맨 위에 빨간 배너가 붙고, mock 결과와 실제 결과는 비교가 거부된다.
- 비용 필드는 측정 전까지 `null` 이다. precomputed baseline 은 토큰 수만 있고 검증된 단가가 없어 `cost_usd` 가 `null` 이다. 채점기는 자기 LLM 토큰을 결과에 남기지 않는다. 채점 비용은 OpenAI 대시보드로만 잴 수 있다(`DECISIONS.md` 런북 A). 단가를 추정해서 채우지 않는다.
- precomputed baseline 은 upstream 이 GPT-5 로 만든 결과 파일이다. 우리가 같은 조건으로 재현한 값이 아니다. upstream `baseline_runner.py` 는 `--reasoning-effort` 를 파싱만 하고 API 에 넘기지 않는다.
- 채점 LLM 의 판정은 비결정적이다. 같은 run 을 다시 채점해도 값이 달라질 수 있다.

## holdout 프로젝트 추가

`datasets/holdout/README.md` 를 따른다. 요약하면:

1. `datasets/holdout/TEMPLATE.json` 을 `datasets/holdout/projects/<project_id>.json` 으로 복사해서 채운다. SCABench 항목 하나와 같은 형태다.
2. `commit` 은 40자 전체 sha. 빈 값은 거부된다.
3. 검증:

```powershell
.venv\Scripts\python.exe datasets\holdout\validate.py
```

## 테스트

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe -m pytest tests -q
```

어떤 테스트도 유료 API 를 호출하지 않는다.
