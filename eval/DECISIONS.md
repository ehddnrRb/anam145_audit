# eval 하네스 — 결정 기록

누적 기록. 새 항목은 아래에 덧붙인다. 지운 내용은 취소선으로 남긴다.

---

# Phase 0 — 정찰 (2026-09-17)

## 고정한 커밋

```
scabench                  eec0020    https://github.com/scabench-org/scabench
evmbench                  2d5c7ab    https://github.com/paradigmxyz/evmbench
auditagent-scoring-algo   a62a15d    https://github.com/NethermindEth/auditagent-scoring-algo
```

## D-1. 채점기는 Nethermind `scoring_algo` 를 그대로 쓴다

`scabench/scoring/scorer_v2.py` 는 upstream이 deprecated 선언했다. 지표도 재구현하지 않는다.
`generate_report.py:66-88` 이 엄격·관대 두 벌을 이미 계산하고 `RepoStats` (`core/types.py:68`) 에 담는다.

```
precision / recall / f1                             엄격 — is_match 만
precision_with_partial / recall_with_partial / f1   관대 — is_partial_match 포함
```

**실측 확인**: `pip install <clone>` 성공, `python -m scoring_algo.cli --help` 동작, 테스트 **94/94 통과** (API 키 없이, LLM 목).
`uv` 는 필요 없다. dev 의존성 중 `pytest-asyncio` 가 없으면 async 테스트 6건이 실패하므로 함께 설치한다.

## D-2. 어댑터 출력은 두 겹으로 만든다

우리 내부 스키마(`vulnerabilities[]`, `id`/`lines`/`confidence`/`evidence` 포함)를 진상으로 두고,
채점기에 넣을 때만 변환한다. AttackTrail 본체는 건드리지 않는다.

채점기가 실제로 읽는 키 (`core/storage.py:86` `_to_vulnerability`):

```
Issue        ← Issue | title | Title | issue
Description  ← Description | description      None 이면 문자열 "None" 이 된다. 반드시 채운다
Severity     ← Severity | severity → normalize_severity
Contracts    ← Contracts | file → _ensure_list (None 은 걸러짐)
Category     ← CategoryEnum 11개 값과 정확히 일치할 때만
```

`location` 과 `lines` 는 **읽지 않는다.** 우리 FN/FP 리포트용으로만 보관한다.

## D-3. High/Critical Recall 은 `high` 기준으로 정의한다

데이터셋 555건의 severity 분포를 직접 셌다.

```
high 114 · medium 237 · low 184 · informational 20      critical 0건
```

`critical` 값이 데이터에 존재하지 않는다. 채점기 `Severity` enum 에도 Critical 이 없고
`normalize_severity` (`storage.py:26`) 가 critical 을 High 로 접는다.
따라서 **High/Critical Recall = `high` 114건에 대한 recall** 이다. 리포트에 이 정의를 명시한다.

## D-4. `SCAN_SOURCE=attacktrail` 을 쓴다

`storage.py:15` 에서 경로 조각으로만 쓰이고 허용값 검증이 없다. `baseline` 으로 위장할 필요가 없다.

## D-5. 소스 체크아웃은 우리가 직접 한다

`checkout_sources.py` 는 쓰지 않는다. 최장 ID 프로젝트에서 `'NoneType' object is not subscriptable`
로 즉시 실패했다(2.5초, 클론 시도조차 안 함). `stderr[:200]` (`:118`) 계열의 버그로 보인다.
같은 프로젝트를 `git` 으로 직접 클론하면 정상 동작한다(아래 실측).

---

# Phase 0 실측 결과

## ① 데이터셋 저장소 접근성 — 해소

`git ls-remote` 로 32개 코드베이스 전수 확인. **접근 가능 32 / 실패 0.**

다만 **커밋이 빈 문자열인 프로젝트가 2건** 있다.

```
code4rena_initia-move_2025_04            commit=''
code4rena_starknet-perpetual_2025_06     commit=''
```

`checkout_sources.py:124` 가 `if commit:` 으로 감싸므로 이 둘은 커밋 체크아웃을 건너뛰고
기본 브랜치 HEAD 상태로 남는다. **재현성이 보장되지 않는다.**
→ 러너에서 빈 커밋을 감지하면 `status="pinned_commit_missing"` 으로 기록하고, 기본 실행에서 제외한다.

## ② Windows 경로 길이 — 해소

최악 사례(`sherlock_20240920---final---boost-core-...`, ID 78자)로 실측했다.

```
clone     exit=0   3.2초
checkout  exit=0   9.3초    ← --depth 50 에 커밋 없어 --unshallow 후 성공
결과      파일 254개 · .sol 68개 · 최장 경로 265자
```

**265자 > Windows 한계 260자.** `core.longpaths=true` 없이는 실패한다.

두 가지가 따라온다.

- Windows 에서 돌릴 때는 clone 과 저장소 양쪽에 `core.longpaths=true` 를 건다.
- `--depth 50` 은 충분하지 않다. 체크아웃 실패 시 `--unshallow` 재시도를 반드시 넣는다.
- 컨테이너(리눅스)로 가면 경로 길이 문제는 사라진다. 이것이 컨테이너를 쓸 또 하나의 이유다.

## ③ 채점 비용·시간 — **미해소 (OPENAI_API_KEY 없음)**

실측 대신 호출 수 상한을 실제 데이터로 계산했다.

```
호출 수 = (truth 수) × (배치 수) × 3        배치 수 = ceil(findings / BATCH_SIZE), BATCH_SIZE=10
                                            3 은 settings.py:7 하드코딩, 환경변수로 못 바꾼다
```

전체 31개 프로젝트 기준:

```
truth 합계            555
baseline findings 합계  1,115
LLM 호출 하한          1,665   모든 truth 가 첫 배치에서 조기 종료
LLM 호출 상한          8,223   조기 종료 없음
```

실제 값은 이 사이에 있다. 토큰당 단가와 왕복 지연은 측정해야 안다. 런북 A 참조.

## ④ scoring-algo 설치 — 해소

```
pip install <clone>       성공 (scoring-algo 0.1.0)
uv                        불필요
scoring-algo --help       동작. 명령 evaluate / report
pytest                    94/94 통과 (키 없이, LLM 목)
추가 필요                  pytest-asyncio (없으면 async 테스트 6건 실패)
```

## ⑤ evmbench `frontier-evals` 서브모듈 — 부분 해소, 추적 종료

`openai/frontier-evals` 로 등록되어 있고 클론은 되지만 체크아웃이 경로 길이로 실패한다
(`project/paperbench/tests/unit/fixtures/...`). PaperBench 등이 든 대형 모노레포다.

우리는 Nethermind 채점기로 가므로 **더 파지 않는다.** evmbench 에서 가져올 것은 격리 패턴뿐이다.

## ⑥ AttackTrail 출력 — **미해소 (ANTHROPIC_API_KEY 없음)**

`internal/tools/scan.py` 는 `--selftest` (위치 검증 6케이스) 만 통과했고 API 호출 실적이 없다.
어댑터 설계의 전제가 비어 있다. 런북 B 참조.

---

# Phase 1 대상 프로젝트

규모가 작고 `high` 가 0이 아닌 것으로 3개를 고른다. 세 개 합쳐 채점 호출 상한 96회.

| project_id | truth | high | baseline findings | 채점 호출 상한 |
|---|---|---|---|---|
| `sherlock_axion_2025_01` | 10 | 4 | 7 | 30 |
| `sherlock_tally_2024_12` | 11 | 2 | 6 | 33 |
| `code4rena_superposition_2025_01` | 11 | 3 | 9 | 33 |

제외한 것과 이유:

- `code4rena_liquid-ron_2025_03` — truth 5건으로 가장 작지만 `high` 가 0이라 High recall 을 못 잰다.
- `sherlock_20240920---final---boost-core-...` — baseline findings 가 **0건**이다. precision 분모가 0이 되는
  경계 케이스라 스모크 테스트로는 부적절하다. 다만 러너의 0-division 처리 검증용으로 따로 쓸 값어치가 있다.

---

# 런북 — 키가 생긴 뒤 실행할 것

## A. 채점 비용·시간 측정 (`OPENAI_API_KEY` 필요)

목적: 불확실성 ③ 해소. 프로젝트 1개로 실측한 뒤 31개로 외삽한다.

**A-0. 지출 상한을 먼저 건다.** platform.openai.com → Settings → Limits.
스크립트 버그로 루프가 돌면 8,223회 호출이 한 번에 나갈 수 있다.

```powershell
$sp  = "<scratchpad>"
$py  = "$sp\evalvenv\Scripts\python.exe"
$sb  = "$sp\recon\scabench"
$DR  = "$sp\scoring_data"
$OR  = "$sp\scoring_output"
$PID = "sherlock_axion_2025_01"

New-Item -ItemType Directory -Force -Path "$DR\source_of_truth","$DR\baseline",$OR | Out-Null
```

**A-1. truth 파일을 쪼갠다.** 프로젝트 객체를 통째로 덤프한다.

```powershell
& $py -c @"
import json, pathlib, os
ds = json.load(open(r'$sb\datasets\curated-2025-08-18\curated-2025-08-18.json', encoding='utf-8'))
item = next(x for x in ds if x['project_id'] == '$PID')
pathlib.Path(r'$DR\source_of_truth\$PID.json').write_text(json.dumps(item, indent=2), encoding='utf-8')
"@
```

**A-2. 이미 있는 baseline 결과를 채점기 이름 규칙으로 복사한다.**
재실행 비용 없이 채점만 재려는 것이므로 baseline runner 를 다시 돌리지 않는다.

```powershell
Copy-Item "$sb\datasets\curated-2025-08-18\baseline-results\baseline_$PID.json" `
          "$DR\baseline\${PID}_results.json"
```

**A-3. 채점을 돌리고 시간을 잰다.**

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:MODEL          = "o4-mini"
$env:BATCH_SIZE     = "10"
$env:SCAN_SOURCE    = "baseline"
$env:DATA_ROOT      = $DR
$env:OUTPUT_ROOT    = $OR
$env:REPOS_TO_RUN   = '["' + $PID + '"]'

$t0 = Get-Date
& $py -m scoring_algo.cli evaluate --no-telemetry --log-level INFO
"소요 {0:N1}초" -f ((Get-Date)-$t0).TotalSeconds
```

**A-4. 기록할 것.**

- 벽시계 소요 시간
- OpenAI 대시보드의 해당 시각 사용량(토큰·요금). 채점기는 토큰을 결과에 남기지 않으므로 대시보드가 유일한 출처다
- `$OR\${PID}_results.json` 의 항목 수와 `is_match`/`is_partial_match`/`is_fp` 분포
- 31개 외삽: 이 프로젝트의 호출 상한이 30이고 전체 상한이 8,223이므로 **약 274배**가 최악값이다

> 단가를 추측해서 적지 말 것. 대시보드 실측만 기록한다.

## B. AttackTrail 출력 확보 (`ANTHROPIC_API_KEY` 필요)

목적: 불확실성 ⑥ 해소. 어댑터가 감쌀 실제 출력을 만든다.

**B-0. 지출 상한을 건다.** console.anthropic.com → Settings → Limits.

```powershell
& $py -m pip install anthropic
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

**B-1. 가장 작은 대상으로 한 번 돌린다.** Phase 1 대상 중 하나를 clone 해서 쓴다.

```powershell
$dir = "$sp\sources\sherlock_axion_2025_01"
git -c core.longpaths=true clone --quiet --depth 50 <repo_url> $dir
git -C $dir config core.longpaths true
git -C $dir checkout --quiet <commit>          # 실패하면 fetch --unshallow 후 재시도

python internal\tools\scan.py $dir
```

**B-2. 확인할 것.**

- `output_config={"format": {"type": "json_schema", ...}}` 가 실제로 받아들여지는가.
  거부되면 `scan.py` 를 고쳐야 한다. 본체 수정이 필요하므로 **먼저 보고한다**
- `usage.cache_read_input_tokens` 가 0이 아닌가 (프롬프트 캐싱이 실제로 히트하는가)
- 후보 중 `_verify` 가 `rejected_by_scanner` 로 떨어뜨린 비율. 위치 환각률의 1차 지표다
- 실제 비용과 소요 시간
- 출력 JSONL 이 어댑터가 변환할 만한 형태인가

**B-3. 그 다음에야 어댑터를 설계한다.** 출력을 보기 전에 `adapters/attacktrail.py` 를 쓰지 않는다.

## C. 컨테이너 (Docker 필요)

Phase 1 의 baseline 재현은 대상 코드를 읽기만 하므로 컨테이너 없이 가능하다.
**PoC 실행 단계를 붙이는 시점부터는 타협하지 않는다.**

Docker 미설치. WSL2 는 있으므로 `WSL2 + Docker Engine` 또는 Docker Desktop 중 하나를 깐다.

evmbench 에서 가져올 격리 설정 (`instancer/backends/docker.py:62-82`):

```
SecurityOpt: ['no-new-privileges']   CapDrop: ['ALL']
Memory: 1GB   NanoCpus: 0.3   PidsLimit: 1024   RestartPolicy: 'no'
```

evmbench 가 하지 않은 것 중 우리가 해야 할 것:

- `ReadOnlyRootFs: True` (evmbench 는 False)
- non-root 실행 (evmbench 는 TODO 주석으로만 남아 있다)
- 네트워크: clone 이 끝난 뒤 빌드·실행 단계에서는 끊는다.
  K8s 로 간다면 `k8s.py:88-190` 의 NetworkPolicy 패턴을 쓴다.
  egress 를 `0.0.0.0/0` 에서 RFC1918·CGNAT·link-local 을 `except` 로 뺀 범위만 허용하는 방식이다
- instancer 에 `docker.sock` 을 마운트하지 않는다 (evmbench 는 마운트하고, 그 결과 격리가 약하다)

---

# 미해결 · 다음 결정이 필요한 것

| # | 항목 | 막힌 이유 |
|---|---|---|
| 1 | `OPENAI_API_KEY` | 채점과 baseline 양쪽에 필수. 없으면 Phase 1 이 돌지 않는다 |
| 2 | `ANTHROPIC_API_KEY` | AttackTrail 출력 확보에 필수 |
| 3 | Docker 설치 | PoC 실행 단계의 전제. Phase 1 자체는 없이도 가능 |
| 4 | `site/` 재구조화 | 결정됨(2026-09-15). Phase 1 착수 시 실행 |
| 5 | 빈 커밋 2건 처리 | 기본 실행에서 제외하기로 함. 대외 수치 낼 때 29개 기준임을 명시 |

---

# Phase 2·3 키 없는 부분 (2026-10-01 ~ 10-05)

워크플로 1회로 진행했다. 에이전트 54개, 오류 0. 유료 API 호출 0회.

## D-6. 목적은 A(내부 실력 파악) + C(회귀 탐지). 주 지표는 recall

대외 수치용이 아니다. G1 단계 평가이므로 놓치는 것(recall)이 주 지표이고, precision 은 감사자 시간 비용이라는 보조 지표다.

## D-7. 공개 사이트는 `site/` 만 배포한다

`index.html`, `styles.css`, `favicon.svg` 를 `site/` 로 옮기고 `deploy.yml` 의 `path` 를 `"."` 에서 `"site"` 로 바꿨다.
루트 문서(README, DEVELOPING)와 `eval/` 은 이제 배포 대상이 아니다.

## D-8. 하네스 환경은 저장소 안에 둔다

스크래치패드가 두 번 정리되어 venv 와 채점기 소스가 사라졌다. 그래서 고정했다.

```
eval/.venv                 scoring-algo 를 a62a15d1ea254158dd5cda4bea8ce8cf5791fb27 로 pip 설치
eval/datasets/scabench     서브모듈, eec0020939a47bbc06a98a7348a90908b96f4b4d
```

## D-9. Recall 분모는 데이터셋 truth 수다 (채점기 recall 과 다르다)

`scoring_algo/core/evaluate.py` 는 판정이 안 나온 truth 를 기록 없이 건너뛴다(`if not content: continue`).
판정이 안 나오는 경우는 둘이다. 에이전트 finding 이 0건이거나, strict 매치마다 finding 이 하나씩 빠져 비교할 게 바닥난 경우다.
그 truth 는 채점기 `RepoStats.recall` 분모에서 빠지고, recall 이 부풀려진다.

요구 정의는 `Recall = TP / 전체 ground truth 수` 이므로 헤드라인은 `recall_vs_dataset` 을 쓴다. 채점기 값도 함께 남긴다.
매칭 판정 자체는 바꾸지 않았다. `scoring/truth_trace.py` 는 `process_in_batches` 를 감싸 결과 유무만 기록하는 관찰 전용 래퍼다.
리포트 헤드라인에 "채점기가 판정하지 않은 truth" 수를 따로 표시한다.

## D-10. 회귀 스위트 6개 (`suites/regression.txt`)

```
sherlock_axion_2025_01  sherlock_tally_2024_12  sherlock_oku_2024_12
code4rena_forte-float128-solidity-library_2025_04  code4rena_next-generation_2025_05
sherlock_cork-protocol_2025_01
```

조건: 체크아웃 ok, EVM, high truth 1건 이상, 커밋 고정. scan.py 가 보낼 파일 합계 187개.
EVM 데이터셋에서 high truth 가 있는 10개 범주를 전부 high 로 덮는다. ORC·UPG·OTHER 는 EVM 프로젝트에 high truth 자체가 없어 못 덮는다.

정정: Phase 0 에서 Phase 1 대상으로 적은 `code4rena_superposition_2025_01` 은 Rust 프로젝트(EVM 밖)였다. 스위트에서 뺐다.

## D-11. 커밋이 브랜치명인 5개는 `pinned=false` 로 기록한다

fenix, lambowin, loopfi, bakerfi, blackhole 은 commit 칸이 `main` 이다. 실행할 때마다 코드가 바뀔 수 있어 재현이 안 된다.
전체 실행에는 넣되 manifest 에 `pinned=false` 를 남기고, 회귀 스위트에서는 뺐다.

## D-12. Phase 3 규모 (실측)

29개 전부 체크아웃 ok. unshallow 필요 4개(tn-contracts, boost-core, morph-l-2, idle-finance). 최장 절대경로 233자.

```
scan.py 가 보낼 파일   3,233 (EVM 24개만 3,082)   = AttackTrail Claude 호출 수
그중 .sol             1,706
.ts/.js/.go/.py       1,527
테스트 파일             572
EVM 밖                  5개 (coded-estate, superposition, pump-science, mantra-dex, cabal)
```

절반 가까이가 감사 대상이 아닌 파일이다. `adapters/PROPOSALS.md` P2 로 제안했다. scan.py 는 수정하지 않았다.
달러 비용은 추정하지 않았다. 호출당 실측 비용이 없다.

## D-13. AttackTrail 의 Cost 는 scan.py 변경 전까지 측정 불가

scan.py 가 `r.usage` 를 버린다. 하네스는 tokens/cost 를 None 으로 남긴다. PROPOSALS.md P3.

## D-14. 에이전트 지문을 셋으로 나눈다 (2026-10-19)

`prompt_hash` 하나가 파일 정체성과 프롬프트 동등성을 같이 맡았다. scan.py 파일 바이트를 해시하므로 주석 한 글자에도 값이 바뀌고, 프롬프트가 같은 run 끼리도 다르게 보였다. AttackTrail 어댑터의 manifest `agent` 에 세 필드를 더한다.

| 필드 | 무엇을 해시하나 | 용도 |
|---|---|---|
| `scan_py_sha256` | scan.py 파일 바이트 | 무결성 기록. `internal/` 이 git 밖이라 scan.py 의 사실상 유일한 식별자 |
| `prompt_sha256` | `ROLE + "\n\n" + load_standards(web)` 본문 (`adapters.attacktrail.system_prompt`) | run 비교 기준. scan.py 주석·코드 변경에 흔들리지 않는다 |
| `call_shape` | 어댑터 상수. API `api-v1`, CLI `cli-v1` | 호출 방식. 호출 형태를 바꾸면 사람이 올린다 |

- `prompt_hash` 는 구 정의(파일 바이트) 그대로 둔다. 이전 manifest 는 새 정의로 재계산할 수 없으므로 이전 run 끼리만 이 값으로 대조한다. 리포트는 `prompt_sha256` 이 있으면 이 값을 "legacy" 로 표시한다.
- 정정: 아래 "검증 결과" 표의 `F724874070E5F7FF…` 는 scan.py 파일 SHA-256 이다. prompt_hash 가 아니다. AUD-CHK-02 를 `STD_WEB` 에 등록한 2026-10-18 이후 scan.py 파일 SHA-256 은 `B8B761E2E0F2CB4B…` 다.
- 비-web 시스템 프롬프트는 2026-10-13 CLI 실행(`at-cli-axion`, `at-cli-regression-r1`) 이후 바뀌지 않았다. 그 실행들의 `scan-cli.jsonl` 에 기록된 `prompt_sha256` `2d0bb457ca2c605a` 가 새 `prompt_sha256` 의 앞 16자와 같다. 재실행할 필요가 없다.
- 한계: `prompt_sha256` 은 출력 스키마(scan.py `SCHEMA`)와 사용자 프롬프트 틀을 포함하지 않는다. 이 둘을 바꾸면 `scan_py_sha256` 만 바뀐다. SCHEMA 를 고칠 때 지문에 넣을지 다시 정한다.
- ~~scan.py 가 자기 `_run` 에 쓰는 `prompt_sha256` 은 `ROLE + std` (구분자 없음)라 위 값과 다르다. scan.py 는 고치지 않았다.~~ (D-20 정정: scan.py 의 해시 식을 `ROLE + "\n\n" + std` 로 맞췄다. 이제 `_run.prompt_sha256` 이 `fingerprints()["prompt_sha256"][:16]` 과 같다. 이 줄 시점 이전 run 의 `_run.prompt_sha256` 은 옛 식이라 매니페스트 값과 다르다.)

## D-15. 웹 스캔은 API 어댑터로 돌린다 (2026-10-19)

CLI 어댑터는 시스템 프롬프트를 `--system-prompt` 인자로 넘긴다. 파일 프롬프트만 stdin 이다. 측정한 명령줄 길이(`subprocess.list2cmdline`, 2026-10-19, AUD-CHK-02 v0.1 기준, exe 를 `claude.exe` 로 두고 잼. 실제 exe 경로 길이만큼 늘어난다):

```
비-web          7,277자     한도 32,767 안쪽
web=True      223,044자     6.8배 초과 (AUD-WEB-01 단독 약 19.8만 자, AUD-CHK-02 는 약 7.6%)
```

AUD-CHK-02 추가 전부터 초과였다. 원인은 시스템 프롬프트를 argv 에 넣는 호출 방식이다. 기존 테스트는 `call_claude` 를 가짜로 바꿔 argv 를 만들지 않아서 잡지 못했다.

결정:
- `_run_cli` 가 첫 호출 전에 명령줄 길이를 한 번 계산하고, `ARGV_LIMIT`(32,767) 이상이면 `crashed` 와 원인 메시지로 멈춘다. 파일마다 실패하는 대신 프로젝트 단위로 즉시 멈춘다. 테스트 `test_cli_web_prompt_over_argv_limit_fails_before_any_call`.
- 웹 스캔은 `attacktrail`(API) 어댑터로 돌린다. scan.py 가 기준 문서를 프로세스 안에서 읽고 캐시하므로 argv 문제가 없다. API 키가 필요하다.
- `call_claude` 의 "Prompt goes on stdin" 주석을 실제 동작대로 고쳤다. 호출 방식은 그대로라 `call_shape` 은 `cli-v1` 을 유지한다.
- 하지 않은 것: `--system-prompt-file` 전환(옵션 동작 미실측), stdin 합치기, 기준 문서 축소. 구독 기반 CLI 로 웹 스캔이 필요해지면 `--system-prompt-file` 을 파일 1개로 실측한 뒤 `cli-v2` 로 올린다.
- eval CLI 에는 `--web` 플래그가 없다. 지금은 어댑터를 `web=True` 로 직접 만들 때만 이 경로에 닿는다.

## D-16. 스캔 파일 선택을 함수 하나로 모으고 --web 만 넓힌다 (2026-10-19)

scan.py 의 파일 선택은 main() 안에 인라인으로 있었고, CLI 어댑터가 그대로 복사해 썼다. 찾은 결함:

- 제외 검사를 절대경로 문자열에 했다. 대상 저장소가 `.../lib/...` 아래 있으면 파일이 전부 빠졌다.
- 선택 파일이 0개여도 오류 없이 "검토 대기 0"으로 끝났다. 깨끗한 스캔과 구별되지 않았다.
- 웹 소스(`.tsx .jsx .vue .java .jsp .php` 등)를 읽지 않아 KISA 웹 항목 대부분이 후보 0이었다.

결정:
- scan.py 에 `select_files(root, web)` 를 만들고 scan.py main() 과 CLI 어댑터가 함께 쓴다. 복사본은 없앴다.
- 제외는 root 기준 상대경로의 디렉터리 이름으로 판단한다 (`.git`, `node_modules`, `lib`).
- 선택 파일이 0개면 scan.py 는 exit 1, CLI 어댑터는 `crashed` 다. scan.py 는 파일 선택을 anthropic 클라이언트 생성보다 먼저 해서 키 없이도 바로 알린다.
- `--web` 에서만: 확장자 `EXT_WEB` 추가, `vendor dist build out target .next venv site-packages` 와 `.min.js .bundle.js` 제외, 100,000바이트 초과 파일은 보내지 않고 `_run.skipped_large` 에 남김, `lib` 은 통째로 빼지 않고 `foundry.toml` 옆 `lib/` 만 뺌(웹 앱은 `src/lib` 에 코드를 둔다).
- 설정 파일(`.conf .xml .yml .properties .sql`)은 `--web` 에서도 보내지 않는다. 설정 기반 KISA 항목은 사람이 판정한다 (AUD-CHK-02 §5-6).

일반 스캔의 파일 집합은 바뀌지 않았다. 옛 인라인 규칙과 `select_files(root)` 를 데이터셋 29개 전부에 돌려 비교했다:

```
projects=29  old=3,254  new=3,254  diffs=0
```

D-14 지문으로 보면 `scan_py_sha256` 만 바뀌고(`bcdd886c9e76523d…`) 비-web `prompt_sha256` 은 `2d0bb457ca2c605a…` 그대로다. 기존 eval 결과와 계속 비교할 수 있다.

하지 않은 것: 웹 정답셋. 웹 대상 데이터셋이 없어서 `--web` 확장의 효과는 측정하지 못한다. 크기 상한 100,000바이트는 고정값이다.

D-26 정정(2026-10-21): 위의 "일반 스캔의 파일 집합은 바뀌지 않았다"(3,254개)는 그 시점의 값이다. D-26 에서 비웹 제외 목록에 `test tests ignition script scripts deploy mocks` 가 들어가 29개 저장소의 선택 파일이 2,141개가 됐다.

## D-17. scan.py 응답 읽기를 고치고 max_tokens 를 16000 으로 올린다 (2026-10-19)

scan.py 는 `json.loads(r.content[0].text)["candidates"]` 로 응답을 읽었다. 찾은 결함:

- `claude-sonnet-5` 는 `thinking` 을 생략하면 적응형 사고가 기본으로 켜지고, thinking 표시 기본값이 `omitted` 라 빈 `thinking` 블록이 text 블록보다 먼저 올 수 있다. 그러면 `content[0]` 이 ThinkingBlock 이라 `.text` 에서 AttributeError 가 나고, 그 파일은 `ERROR 'ThinkingBlock' object has no attribute 'text'` 한 줄만 남기고 빠진다. 모델이 파일을 거절한 것도, 스캔이 실패한 것도 아닌데 후보 0 으로 보인다.
- `stop_reason` 을 보지 않았다. `refusal`(Sonnet 5 는 사이버 안전장치로 거절할 수 있다. 보안 스캐너 입력은 이 범주에 걸리기 쉽다)은 content 가 비면 IndexError, `max_tokens` 는 잘린 JSON 의 파싱 오류로 나와 원인을 알 수 없었다.
- `max_tokens=8000` 은 사고 토큰이 같은 한도를 쓴다는 점을 반영하지 않은 값이다.

실제 API 응답으로 thinking-first 를 본 것은 아니다(키 없음). 근거는 문서의 기본값 설명과, 같은 모양의 JSON 을 SDK 타입(`Message.model_validate`)과 HTTP 로 통과시켜 실패를 재현한 것이다.

결정:
- scan.py 에 `parse_candidates(r)` 를 만들고 main() 이 쓴다. 속성 접근만 써서 SDK 타입에 의존하지 않는다(`--selftest` 가 SDK 없이 돈다). 실패는 `ScanError` 로, 메시지가 ERROR 로그 한 줄에서 원인을 알려준다.

| 응답 | 결과 |
|---|---|
| `end_turn` + text 블록(앞에 thinking 등이 있어도) | candidates 리스트 |
| text 블록이 여러 개 | 이어 붙여서 파싱 (분할 여부는 문서에 없음. 파싱 실패면 ERROR) |
| `refusal` | `stop_reason=refusal category=<값 또는 null>` |
| `max_tokens` | `stop_reason=max_tokens (출력이 잘림...)`. 텍스트가 멀쩡해 보여도 믿지 않는다 |
| 그 밖의 `stop_reason` (`pause_turn`, `model_context_window_exceeded`, `None` 등) | `stop_reason=<값> (end_turn 아님)` |
| text 블록 없음 또는 공백뿐 | `text 블록 없음 (content types=[...])` |
| JSON 아님 / `candidates` 가 list 아님 / 항목이 dict 아님 | 각각 별도 메시지. 마지막은 main() 의 `c["file"] = rel` 이 try 밖이라 넣었다 |

- `max_tokens` 16000. 근거: ~~문서의 비스트리밍 요청 권장값이다~~ (D-19 정정: 과장이었다. 문서에서 16000 은 권장값이 아니라 Python SDK 예시의 값으로만 나온다. `python/claude-api/README.md` 예시가 전부 `max_tokens=16000` 이다. `shared/model-migration.md` 는 "`max_tokens > ~16000` 이면 스트리밍"을 권하므로 16000 은 그 경계 이하다). 실제 한도는 설치된 SDK 1.15.0 `_base_client.py` 의 `_calculate_nonstreaming_timeout` 검사식이다. 비스트리밍에서 `3600 * max_tokens / 128000 > 600`(약 21,333 초과)이면 ValueError 라 그 위로는 스트리밍이 필요하다. 16000 이면 450 이라 통과한다. 적응형 사고가 켜지면 사고 토큰도 `max_tokens` 를 같이 쓴다(model-migration.md, Sonnet 5 "Silent default change"). 16000 이 파일 크기에 충분한지는 실측하지 못했다. 모자라면 이제 `stop_reason=max_tokens` 로 보인다.
- 모델, thinking·effort(보내지 않음), ROLE, SCHEMA, 기준 문서, 사용자 프롬프트 틀은 그대로다. `prompt_sha256` 앞 16자 `2d0bb457ca2c605a` 는 전후 같다. `scan_py_sha256` 만 바뀌었다.
- 호출 형태가 바뀌어 API 어댑터 `call_shape` 를 `api-v1` → `api-v2` 로 올렸다 (D-14 규칙. D-14 표의 `api-v1` 은 이 결정으로 `api-v2` 가 됐다). CLI 어댑터는 호출 방식이 그대로라 `cli-v1`. scan.py 의 ROLE·SCHEMA·`select_files`·`verify` 만 import 하므로 영향이 없고, 기존 CLI 시험이 통과한다. 이전 API run 은 없어서(키 없음) 깨지는 비교도 없다.

테스트(실제 API 호출 없음):
- `scan.py --selftest`: 위치 검증 6건 + 가짜 응답(SimpleNamespace) 19건. SDK 가 없어도 돈다.
- `tests/test_scan_parse.py`: 같은 케이스를 실제 SDK 타입으로. 첫 블록이 ThinkingBlock 이라 옛 코드가 AttributeError 인 것을 먼저 확인하고 고친 경로를 검증한다. refusal 카테고리 3종(값, null, stop_details 없음), `end_turn` 이 아닌 5종, 오류 메시지가 한 줄인지.
- `tests/test_scan_fake_api.py`: 127.0.0.1 임의 포트에 가짜 Messages API(http.server 스레드)를 띄우고, 진짜 SDK 로 scan.py 를 서브프로세스로 돌린다. 파일 3개가 각각 thinking-first 정상, refusal(cyber), max_tokens 를 받는다. 확인: 정상 파일에 후보가 기록되고 ERROR 가 없다. 나머지 둘은 `stop_reason`·`category` 가 든 ERROR 줄로 남고 다른 파일을 막지 않는다. 서버가 받은 본문에 `max_tokens` 16000, `output_config.format.type=json_schema`, 두 번째 system 블록 `cache_control.ttl="1h"`(D-23: 지금은 `5m`)가 있고 `thinking` 은 없다. 트래픽은 127.0.0.1 뿐이다. 환경은 OS 변수와 더미 키, 빈 임시 홈만 준다. 프록시 환경변수는 죽은 로컬 포트로 막아, base URL 이 무시돼도 바깥 연결이 생길 수 없다(`.invalid` 호스트로 CONNECT 가 프록시에 잡히는 것을 확인했다). 이 시험을 옛 scan.py 에 돌리면 5건이 실패한다(`'ThinkingBlock' object has no attribute 'text'`, `8000 == 16000`).

하지 않은 것: 인증 오류(401)·잘못된 모델(404)이 파일마다 반복될 때 첫 발생에 중단하는 처리, usage 기록(D-13), 모델 변경, `thinking`·`effort` 설정 추가. 응답 읽기 고침과 한도 상향만 했다.

## D-18. API 어댑터가 scan.py 실패를 보고, 빈 임시 홈을 준다 (2026-10-19)

D-17 작업 중 API 어댑터(`adapters/attacktrail.py`)에서 찾은 두 결함. 둘 다 실제 키로 처음 돌릴 때 드러날 문제였다.

- 파일별 실패를 세지 못했다. 검사 문자열이 `"]  ERROR "` 였는데 scan.py 로그는 `"  [2/3] B.sol  ERROR ..."` 라 대괄호와 ERROR 사이에 파일명이 있어 한 번도 맞지 않았다. 모든 파일이 실패해도 `status=ok`, `error=None` 이었다.
- 시작 직후 죽을 수 있었다. `scoped_env` 는 `PATH SYSTEMROOT TEMP TMP` 와 키만 준다. anthropic 1.15.0 의 `Anthropic()` 은 생성할 때 홈·설정 디렉터리를 찾고, 없으면 `RuntimeError: Could not determine home directory` 로 scan.py 가 exit 1 이다 (scoped_env 와 같은 환경, 더미 키, 닫힌 로컬 base URL 로 재현).

결정:
- scan.py 진행 줄을 정규식 `^\s*\[(\d+)/(\d+)\] (.+?)  (ERROR|후보) ` 로 읽는다. 일부 실패는 `"<k> of <n> files failed inside scan.py ...; first: <첫 ERROR 줄>"` 메모, 전부 실패면 `crashed` (CLI 어댑터와 같은 규칙).
- scan.py 서브프로세스에 `HOME USERPROFILE APPDATA ~~ANTHROPIC_CONFIG_DIR~~` 를 실행마다 만드는 빈 임시 디렉터리로 준다. ~~진짜 사용자 프로필을 넘기면 SDK 가 사용자 자격증명 설정을 읽을 수 있어서 넘기지 않는다.~~ 실행이 끝나면 지운다. (D-19 정정: 취소선 두 곳. 이유 설명은 D-19 "SDK 홈 변수" 참조. 요지: 키가 있으면 SDK 는 자격증명 설정을 읽지 않는다. 임시 홈을 주는 이유는 홈 의존 크래시 방지와 진짜 프로필 경로에 의존하지 않기다. `ANTHROPIC_CONFIG_DIR` 는 D-19 에서 뺐다.)
- 호출 형태(요청 본문)는 그대로라 `call_shape` 은 `api-v2` 유지.

테스트(`tests/test_adapters.py`): 가짜 실행기로 env 에 빈 임시 홈이 들어가고 진짜 `USERPROFILE` 이 새지 않는지, 실행 후 지워지는지, 일부 실패 메모, 전부 실패 시 `crashed`.

## D-19. 후보 필드 검증, 병렬화와 시간 예산, 치명 오류 중단, usage, --out (2026-10-19)

D-17·D-18 뒤 독립 리뷰와 블랙박스 검증이 남긴 결함을 고쳤다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, `select_files`, 모델, `thinking`·`effort`(보내지 않음)는 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). 실제 API 는 한 번도 호출하지 않았다.

### 1. 후보 필드 검증 (`check_candidates`)

결함: 후보의 `line` 이 `"5"`·null·실수·bool 이거나 `code_excerpt` 가 str 이 아니면, try 밖 후보 루프에서 `verify()` 가 TypeError 를 냈다. scan.py 가 exit 1 로 죽고 앞선 파일 결과까지 버려졌다(두 검증이 각각 재현). CLI 어댑터도 같은 루프 구조라 같은 위험이었다.

- scan.py 에 순수 함수 `check_candidates(cands)` 를 만들고 `parse_candidates` 가 마지막에 부른다. `line` 은 bool 을 뺀 int(`True` 는 `isinstance(.., int)` 라 따로 막는다), `title code_excerpt category severity_guess why poc_sketch` 는 str 이어야 한다. 키가 없어도 위반이다(SCHEMA 는 8개 전부 필수). `file` 은 main() 이 실제 경로로 덮어써서 보지 않는다.
- 위반하면 `ScanError("candidates[1].line 이 정수 아님 (str)")` 처럼 한 줄 메시지로 그 파일만 ERROR 가 된다. 후보 하나만 이상해도 그 파일의 후보 전체를 버린다. 구조화 출력이 스키마를 어겼다는 신호라 나머지도 믿지 않는다.
- CLI 어댑터는 `structured_output["candidates"]` 에 같은 함수를 쓴다. 검사는 파일 단위 `one()` 안이라 ~~이상 응답이 프로젝트 전체를 죽이지 않는다~~ (D-20 정정: 이상 응답에만 해당했다. 같은 `one()` 에서 파일 읽기와 프롬프트 조립은 try 밖이라 `.sol` 이름의 디렉터리나 읽기 오류 하나가 프로젝트 전체를 crashed 로 만들었다. D-20 §5 에서 try 안으로 옮겼다). 동작 변경 하나: 예전에는 `candidates` 키가 없으면 후보 0 개로 읽었고(`or []`), 지금은 그 파일 ERROR 다.

### 2. 병렬화와 시간 예산

결함: scan.py 는 파일을 한 번에 하나씩 불렀다. 데이터셋 최대 프로젝트는 557 파일이고 하네스 기본 타임아웃은 1800초다. 타임아웃이 나면 어댑터가 그 프로젝트 결과를 전부 버린다.

- `--workers N`(기본 4, CLI 어댑터 `WORKERS` 와 같다). `ThreadPoolExecutor` 로 파일마다 호출하고, 결과 처리와 JSONL 기록은 메인 스레드가 submit 순서대로 한다. 결과 파일의 행 번호가 finding id(`at-<행>`)가 되므로 순차 실행과 같은 순서여야 한다. 진행 줄 `[i/n]` 도 파일 순서 그대로다.
- `--time-budget SECONDS`(선택). 예산은 `main()` 시작부터 센다(하네스 타임아웃은 프로세스 시작부터 센다). 넘으면 새 파일 호출을 시작하지 않고 남은 파일을 `  [i/n] <file>  ERROR skipped: time budget reached` 로 남긴 뒤 정상 종료(exit 0, 요약 출력, 파일 닫기)한다. 이미 나간 호출은 끊지 않는다.
- API 어댑터는 `ctx.timeout_s - max(60, 0.1 × timeout_s)`(하한은 timeout 의 절반)을 `--time-budget` 으로 넘긴다. 1800초면 1620초. 여유는 파이썬·SDK 시작과 예산이 끝날 때 이미 나가 있던 호출(최대 workers 개)이 끝나길 기다리는 몫이다. ~~그래서 하네스가 죽이기 전에 scan.py 가 스스로 멈추고 결과를 남긴다.~~ (D-20 정정: 과장이었다. 예산은 새 호출을 시작하지 않을 뿐 이미 나간 호출은 SDK 기본 read timeout 600초·재시도 2회로 돌았다. 그 호출이 여유(1800초면 180초)보다 길면 하네스가 먼저 프로세스를 죽였고, 결과를 파일 순서로 쓰기 때문에 첫 파일이 매달리면 뒤에서 끝난 결과도 메모리에서 사라졌다. 블랙박스 u2 가 재현했다. 호출별 timeout 과 하드 데드라인은 D-20 §1.)
- JSONL 은 파일 하나 처리할 때마다 flush 하고, 진행 줄 출력도 flush 한다. 강제 종료돼도 처리분이 디스크에 남는다.
- 호출 형태(동시성)가 바뀌어 API 어댑터 `call_shape` 를 `api-v2` → `api-v3` 로 올렸다(D-14 규칙). 동시성은 rate limit 동작과, 어떤 호출이 프롬프트 캐시 쓰기를 같이 내는지를 바꾸므로 api-v2 run 과 같은 조건이 아니다. 이전 API run 은 없다(키 없음). 어댑터는 `--workers` 를 넘기지 않으므로 동시성은 scan.py 기본값 4 에 달려 있다. 기본값을 바꾸면 `call_shape` 도 올린다. 실제 값은 JSONL `_run` 헤더(`workers`, `time_budget`)에 남는다.
- 알려진 비용: 캐시 항목은 첫 응답이 시작된 뒤에야 읽을 수 있다(`shared/prompt-caching.md` "Concurrent-request timing", 같은 접두사 병렬 요청은 모두 정가). 시작할 때 처음 workers 개 요청이 각각 기준 문서의 1시간 TTL 쓰기(2x)를 낼 수 있다(D-23: TTL 이 5분으로 바뀌어 쓰기는 1.25x). 첫 호출을 먼저 끝내는 예열은 넣지 않았다(비-web 기준 문서는 작고, 예열은 첫 파일 지연만큼 예산을 쓴다). 실제 중복은 `_usage` 의 `cache_creation_input_tokens` 합과 `cache_read_input_tokens` 로 볼 수 있다. 미측정.
- 한계: 호출마다 타임아웃(`timeout=`)을 주지 않는다. 이미 나간 호출이 길면(SDK 기본 600초, 재시도 2회) 여유를 넘겨 하네스가 먼저 죽일 수 있다. 그때 어댑터는 `timeout` 으로 기록하지만 `raw/scan.jsonl` 은 남는다. (D-20 §1 에서 해소: 호출별 timeout·재시도와 `--deadline-s`.)

### 3. 치명적 오류 즉시 중단

(D-20 정정: 치명 목록은 401·402·403·404 이고 연속 API 오류 차단기가 더해졌다. 아래는 D-19 시점 기록이다. D-20 §3.)

401(`AuthenticationError`), 403(`PermissionDeniedError`), 404(`NotFoundError`)는 키·권한·모델 이름 문제라 파일마다 반복된다. 557 파일에 같은 오류를 557번 내는 것을 막는다. 클래스 이름은 SDK `_exceptions.py` 와 문서 `shared/error-codes.md`(401 `authentication_error`, 403 `permission_error`, 404 `not_found_error`, 모두 재시도 불가)로 확인했다. 404 는 없는 모델과 조직이 쓸 수 없는 모델을 구별하지 않는다(같은 응답).

- 셋 중 하나가 나면 공유 `threading.Event` 를 세워 아직 시작하지 않은 호출이 요청 없이 `ERROR skipped: aborted after fatal API error` 로 끝나게 한다. stderr 에 `치명적 API 오류로 스캔을 중단했습니다: <클래스> (HTTP <코드>): <메시지>` 한 줄을 남기고 exit 2. 앞선 파일 결과와 `_usage` 는 JSONL 에 남고 파일은 정상으로 닫힌다. 이미 나간 호출(최대 workers 개)은 끝까지 기다린다.
- 지시는 `cancel_futures` 였지만 Event 로 했다. 결과를 순서대로 읽는 메인 스레드가 앞선 future 에서 막혀 있을 때 뒤쪽 워커의 오류를 `cancel_futures` 로 전파하기 어렵고(취소된 future 는 `CancelledError`), 워커가 큐에서 꺼낸 직후의 틈도 Event 가 막는다. 효과(남은 작업이 요청을 내지 않음)는 같다. 메인 스레드가 예외로 빠져나갈 때(Ctrl-C 등)도 `finally` 에서 Event 를 세워 큐의 호출이 계속 나가지 않게 한다.
- 어댑터는 rc≠0 이면 이미 `crashed` 로 처리한다. 이제 그때(타임아웃 포함) `raw/scan.jsonl` 을 지우지 않고 오류 메시지에 `(partial output kept in raw/)` 를 붙인다. 진단용이다.
- 429·5xx 는 SDK 재시도(`DEFAULT_MAX_RETRIES=2`)에 맡기고 파일별 ERROR 로 둔다. 400 같은 그 밖의 4xx 도 파일별 ERROR 다. (D-20 정정: "그 밖의 4xx" 중 402 는 치명으로 옮겼고, 413 은 중립, 400 은 차단기 카운터에 센다. 이 항목에 입력 토큰 초과를 400 으로 든 문장은 없었다. 다른 곳에서 그렇게 적었다면 틀린 것이다. 문서상 입력이 너무 큰 요청은 413 `request_too_large` 다: `shared/error-codes.md` 표와 "413 Request Too Large" 절, 원인에 "Too many tokens in input". 400 은 형식·파라미터 오류다. SDK 도 413 을 `RequestTooLargeError`, 400 을 `BadRequestError` 로 가른다.)
- 종료 코드: 0 정상(파일별 ERROR·시간 예산 소진 포함), 1 시작 전 실패(스캔할 파일 0개 등), 2 치명적 API 오류. argparse 인자 오류(`--workers 0` 포함)도 2 라 stderr 로 구별한다.

### 4. usage 기록 (D-13 해소)

- scan.py 는 응답을 받은 파일마다 JSONL 에 `{"_usage": {"file", "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens", "cache_creation": {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"}}}` 한 줄을 쓴다(`cache_creation` 은 SDK 응답에 있을 때만). refusal·max_tokens·후보 필드 위반처럼 파싱이 실패한 파일도 응답이 왔으면 쓴다. 비용이 나갔기 때문이다. 요청이 오류 응답(4xx)으로 끝난 파일은 usage 가 없어 행이 없다.
- 주의: scan.py 행의 `input_tokens` 는 캐시를 쓰지 않은 입력만이다. CLI 어댑터가 쓰는 `_usage.input_tokens` 는 세 종류를 이미 합한 값이다(`cost_usd`, `num_turns` 도 있다). 행 모양이 다르다. 읽는 쪽은 각자 어댑터뿐이다. (D-20 §6: CLI 행에 원값 키를 추가했고, `sum_usage` 가 두 행을 모두 맞게 읽는다. 표는 거기에 있다.)
- API 어댑터는 `sum_usage()` 로 합산한다. `tokens_in` = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`(CLI 어댑터와 같은 정의), `tokens_out` = `output_tokens`. `_usage` 행이 하나도 없으면 둘 다 None 이다. `parse_scan_jsonl` 은 `_usage` 행을 명시적으로 건너뛴다(`_status` 가 없어 원래도 후보가 되지 않았다).
- `cost_usd` 는 계산하지 않는다(단가 확정 전, None 유지). 계산에 필요한 정보만 남긴다. 파일별 행에서:

```
비용 = P_in × (input_tokens
              + 2.0  × cache_creation.ephemeral_1h_input_tokens
              + 1.25 × cache_creation.ephemeral_5m_input_tokens
              + 0.1  × cache_read_input_tokens)
     + P_out × output_tokens
```

  (D-20 정정: 위 식의 변수명을 실제 `_usage` 행 키와 맞췄다. 옛 식은 `cache_creation_1h`·`cache_creation_5m` 이라고 적었는데 그런 키는 없다. 행의 키는 `cache_creation` 객체 안의 `ephemeral_1h_input_tokens`·`ephemeral_5m_input_tokens` 다. `cache_creation` 객체는 SDK 응답에 있을 때만 행에 들어가므로 없으면 쓰기 토큰은 `cache_creation_input_tokens` 하나다. TTL 구분 없이 그 값을 쓰려면 배수를 정해야 한다. scan.py 는 1시간 TTL 만 보내므로 2.0 이다. D-23 정정: 지금은 5분 TTL 만 보내므로 1.25 이고 식은 D-23 §3 에 있다.) 배수는 `shared/prompt-caching.md`(쓰기 1.25x 5분, 2x 1시간, 읽기 약 0.1x, 읽기 배수는 모델마다 다르다고 적혀 있어 sonnet-5 값은 단가와 함께 확인할 것). `output_tokens` 는 사고 토큰을 포함한 청구 기준 합계다(SDK `Usage.output_tokens_details` 설명). scan.py 는 1시간 TTL 만 썼으므로 5분 쓰기는 보통 0 이었다(D-23 정정: 지금은 반대로 5분 TTL 만 쓴다). P_in, P_out 은 sonnet-5 단가를 사람이 확정한 뒤 넣는다. CLI 행은 `input_tokens` 가 이미 합이므로 이 식에 그대로 넣으면 안 되고 D-20 §6 의 `input_tokens_uncached` 를 쓴다.

### 5. 결과 파일 경로 (`--out`)

결함: scan.py 는 결과를 자기 옆 `scan-<commit8>.jsonl` 에 썼다. 같은 커밋 두 프로젝트를 병렬로 돌리면 겹치고, 어댑터는 다른 git env 로 커밋을 조회해 파일을 못 찾을 수 있었다.

- scan.py `--out PATH`(선택, 미지정이면 예전 동작). API 어댑터는 `<project_dir>/raw/scan.jsonl` 을 직접 지정한다. 어댑터의 `rev-parse`·mtime 추측·`shutil.move` 를 없앴다. 실행 전에 같은 경로의 이전 파일을 지워 잔여물을 읽지 않는다. 어댑터가 `raw/` 를 먼저 만든다(scan.py 는 상위 디렉터리를 만들지 않는다).

### 6. SDK 홈 변수 (`ANTHROPIC_CONFIG_DIR` 제거)

`SDK_HOME_KEYS` 는 `("HOME", "USERPROFILE", "APPDATA")` 다. `ANTHROPIC_CONFIG_DIR` 를 넣으면 SDK 가 매 실행 stderr 에 잘못된 경고를 낸다("`ANTHROPIC_API_KEY` is set and takes precedence over the SDK's profile / federation auto-discovery; unset ANTHROPIC_API_KEY ..."). 우리는 프로필을 쓰려는 것이 아니다.

SDK 1.15.0 소스로 다시 확인했다:
- 경고 조건: `_client._warn_env_shadow` 가 `lib/credentials/_constants.py` `_has_auto_discoverable_credentials()` 를 부르고, 이 함수는 `ANTHROPIC_PROFILE` 또는 `ANTHROPIC_CONFIG_DIR` 가 설정돼 있으면 True 다. 그러면 `lib/credentials/_auth.py` `warn_env_static_shadows_auto_discovery` 가 경고를 낸다.
- 키가 있으면 기본 자격증명 해석은 일어나지 않는다. `_client.py` 는 `credentials is None and api_key is None and auth_token is None` 일 때만 `default_credentials()` 를 부른다. D-18 의 "진짜 프로필을 넘기면 SDK 가 자격증명 설정을 읽을 수 있다"는 과장이었다.
- 그래도 홈이 필요한 이유: 같은 `_has_auto_discoverable_credentials()` 가 `_has_explicit_active_config()` → `_read_active_config_pointer()` → `_config_dir()` 로 `active_config` 포인터 파일 위치를 찾는다. Windows 는 `%APPDATA%\Anthropic`(APPDATA 가 없으면 `Path.home()`), 그 밖은 `~/.config/anthropic`(`Path.home()`)이다. 홈이 없으면 `Path.home()` 이 RuntimeError 를 내고 클라이언트 생성에서 죽는다. 포인터 파일은 읽지만 자격증명은 읽지 않는다. 포인터가 있으면 같은 경고가 날 수 있다.
- 그래서 빈 임시 홈의 이유는 둘이다. 홈 의존 크래시를 막고, 진짜 프로필 경로(`%APPDATA%\Anthropic`)의 내용에 동작이 달라지지 않게 한다. HOME·USERPROFILE·APPDATA 셋이면 설정 디렉터리도 임시 폴더로 간다(블랙박스로도 확인됨).

### 7. 부분 실패를 리포트에 드러내기

일부 파일이 실패해도 프로젝트는 `status ok` 로 지표 분모에 들어간다. 정책은 바꾸지 않았다(분모에 넣을지는 사용자 결정 사항). `report/render.py` 에 "Partial failures (부분 실패)" 절을 더했다. `agent_status` 가 ok 인데 `agent_error` 메모가 있는 프로젝트를 (프로젝트, 메모)로 보여 준다. 없으면 "없음". 기존 "Excluded / failed projects" 절 바로 아래, 같은 표 형식이다. 어댑터 메모에는 시간 예산으로 건너뛴 파일 수도 들어간다(`... (2 skipped by time budget)`). (D-20: 메모 형식이 바뀌었다. 실패 파일 이름과 사유별 건너뜀 수, `files_*` 커버리지 필드는 D-20 §2.)

### 8. 문서 정정

D-17 의 16000 근거(과장 정정)와 D-18 의 홈 변수 설명(과장 정정, `ANTHROPIC_CONFIG_DIR` 제거)을 해당 항목에 취소선과 정정 문구로 남겼다. "검증 결과" 표의 옛 값은 시점 기록이라 지우지 않았고, 현재 값은 아래에 날짜를 붙여 따로 적는다. `README.md` 의 API 어댑터 설명을 현재 동작에 맞췄다. `adapters/PROPOSALS.md` 는 낡은 문장(usage 미기록, `]  ERROR ` 검사, `--out` 부재)과 P3 아래에 "2026-10-19 갱신·적용됨" 문구를 덧붙였다(옛 문장은 그대로 둠).

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 44건. 위치 검증 6, 가짜 응답 20, `check_candidates` 15(line `"5"`·None·1.5·True·누락, `code_excerpt` 숫자·None, 다른 문자열 필드, list 아님, 항목 dict 아님, `file` 은 안 봄), `usage_of` 3.
- `tests/test_scan_parse.py`: 같은 케이스를 실제 SDK 타입(`Message`)으로. 필드 위반 7종, `verify()` 가 `"5"`·None 에서 TypeError 를 내는 것, `usage_of` 의 refusal 응답 usage.
- `tests/test_scan_fake_api.py`(로컬 가짜 Messages API, 진짜 SDK, 서브프로세스, 프록시 차단·`NO_PROXY`·빈 임시 홈 유지): 후보 필드 위반 5종 + 후보 하나만 나쁜 파일이 그 파일만 ERROR 이고 다른 파일 결과는 유지 / 400 은 중단이 아님 / 401·403·404 는 첫 오류 뒤 요청이 더 나가지 않고(서버 요청 수 2) exit 2, stderr 한 줄, 앞선 파일 결과 유지 / 병렬(workers 4)에서도 첫 오류 뒤 새 요청 없음 / 완료 순서를 뒤집는 지연을 줘도 workers 1 과 4 의 JSONL 행 순서·진행 줄이 파일 순서와 같고 동시 요청이 실제로 겹침 / `--time-budget` 이 작으면 남은 파일이 skipped ERROR 이고 exit 0 에 요약 출력, 건너뛴 파일은 요청이 안 나감 / `_usage` 행(refusal·max_tokens 파일 포함, 값은 가짜 서버가 돌려준 것) / `--out` 경로에 쓰고 scan.py 옆에는 안 씀 / 파일 하나 처리 직후 강제 종료해도 그 파일 행이 디스크에 있음 / SDK 경고 없음.
- `tests/test_adapters.py`: `--time-budget` 값(1800→1620, 600→540, 300→240, 10→5), `--out` 경로, scan.py 옆의 남의 `scan-*.jsonl` 을 읽지도 옮기지도 않음, `_usage` 합산과 None, `_usage` 행이 후보가 아님, `SDK_HOME_KEYS` 3개와 `ANTHROPIC_CONFIG_DIR` 부재, rc≠0·타임아웃에서 raw 보존, 시간 예산 건너뜀 메모.
- `tests/test_attacktrail_cli.py`: 이상한 후보 타입 4종이 그 파일만 ERROR 이고 usage 는 기록됨.
- `tests/test_report.py`: 부분 실패 목록(ok + 메모만 나오고, 비 ok 는 제외 목록으로, 분모 정책 불변).
- mutation 확인(스크래치 복사본, 레포 파일 불변): round2 백업의 옛 scan.py·attacktrail.py·attacktrail_cli.py·render.py 를 각각 되돌리면 새 테스트가 실패한다(21·14·1·2건). 새 코드에 변이를 넣어도 잡힌다: 치명 오류 뒤 새 호출 허용, 완료 순서대로 기록, 예산 무시, workers 무시, bool 허용, 실패한 파싱의 usage 버림, `ANTHROPIC_CONFIG_DIR` 복원, 시간 여유 제거, exit 2 제거, 파일별 flush 제거. 각각 1~4건씩 실패.

### 하지 않은 것

- `cost_usd` 계산, 단가 확정. 모델 변경, `thinking`·`effort` 설정 추가, 프롬프트·SCHEMA 변경.
- 병렬 시작 시 캐시 쓰기 중복 예열, ~~호출별 타임아웃, 429·5xx 별도 처리~~ (D-20 에서 호출별 timeout 과 연속 실패 차단기를 넣었다. 캐시 예열은 D-20 §10 에 근거와 선택지만 적고 구현하지 않았다).
- 부분 실패 프로젝트의 분모 정책 변경.
- 파일별 ERROR·skipped 를 JSONL 에 남기기(지금은 stdout 에만 있다. 어댑터가 `raw/scan.stdout.txt` 로 보존한다).

## D-20. 하드 데드라인과 호출별 timeout, 커버리지, 402·연속 오류 차단기, 헤더 해시, 에이전트 스냅샷 (2026-10-20)

D-19 뒤 2차 재리뷰(조건부)와 독립 블랙박스 재검증(34건 중 32건 통과)이 남긴 것을 고쳤다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA` 의 properties·required·enum, `select_files`, 모델, `thinking`·`effort`(보내지 않음)는 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). 실제 API 는 한 번도 호출하지 않았고 외부 네트워크 연결도 없다(통합 시험은 127.0.0.1 가짜 서버).

### 1. 하드 데드라인과 호출별 timeout (H1, 블랙박스 u2)

결함: `--time-budget` 은 새 호출의 시작만 막는다. 이미 나간 호출은 SDK 기본(read timeout 600초, 재시도 2회)으로 돌아서, 그 호출이 여유(timeout − max(60초, 10%))보다 길면 하네스가 프로세스를 죽이고 어댑터는 그 프로젝트 결과를 전부 버렸다. 결과를 파일 순서로 쓰기 때문에 첫 파일이 매달리면 뒤에서 이미 끝난 결과도 메모리에서 사라졌다. 블랙박스 u2: 첫 파일 hang, 2·3번째 즉시 응답, raw 에 0행.

SDK 1.15.0 소스에서 확인한 것(추측 아님):
- `client.with_options(timeout=..., max_retries=...)` 는 같은 http 클라이언트를 공유하는 복사본을 만든다(`_client.py` `copy()` 의 `http_client = http_client or self._client`). 복사 200번에 0.026초라 호출마다 만들어도 비용이 없다.
- 호출별 `max_retries` 는 클라이언트 옵션으로만 줄 수 있다(`messages.create` 는 `timeout=` 만 받는다).
- 기본 timeout 은 `Timeout(600, connect=5.0)` 이다. 숫자 하나를 주면 connect 까지 바뀌므로 `anthropic.Timeout(t, connect=min(5, t))` 로 준다.
- 한 호출의 최악 시간은 (재시도+1) × timeout + 재시도 사이 대기다. 대기는 `Retry-After` 헤더가 없으면 0.5초에서 8초 상한(`_constants.py` `INITIAL_RETRY_DELAY`, `MAX_RETRY_DELAY`)이지만, 헤더가 있으면 그 값을 상한 없이 따른다(`_base_client.py` `_calculate_retry_timeout`, 최대 4,294,967초). 문서는 429 에 `retry-after` 가 온다고 적는다(`shared/error-codes.md` "429 Rate Limited"). 그래서 SDK 옵션만으로는 총 시간을 못 묶는다.
- httpx 의 read timeout 은 읽기 한 번마다 센다. 응답을 조금씩 흘리면 걸리지 않는다(가짜 서버가 한 바이트씩 흘리는 시험으로 확인).

설계:

| 한도 | scan.py 플래그 | 어댑터가 넘기는 값 | 하는 일 |
|---|---|---|---|
| 소프트 | `--time-budget` | timeout − max(60초, 10%), 하한 timeout/2 | 새 파일 호출을 시작하지 않는다. 이미 나간 호출은 끊지 않는다 |
| 하드 | `--deadline-s` | timeout − 15초, 하한 timeout/2 | 호출마다 남은 시간으로 timeout·재시도를 정해 데드라인 안에 끝낸다. 남은 시간 1초 미만이면 요청 없이 skipped |

두 개를 유지한다. 하나로 합치면 데드라인 직전에 시작한 호출이 도중에 끊겨 토큰만 청구되고 결과는 버려진다. ~~소프트 한도가 "이 호출이 끝날 시간이 있는가"를 시작 시점에 판단하고(1800초 기준 1620초 이후는 새 호출 없음)~~ (D-21 정정: 소프트 한도는 호출이 끝날 시간을 판단하지 않는다. 시계가 한도를 넘었는지만 보고 새 호출을 막는다. 한도 직전에 시작한 호출은 데드라인까지 남은 시간만 돌고(1800초 기준 1785 − 1620 = 약 165초) 호출이 그보다 길면 끊겨 토큰만 청구된다. 호출 시간 실측이 없어 그 165초가 맞는지는 모른다. 하네스 타임아웃이 30초 이하면 소프트 예산이 데드라인과 같아져 마지막 호출이 과금 후 잘릴 수 있다. D-21 "아직 안 된 것"), 하드 한도는 그래도 남은 호출을 끊는다. 시계는 둘 다 scan.py `main()` 진입 시각의 `time.monotonic()` 에서 센 초다. 하네스 시계는 spawn 부터라 그 차이(0.08~0.12초, 7회 측정)는 어댑터 여유에 들어 있다.

호출별 한도 `call_limits(rem)`: 최악의 경우 (재시도+1) × timeout + 재시도 × 8초가 남은 시간 rem 안에 든다. 시도 하나가 120초(`MIN_RETRY_ATTEMPT_S`) 밑으로 내려가야 하면 재시도를 하나씩 포기하고, 끝에는 재시도 0 에 rem 전체를 준다. 120초는 실측 없는 선택이다(파일럿 뒤 조정). `--deadline-s` 가 없으면 SDK 기본 그대로다.

| 남은 시간 rem | 시도당 timeout | max_retries |
|---|---|---|
| 1816초 이상 | 600초 | 2 |
| 376 ~ 1816초 (1800초 하네스의 첫 호출은 약 1782초) | (rem − 16) / 3 (약 589초) | 2 |
| 248 ~ 376초 | (rem − 8) / 2 | 1 |
| 1 ~ 248초 | rem | 0 |

`Retry-After` 대기와 응답을 흘리는 서버는 위 계산 밖이다. 그래서 감시를 하나 더 둔다. 메인 스레드는 데드라인 + 2초(`HARD_GRACE_S`)까지만 각 파일 결과를 기다리고, 그 뒤에는 끝난 파일의 결과를 먼저 쓰고(앞 파일이 매달려도 뒤 결과가 갇히지 않는다) 안 끝난 호출을 `ERROR timeout: hard deadline reached (call still running)`(시작도 못 한 파일은 `ERROR skipped: hard deadline reached`)로 남긴다. 그런 호출이 남았으면 요약을 출력한 뒤 `os._exit` 로 끝낸다. 워커 스레드는 데몬이 아니라 정상 종료가 그 호출을 기다리기 때문이다. 풀은 `with` 로 닫지 않는다(`shutdown(wait=True)` 가 같은 이유로 종료를 막는다).

데드라인이 끊은 호출(`ERROR timeout: hard deadline reached (call limited to Ns)`)은 API 장애 신호가 아니라서 연속 오류 차단기에 세지 않는다. 세면 시간 한도로 부분만 스캔한 정상 실행이 exit 2 가 되고 어댑터가 crashed 로 버린다(시험으로 고정). 데드라인 때문이 아니라 호출이 제 시간에 응답하지 못해 난 timeout 은 `ERROR timeout: no response within Ns (max_retries=R)` 이고 차단기에 센다.

Ctrl-C(~~M7 해결~~ D-21 정정: M7 은 이 단락만으로 해결되지 않았다. 프로세스가 끝나는 것은 D-21 H-1, CLI 어댑터는 H-2): ~~풀을 `finally` 에서 `shutdown(wait=False, cancel_futures=True)` 로 닫아 큐의 호출이 더 나가지 않고 진행 중 호출을 기다리지 않는다.~~ (정정: 큐의 호출이 더 나가지 않는 것은 맞다. 그러나 `main()` 이 기다리지 않을 뿐 프로세스는 기다렸다. 워커 스레드는 데몬이 아니라 인터프리터 종료가 join 하므로, KeyboardInterrupt 뒤에도 프로세스는 진행 중 호출이 끝날 때까지 살았다. 3차 재리뷰 측정: 데드라인 60초로 돌린 자식 프로세스가 인터럽트 뒤 54초 더 살았다.) Windows 에서는 무기한 `Future.result()` 가 Ctrl-C 로 깨어나지 않는다(Python 3.12.10 에서 측정: 대기 8초 중 1초에 보낸 인터럽트가 8초 뒤에 왔다). 그래서 결과 대기를 0.5초 단위로 나눈다(`wait_result`, 같은 측정에서 1.5초 만에 왔다). ~~진행 중 호출은 위 timeout 으로 제한된다.~~ (정정: 호출이 timeout 으로 제한되는 것은 맞지만 그건 프로세스가 사는 시간의 상한일 뿐이다. 최대 수백 초이고, 인터럽트를 받고도 그만큼 산다.) ~~`--deadline-s` 없이 손으로 돌릴 때는 SDK 기본 600초라 인터프리터 종료가 그 호출을 기다릴 수 있다.~~ (정정: `--deadline-s` 가 있어도 같다.) ~~실제 Ctrl-C 키 입력으로는 시험하지 않았다(자식 프로세스에 CTRL_C_EVENT 를 보내는 시험은 만들지 않았다). 같은 경로를 같은 프로세스에서 `_thread.interrupt_main()` 으로 시험했다.~~ (정정: 같은 프로세스 시험은 `main()` 이 KeyboardInterrupt 를 내보내는 데까지만 본다. 프로세스가 끝나는지는 보지 못한다. D-21 은 자식 프로세스로 시험한다.)

어댑터 여유 15초(`DEADLINE_MARGIN_S`)의 근거(2026-10-20, 이 머신, `adapters/attacktrail.py` 주석에도 있다): 프로세스 종료 시각 − 데드라인을 spawn 기준으로 쟀다. SDK timeout 이 끊은 경우 +0.61~0.75초(3회), SDK 가 못 끊는 호출을 감시가 버린 경우 +2.23~2.66초(3회, 여유 2초 포함). spawn → `main()` 진입은 0.08~0.12초(7회), 유휴 인터프리터 종료는 0.01초. 최대 2.7초의 약 5.5배를 잡았다. `--jobs` 가 크면 스캔이 겹쳐 느려질 수 있어서다. 부하 속 측정은 하지 않았다.

호출 형태가 바뀌어 API 어댑터 `call_shape` 를 `api-v3` → `api-v4` 로 올렸다. 요청 본문(system 블록, 출력 형식, max_tokens)은 그대로지만, 호출마다 timeout·재시도 횟수가 달라지고 연속 오류에서 실행이 멈추고 402 가 치명이 되어 어떤 파일이 언제 결과를 얻는지가 바뀐다. api-v3 run 과 같은 조건이 아니다. 이전 API run 은 없다(키 없음). CLI 는 호출 방식이 그대로라 `cli-v1` 이다.

### 2. 커버리지 기록과 표시 (H2)

예산·데드라인·오류로 건너뛴 파일이 지표에 숨었다. SCABench truth 에 파일 위치가 없어 건너뛴 파일에 있던 truth 도 Recall 에서 미탐으로 계산된다(분모 정책은 D-9 그대로, 바꾸지 않았다. 사용자 결정 대기).

- `Meta`(`adapters/base.py`)에 선택 필드 `files_total`, `files_scanned`, `files_failed`, `files_skipped` 를 더했다(기본 None). `runner/run.py` 가 `Meta` 에서 manifest `projects[]` 로 그대로 옮긴다(`_run_project`, `rescore`). 기존 manifest·findings.json 은 키가 없어 None 으로 읽힌다.
- 정의: `files_total` = 스캔 대상으로 선택한 파일 수(`--web` 에서 크기 상한으로 안 보낸 파일 포함), `files_scanned` = 응답을 받아 후보를 읽은 파일(후보 0 도 포함), `files_failed` = 호출이나 응답 읽기가 실패한 파일(timeout, 거절, 잘림, 필드 위반, 읽기 오류), `files_skipped` = 요청 없이 건너뛴 파일(시간 예산, 데드라인, 중단, 크기 상한). `total = scanned + failed + skipped`.
- 값은 어댑터가 만든다. API 어댑터는 scan.py 진행 줄(`[i/n] <file>  후보 k` / `ERROR <사유>`, 사유가 `skipped:` 로 시작하면 건너뜀)과 `_run.skipped_large` 에서, CLI 어댑터는 자기 로그에서 센다. 진행 줄이 하나도 없으면 None(미측정)이다.
- 부분 실패 메모(`agent_error`)는 `coverage_meta()` 가 만든다: `6 of 8 files not scanned: 2 failed, 4 skipped (2 too large for --web, 1 hard deadline reached, 1 time budget reached); failed files (first 5 of 7): B.sol (<사유 80자>), ...`. 실패한 파일은 최대 5개까지 이름과 사유를 적고 건너뛴 파일은 사유별 개수만 적는다. 옛 메모 `N of M files failed inside scan.py ...; first: ...` 를 대체한다.
- 리포트(`report/render.py`): 프로젝트별 표에 열 `Coverage (files scanned/total)` 를 더했다(`53/53`, `40/53`, 기록이 없으면 `미측정`). "Partial failures (부분 실패)" 절은 메모가 있거나 커버리지가 100% 미만인 ok 프로젝트를 보인다. Manifest 표에 `agent.snapshot_dir` 행이 생긴다.

### 3. 402 치명 처리와 연속 오류 차단기 (M1, M2, 재리뷰 M8)

SDK 에 402 전용 클래스가 없다. 402 는 일반 `APIStatusError`(`status_code == 402`)로 온다(SDK 의 `_make_status_error_from_response` 로 직접 확인. 529 는 `InternalServerError` 가 아니라 `OverloadedError`). `shared/error-codes.md` 는 402 `billing_error` 를 재시도 불가로 적는다. 그래서 분류를 클래스가 아니라 `status_code` 로 한다(`classify()`, 속성·클래스 이름만 봐서 `--selftest` 가 SDK 없이 돈다).

| 오류 | 분류 | 그 파일 | 연속 카운터 | 결과 |
|---|---|---|---|---|
| 401, 402, 403, 404 | 치명 | ERROR | 해당 없음 | 첫 발생에 남은 호출 중단, exit 2 |
| 429 (SDK 재시도 소진 뒤), 5xx(500, 529 포함) | `429`, `5xx` | ERROR | +1 | 연속 8건이면 중단, exit 2 |
| 400 | `400` | ERROR | +1 | 〃 |
| 연결 오류 | `connection` | ERROR | +1 | 〃 |
| 호출 timeout(데드라인이 끊은 것 제외) | `timeout` | ERROR | +1 | 〃 |
| 데드라인이 끊은 timeout | 없음 | ERROR | 세지 않음 | 정상 종료(exit 0) |
| 413, 408, 409, 422, 로컬 오류 | 중립 | ERROR | 그대로 | 계속 |
| 응답을 받음(HTTP 200, 파싱 실패 포함) | 정상 | 후보 또는 ERROR | 0 으로 | 계속 |
| 요청 없이 건너뜀 | 없음 | skipped | 그대로 | 계속 |

"연속"의 정의: 호출이 끝난 순서(완료 순서)로 센다. 병렬(`--workers`)이면 파일 순서와 다르다. 장애 신호는 시간순이라서다. 한 호출은 SDK 재시도를 합쳐 1건이다. 차단되면 이미 나가 있던 호출(최대 workers − 1개)의 결과는 평소대로 기록하고, 남은 파일은 `ERROR skipped: aborted after 8 consecutive API errors (429,5xx,400)`(범주는 연속 구간에 나온 것을 처음 나온 순서로)로 남긴다. stderr 에 `연속 API 오류로 스캔을 중단했습니다: <사유>. 마지막 오류: <메시지>` 한 줄을 남기고 exit 2 다. 어댑터는 rc≠0 이라 `crashed` 로 기록하고 `raw/scan.jsonl` 은 남긴다. N=8 은 workers 기본값 4 의 두 배로 고른 상수이고 실측 근거가 없다(`_run.breaker_n` 에 기록). 413 을 중립으로 둔 이유: 입력이 큰 요청은 그 파일 하나의 문제다(`error-codes.md` 413 "Too many tokens in input"). 400 은 형식·파라미터 오류라 모든 파일에서 반복되기 쉬워 센다.

재리뷰 M8("429 를 치명으로 바꾸는 변이가 테스트로 안 잡힘")은 한 실행에 400·429·500·529·413 이 섞인 시험으로 막았다. 어느 것이 치명이 되거나 요청을 멈추면 요청 수가 모자라 실패한다.

### 4. 헤더 prompt_sha256 (M3)

scan.py `_run.prompt_sha256` 이 `sha256(ROLE + std)` 라 매니페스트의 `prompt_sha256`(`adapters.attacktrail.system_prompt` = `ROLE + "\n\n" + std`)과 달랐다. 해시 식만 `ROLE + "\n\n" + std` 로 맞췄다. API 로 보내는 system 블록 두 개(ROLE, 기준 문서)는 그대로다. 이제 API 헤더 앞 16자 == `fingerprints()["prompt_sha256"][:16]` 이고 테스트가 고정한다(CLI 는 전부터 같았다). 매니페스트 값 `2d0bb457ca2c605a` 는 그대로다. 이 변경 이전 run 의 헤더 값은 옛 식이라 매니페스트와 안 맞는다.

### 5. CLI 어댑터 파일 읽기 (M4)

`attacktrail_cli.py` `one()` 은 파일 읽기와 프롬프트 조립이 try 밖이라 `.sol` 이름의 디렉터리(`select_files` 는 확장자만 보고 디렉터리도 돌려준다)나 읽기 오류 하나가 `pool.map` 을 거쳐 프로젝트 전체를 crashed 로 만들었다. try 안으로 옮겼다. 그 파일만 `ERROR <예외 이름>: <메시지>` 가 되고 나머지는 이어진다. 호출 방식은 그대로라 `call_shape` 는 `cli-v1` 이다.

### 6. usage 필드 (M6)

API `_usage` 행에 SDK `Usage` 에 있는 `service_tier`, `speed`, `inference_geo` 를 값이 있을 때만 더했다(SDK `types/usage.py` 로 필드 확인). CLI 행의 `input_tokens` 는 세 종류의 합이라 뜻이 달랐다. 기존 값은 그대로 두고 원값을 같은 이름의 추가 키로 남긴다:

| 키 | API 행 (scan.py) | CLI 행 (`scan-cli.jsonl`) |
|---|---|---|
| `input_tokens` | 캐시를 쓰지 않은 입력만 | 세 종류의 합 (기존 그대로) |
| `input_tokens_uncached` | 없음 (`input_tokens` 가 같은 값) | 캐시를 쓰지 않은 입력 원값 (신규) |
| `cache_creation_input_tokens` | 캐시 쓰기 | 캐시 쓰기 원값 (신규) |
| `cache_read_input_tokens` | 캐시 읽기 | 캐시 읽기 원값 (신규) |
| `output_tokens` | 출력(사고 포함) | 같음 |
| `cache_creation` | `{ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}`, 응답에 있을 때만 | CLI usage 에 있을 때만 그대로 (신규) |
| `service_tier`, `speed`, `inference_geo` | 응답에 있을 때만 (신규) | CLI usage 에 있을 때만 (신규) |
| `cost_usd`, `num_turns` | 없음 | CLI 의 `total_cost_usd`, `num_turns` |

`sum_usage()` 는 행에 `input_tokens_uncached` 가 있으면 CLI 행으로 보고 `input_tokens` 를 이미 합한 값으로 읽는다. 없으면 API 행이라 세 종류를 더한다. 두 행 모두 같은 `tokens_in` 정의(세 종류의 합)로 모인다. 세 키를 새로 붙인 CLI 행에 옛 `sum_usage` 를 쓰면 두 번 더해진다. CLI 어댑터는 자기 메모리에서 합산해 `sum_usage` 를 쓰지 않는다.

### 7. 후보 안의 내부 키 (블랙박스 ad)

후보 dict 안에 스키마 밖 키 `_usage` 가 있으면 결과 행이 usage 행처럼 읽혀 어댑터 합산이 부풀었다(기대 (20, 10), 실제 (1000020, 1000010)).
- `check_candidates` 가 `SCHEMA` properties 밖의 키를 전부 위반으로 본다: `ERROR candidates[0] 에 스키마 밖 키: '_usage'`(키 3개까지, `repr` 로 줄바꿈을 가리고 40자로 자른다). `SCHEMA` 자체는 바뀌지 않았고, 이건 검증 로직이다. 모델이 `_status`·`_verify` 같은 내부 키를 내는 경우도 같이 막힌다. CLI 어댑터도 같은 함수를 쓴다. 그 파일의 응답은 받았으므로 usage 행은 남는다.
- 어댑터는 키가 정확히 `_usage` 하나인 행만 usage 로, 정확히 `_run` 하나인 행만 헤더로 읽는다(`_only`). 다른 도구가 쓴 JSONL 에도 안전하다. 동작 변경: D-19 의 "`_usage` 키가 있는 행은 모두 건너뜀"이 사라졌다. `_usage` 와 `_status` 를 함께 가진 행은 이제 후보 행으로 읽힌다(scan.py 는 더 이상 그런 행을 쓰지 않는다).

### 8. 에이전트 스냅샷 (M9 완화)

`internal/` 은 git 밖이라 실행에 쓴 scan.py 와 기준 문서를 나중에 되살릴 수 없다. git 정책은 바꾸지 않았다. API·CLI 어댑터가 실행하기 전에 run 디렉터리 아래에 한 번만 복사한다:

```
<run_dir>/agent_snapshot/tools/scan.py
<run_dir>/agent_snapshot/docs/<기준 문서>.md     (STD_BASE, --web 이면 STD_WEB 도)
```

`internal/` 의 모양(`tools/`, `docs/`)을 그대로 따라서 `python agent_snapshot/tools/scan.py` 가 같은 상대 경로로 기준 문서를 찾는다. 이미 있으면 바이트가 같은지 보고, 다르면 그 프로젝트는 `crashed`(`agent_snapshot ... differs`)다. 한 run 에 두 버전이 섞이면 프롬프트 정체를 알 수 없어서다. `--jobs` 로 프로젝트가 스레드로 돌며 같은 폴더를 쓰므로 잠금을 건다. 키가 없어 실행하지 않은 경우는 복사하지 않는다. manifest `agent.snapshot_dir` 에 `agent_snapshot`(run 디렉터리 기준)을 적고 리포트 Manifest 표에 보인다. repeat 집계 manifest 는 rep-1 의 값을 복사하므로 그 경로는 각 `rep-k/` 아래다.

커밋 여부: `.gitignore` 가 `eval/runs/*/*` 를 무시하고 `manifest.json` 만 예외다. `git check-ignore` 로 `eval/runs/x/agent_snapshot/...`, `eval/runs/x/rep-1/agent_snapshot/...` 가 무시되고 `eval/runs/x/manifest.json` 은 무시되지 않는 것을 확인했고, 테스트가 이를 고정한다. 한계: `--out` 을 `eval/runs/` 밖으로 주면 이 규칙이 닿지 않는다. 내부 문서가 든 폴더라 커밋하지 말아야 하고, 그 경우는 사람이 관리한다. `rescore` 는 D-26 후속부터 폴더가 있으면 채택해 manifest 에 `snapshot_dir` 을 쓴다(전에는 빠졌다).

### 9. 낮음

- L6: `verify()` 가 파일을 다시 읽다 `OSError` 가 나면(사라짐, 권한) 실행 전체를 죽이지 않고 `READ_ERROR` 를 돌려준다. `OK` 로 시작하지 않으므로 `rejected_by_scanner` 가 된다.
- L7: `git rev-parse HEAD` 가 실패하면 stderr 에 `경고: 대상의 git 커밋을 알 수 없어 commit=unknown 으로 기록합니다 (<사유>)` 한 줄. 진짜 저장소에서는 경고가 없고 커밋이 기록된다.
- 시간 시험 여유(재리뷰 M8): `import anthropic` 이 이 환경에서 2.8~3.1초(2026-10-20 측정 2.81·2.94·3.14초)이고 `--time-budget` 이 프로세스 시작부터 재서 고정 5초 예산이면 느린 환경에서 첫 파일이 시작도 못 했다. 시험이 `import anthropic` 시간(`import_s`)을 먼저 재고 한도를 거기에 상대적으로 잡는다(예산 `import_s + 2.5`, 파일당 지연 5초: 첫 파일 시작 시각이 `import_s ± 2.5초` 안이면 성립). 옛 주석의 "약 0.5~2초"는 틀린 수치라 고쳤다.

### 10. 캐시 근거 (재리뷰 M5, 구현하지 않음)

- 병렬 시작의 중복 쓰기: 캐시 항목은 첫 응답이 시작된 뒤에야 읽을 수 있고, 같은 접두사의 병렬 요청은 모두 정가를 낸다(`shared/prompt-caching.md` "Concurrent-request timing"). scan.py 는 비스트리밍이고 문서는 비스트리밍의 "읽을 수 있게 되는 시점"을 따로 적지 않으므로 첫 응답 완료로 보수적으로 본다. 시작 직후 최대 workers 개(기본 4)의 요청이 기준 문서를 각각 1시간 TTL 로 쓴다(쓰기 2배, 읽기는 약 0.1배. D-23: 지금은 5분 TTL 이라 쓰기 1.25배이고 아래 식의 2.0 은 1.25 가 된다). 낭비는 대략 (workers − 1) × (2.0 − 0.1) × 기준 문서 입력 토큰 수 × 입력 단가다. 비-web 기준 문서는 작아 영향이 작다. `--web` 은 기준 문서가 약 22만 자(D-15)라 영향이 크다. 토큰 수는 측정하지 못했다. 실제 중복은 `_usage` 의 `cache_creation_input_tokens` 합과 `cache_read_input_tokens` 로 볼 수 있다.
- `max_tokens: 0` 예열은 `output_config.format`(구조화 출력)과 함께 쓰면 `invalid_request_error`(400)다(같은 문서 "Pre-warming" 의 "Rejected combinations"). scan.py 는 구조화 출력을 쓰므로 같은 요청 모양으로 예열할 수 없다. 구조화 출력만 빼고 예열하면 캐시가 맞는지는 문서에 없다. 첫 실제 요청의 `cache_read_input_tokens` 로 확인해야 한다.
- 대안은 첫 요청이 끝난 뒤 나머지를 병렬로 내는 것이다(fan-out. 같은 문서가 권하는 방식). 첫 파일 지연만큼 예산을 쓴다. 구현은 사용자 결정 뒤에 한다.
- 1시간 대 5분 TTL: 선택 근거가 없다. `prompt-caching.md` "Choosing the TTL" 은 요청 시작 간격이 5분 미만이면 5분 TTL 이 더 싸고(읽을 때마다 갱신), 5~60분이면 1시간이라고 적는다. 병렬 호출이 계속 나가는 스캔은 간격이 5분보다 짧을 테니 5분 TTL 이 유리할 수 있다(추정). 문서의 기준은 시작 간격이고 우리는 그 간격을 측정하지 못했다. 파일럿 뒤 결정한다. ~~지금 코드는 1시간(`ttl: "1h"`)이다.~~ (D-23: 사용자 결정으로 `5m` 로 바꿨다. 호출 간격 측정은 여전히 파일럿에서 한다.)

### 11. 모델과 effort (L1, L2)

- 기본 effort(L1): Claude Sonnet 5 에서 `effort` 를 안 보내면 `high` 다. 번들 문서에서 확인했다: `shared/model-migration.md` "Choosing an effort level on Claude Sonnet 5"("`effort` defaults to `high` when not set (same as Sonnet 4.6 and Opus 4.8)"), 같은 파일 "Migrating to Claude Sonnet 5" 의 요약. 후속 `claude-sonnet-5-5` 도 기본 `high` 이고 단계가 재보정됐다(`shared/models.md`). 스킬 빠른 참조(2026-10-06 캐시)의 "default `high` (equivalent to omitting it) on every current model except Claude Opus 5.5 and Claude Haiku 5.5" 문장 자체는 번들 문서 파일에서 찾지 못했다. 그 예외 둘이 기본 `medium` 이라는 점은 파일에서 확인된다(Python README 의 Opus 5.5 안내, `models.md` 의 Haiku 5.5). 문장은 "스킬 빠른 참조 기준"이다. 적응형 사고가 기본이라 사고 토큰이 `max_tokens` 16000 을 쓴다는 D-17 의 우려는 그대로이고, 실측은 파일럿 뒤다.
- 모델 선택(L2): 미결(사용자 결정). `claude-sonnet-5` 를 쓰는 근거는 문서에 없다. 입출력 가격이 같은($2/$10 per MTok. 이 줄은 처음 캐시 읽기도 $0.20 으로 같다고 적었으나 5.5 는 $0.10 이다. D-29 리뷰 후 수정) 후속 `claude-sonnet-5-5` 가 있다(`shared/models.md`: Sonnet 5 는 "The previous Sonnet", Sonnet 5.5 는 "Successor ... at the same prices"). 5.5 는 `thinking: {type: "disabled"}` 가 400 이고 effort 단계가 재보정되는 등 호출 형태가 달라서 바꾸면 `call_shape` 와 비교 가능성에 영향이 있다. ~~바꾸지 않았다.~~ (D-23: 사용자 결정으로 `claude-sonnet-5-5` 로 바꿨고 `call_shape` 를 `api-v5` 로 올렸다.)

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 85건(D-19 의 44건에서 늘었다). 위치 검증 7(`READ_ERROR` 포함), 가짜 응답 20, `check_candidates` 19(스키마 밖 키 4종 추가), `usage_of` 4, `call_limits` 16(경계 4 + 속성 12), `classify` 16, 차단기 3. SDK 없이 돈다.
- `tests/test_scan_parse.py`(실제 SDK 타입): `usage_of` 의 service_tier 등, 스키마 밖 키 4종과 메시지가 한 줄·3개 한도, `verify()` 의 `READ_ERROR`, `classify` 를 SDK 가 상태 코드로 직접 고른 예외 13종과 연결·timeout 으로(402 가 일반 `APIStatusError` 인 것 포함), `call_limits` 의 경계와 "최악의 경우가 남은 시간 안에 든다" 속성, 차단기 카운터.
- `tests/test_scan_fake_api.py`(로컬 가짜 Messages API, 진짜 SDK, 서브프로세스, 프록시 차단·`NO_PROXY`·빈 임시 홈 유지. 응답을 지연시키거나 한 바이트씩 흘리는 동작과 응답 헤더 지정을 추가):
  - 하드 데드라인: u2 시나리오(첫 파일 hang, 나머지 즉시)에서 데드라인 + 8초의 하네스 한계 안에 exit 0 으로 끝나고, 뒤 파일 결과·usage 가 raw 에 있고, 첫 파일은 `ERROR timeout: hard deadline reached`. SDK timeout 이 못 끊는 호출(응답을 흘림)은 데드라인 + 2초 뒤에 버리고 exit 0. 이미 지난 데드라인은 요청 0. `--deadline-s 0` 거부. 호출별 한도는 서버가 받은 `x-stainless-read-timeout`·`x-stainless-retry-count` 헤더로 확인: 남은 시간이 작으면 재시도 0 에 read timeout 이 남은 시간으로 줄고, `--deadline-s 5000` 이면 재시도 2회에 600초, 데드라인이 없으면 600초. 데드라인이 끊은 호출 8건은 차단기에 안 센다.
  - 분류와 차단기: 한 실행에 400·429·500·529·413 이 섞여도 각각 파일 하나의 ERROR 이고 요청이 계속 나가며, 연속 7건 사이에 응답(정상, refusal)이 끼면 리셋된다. 429·5xx·400 이 413(중립) 사이에 낀 채 연속 8건이면 중단, skipped 문구·stderr 한 줄·exit 2·앞선 결과 유지. 병렬(workers 4)은 완료 순서 기준(요청 수 8~11). 402 는 401·403·404 와 같은 치명 경로(요청 수 2, exit 2).
  - 헤더 `prompt_sha256` == `fingerprints()` 앞 16자, git 경고(저장소 아님은 경고, 진짜 저장소는 경고 없음과 커밋 기록), 후보 안의 `_usage` 는 그 파일의 ERROR 이고 어댑터 합산은 (120, 10)이다.
  - Ctrl-C: 같은 프로세스에서 `_thread.interrupt_main()` 으로 KeyboardInterrupt 를 넣고, 가짜 호출이 20초 매달려도 6초 안에 나오며 큐의 호출이 더 나가지 않는다(옛 코드는 호출이 끝날 때까지 붙잡힌다). (D-21 정정: 이 시험은 `main()` 에서 KeyboardInterrupt 가 나오는 것까지만 본다. 그 뒤 프로세스가 호출을 기다리며 사는 것은 못 본다. 보완은 D-21 H-1.)
  - 시간 예산 시험은 `import_s`(이 환경의 `import anthropic` 시간) 상대로 바꿨다.
- `tests/test_adapters.py`: `--time-budget`·`--deadline-s` 값 5종(1800→1620·1785, 600→540·585, 300→240·285, 100→50·85, 10→5·5), `coverage_meta`(실패 파일 5개 한도와 사유 80자 절단), 어댑터가 데드라인 timeout·실패·건너뜀·크기 상한을 `files_*` 와 메모로 옮기는 것, exit 2 중단은 `crashed` 이고 raw 보존, 진행 줄이 없으면 커버리지 미측정, 키가 정확히 `_usage` 하나인 행만 usage·`_run` 하나인 행만 헤더, CLI 행(합) 합산, 스냅샷(복사 내용, 한 번만, web 기준 문서, 다른 버전 거부, 키 없으면 안 만듦, 병렬 안전), `git check-ignore` 로 `agent_snapshot` 은 무시되고 `manifest.json` 은 아님.
- `tests/test_attacktrail_cli.py`: 디렉터리 `.sol`(읽기 오류)이 그 파일만 ERROR, `_usage` 행 키(합 유지 + 원값 + `input_tokens_uncached`), 스냅샷, 새 메모와 커버리지, `call_shape` api-v4/cli-v1.
- `tests/test_report.py`·`tests/test_runner.py`: 커버리지 열(`40/53`, `53/53`, 기록 없으면 `미측정`), 메모 없이 커버리지가 100% 미만인 프로젝트가 부분 실패에 나옴과 분모 정책 불변, `Meta.files_*` 가 manifest 레코드로 옮겨짐.
- mutation 확인(스크래치 복사본에서만 코드를 바꾸고 레포 파일은 불변. 하네스는 스크래치패드의 `mut4/run_mut.py`). round3_pre 백업(D-20 이전 코드)으로 되돌리면 새 시험이 실패한다: scan.py 37건(가짜 API 14 + 파싱 23), `attacktrail.py` 는 어댑터·CLI 시험 파일이 `coverage_meta` import 에서 수집 단계 실패(2개 파일), `attacktrail_cli.py` 5건, `render.py` 1건, `run.py` 1건, `base.py` 21건(`Meta` 에 필드가 없어 어댑터가 깨진다). 새 코드에 변이 27개를 넣어도 전부 잡힌다(실패 건수):

| 변이 | 잡은 시험 (건수) |
|---|---|
| 데드라인 무시(`rem = None`) | u2 시험, 이미 지난 데드라인, 호출별 한도 헤더, 데드라인 컷이 차단기에 안 셈 (4) |
| 감시 제거 / 감시 뒤 `os._exit` 제거 | 응답을 흘리는 서버 시험 (각 1) |
| 데드라인이 끊은 timeout 을 차단기에 셈 | 데드라인 컷 8건 시험 (1) |
| `call_limits` 가 재시도 대기를 무시 | 최악 시간 속성 시험 (1) |
| 어댑터가 `--deadline-s` 를 안 넘김 | 어댑터 argv 시험 (5) |
| 풀 종료가 `wait=True` / 결과 대기를 폴링하지 않음 | Ctrl-C 시험 (각 1) |
| 402 비치명 | 402 치명 경로, `classify` (2) |
| 429 치명화 | 상태별 분류 시험, 차단기 시험 2개, `classify` (4) |
| 차단기 제거 | 차단기 시험 2개, 카운터 단위 시험 (3) |
| 응답을 받아도 카운터 리셋 안 함 | 상태별 분류·리셋 시험, 카운터 단위 시험 (2) |
| 413 을 오류로 셈 | 차단기 시험, `classify` (2) |
| 중립 오류가 카운터를 리셋 | 차단기 시험, 카운터 단위 시험 (2) |
| 5xx 를 안 셈 | 차단기 시험, `classify` 3건 (4) |
| 헤더 해시 식 원복 | 헤더 == `fingerprints()` 시험 (1) |
| 스키마 밖 키 허용 | 후보 안 `_usage` 시험, 스키마 밖 키 4종 (5) |
| 어댑터가 `_usage` 키가 든 후보 행을 usage 로 읽음 | `_only` 시험 (1) |
| `USAGE_EXTRA` 비움 | usage 필드 시험 (1) |
| `verify()` 가 읽기 오류에서 예외 | `READ_ERROR` 시험 (1) |
| CLI 행을 두 번 더함 | 어댑터·CLI usage 시험 (2) |
| 스냅샷 제거(API) / 다른 버전 조용히 덮음 / 스냅샷 제거(CLI) | 스냅샷 시험 (2, 1, 1) |
| 커버리지를 manifest 에 안 옮김 / 리포트 열 제거 / 메모가 실패 파일 전부 나열 | 러너, 리포트, `coverage_meta` 시험 (각 1) |

  `deadline_ignored` 변이를 처음 돌릴 때 33분이 걸렸다. 같은 변이를 다시 돌리니 46초였고 시험을 하나씩 돌려도 17초 안이다. 그 사이 머신이 멈춘 것으로 보인다(원인은 확인하지 못했다). 재현되지 않았다.
- 전체 실행(eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests -q`) 3회 연속 186건 통과(132.2초, 128.1초, 129.9초). D-19 시점 130건에서 늘었고 불안정한 시험은 없었다.

### 하지 않은 것

- `cost_usd` 계산과 단가 확정, 분모 정책 변경(부분 실패 프로젝트를 분모에 넣을지), 모델·TTL·effort 결정, 캐시 예열과 fan-out 구현, 프록시·CA 환경변수 허용, 웹 정답셋.
- `--time-budget`·`--deadline-s` 의 실제 API 응답 시간 실측. 120초·600초·8건은 전부 임의 선택이다. 파일럿에서 호출 시간 분포를 본 뒤 정한다.
- 부하가 큰 머신에서의 종료 지연 측정, 실제 Ctrl-C 키 입력 시험(D-21: 자식 프로세스에 CTRL_BREAK_EVENT 를 보내는 시험은 추가했다. 키 입력은 아니다), Retry-After 를 단 서버로 감시 경로를 거치는 시험(그 경로는 응답을 흘리는 서버로 대신 시험했다).
- 파일별 ERROR·skipped 를 JSONL 에 남기기(지금도 stdout 에만 있고 어댑터가 `raw/scan.stdout.txt` 로 보존한다).

## D-21. Ctrl-C 로 프로세스가 끝나게 하고, 부분 실패 분류·호출 시간 로그·버린 호출 기록을 고친다 (2026-10-20)

> 범위 (D-22 에서 덧붙임): 제목의 "Ctrl-C 로 프로세스가 끝나게"는 `scan.py` 를 직접 실행할 때(API 경로)와 어댑터를 메인 스레드에서 직접 호출할 때만 참이다. `eval run` 은 러너가 에이전트를 스레드 풀에서 돌려 인터럽트가 어댑터에 닿지 않는다("아직 안 된 것" 표의 러너 행). CLI 경로는 D-22 전까지 파이프를 쥔 `claude` 자손이 있으면 워커가 약 60초 남았다.

3차 재리뷰가 남긴 것만 고친 마지막 수정이다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, `select_files`, 모델, `thinking`·`effort`(보내지 않음)는 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). 호출 요청 형태와 timeout 정책이 그대로라 `call_shape` 는 API `api-v4`, CLI `cli-v1` 유지다(로그 필드 추가는 호출 형태 변경이 아니다). 실제 API 는 한 번도 호출하지 않았고 외부 네트워크 연결도 없다(통합 시험은 127.0.0.1 가짜 서버). 수정 전 파일은 스크래치패드 `backup_20261019/round4_pre/` 에 있다.

### 1. API 경로 Ctrl-C (H-1, scan.py)

결함: `KeyboardInterrupt` 뒤 `concurrent.futures` 가 인터프리터 종료 때 워커 스레드를 join 한다. 워커는 진행 중 호출을 들고 있어서 프로세스가 그 호출이 끝날 때까지(최대 수백 초) 살아 있다. 3차 재리뷰 측정: 데드라인 60초로 돌린 자식 프로세스가 인터럽트 뒤 54.1초 더 살았다(종료 코드 3221225786 = 0xC000013A). D-20 §1 의 "진행 중 호출을 기다리지 않는다"는 `main()` 에만 맞는 말이었다.

수정: `if __name__ == "__main__":` 에서 `main()` 을 감싸 `KeyboardInterrupt` 를 잡는다. stderr 에 `중단됨(Ctrl-C): 진행 중 호출을 버리고 종료합니다` 한 줄을 flush 하고 stdout 도 flush 한 뒤 `os._exit(130)`. 130 은 docstring 의 종료 코드 목록에 넣었다. `finally` 의 틀린 주석("진행 중 호출을 기다리지 않는다, 위 timeout 으로 제한된다")은 프로세스가 기다린다는 사실과 이유로 고쳤다.

결과 파일은 닫혀 있다: JSONL 은 `main()` 의 `with io.open(out, "w")` 한 곳에서만 열린다. 인터럽트가 with 안에서 오면 `finally`(큐 취소) 다음에 with 가 닫고, with 밖(열기 전, 요약 출력 중)에서 오면 열린 파일이 없다. 닫히지 않은 채 올 수 있는 경로를 찾지 못해서 flush·close 코드는 더하지 않았다. 파일별 flush 는 D-19 그대로다.

Windows 에서 자식에게 인터럽트를 보내는 방법을 이 머신에서 확인했다(2026-10-20, Python 3.12.10):
- `CREATE_NEW_PROCESS_GROUP` 자식에 `CTRL_BREAK_EVENT` 가 도달한다. 자식이 `SIGBREAK` 에 `default_int_handler` 를 걸면 `KeyboardInterrupt` 가 된다. 시험이 쓰는 방식이다. 그 자식에서 `Lock.acquire(timeout=20)` 중에 신호를 보내면 `KeyboardInterrupt` 가 20초 뒤에 왔다. D-20 의 "Windows 락 대기는 신호로 깨어나지 않는다"를 다시 확인한 것이다.
- `CTRL_C_EVENT` 는 자식이 `SetConsoleCtrlHandler(None, False)` 로 Ctrl+C 를 다시 켜면 그룹 pid 로도 도달했다. 문서는 그룹을 지정한 `CTRL_C_EVENT` 를 지원하지 않는다고 적고, 콘솔 전체로 퍼지면 pytest 를 죽일 수 있어서 시험에는 쓰지 않았다. 실제 키 입력은 시험하지 않았다.

전후 비교(3차 재리뷰의 `x2_ctrlc_proc.py`, 가짜 서버가 호출을 120초 붙잡고 6초에 `KeyboardInterrupt` 를 넣음, 새 scan.py): 데드라인 없음 전체 6.2초·exit 130, 데드라인 60초 전체 6.6초·exit 130. 옛 코드는 데드라인 60초 실험에서 60.4초까지 살았다.

시험(`tests/test_scan_fake_api.py`): `test_ctrl_c_ends_the_scan_process_without_waiting_for_calls[flag|signal]`. 자식 프로세스가 `runpy` 로 scan.py 를 `__main__` 으로 돌려서 `__main__` 블록을 탄다. 서버는 첫 파일만 즉시 응답하고 나머지를 60초 붙잡는다(workers 2). 요청 3개가 나가고 첫 파일 결과가 파일에 쓰인 뒤 인터럽트를 보낸다. 확인: 5초(`CTRL_C_S`) 안에 끝남, exit 130, stderr 한 줄, 그 뒤 1초를 기다려도 요청 수가 3 그대로(새 호출 없음), JSONL 의 모든 행이 온전한 JSON 이고 마지막이 개행이며 처리한 첫 파일의 후보·usage 가 남음. `flag` 는 자식이 플래그 파일을 보고 스스로 `KeyboardInterrupt` 를 넣는다(어느 환경에서나 돈다). `signal` 은 진짜 OS 신호다. 콘솔이 없어 보낼 수 없으면 skip 한다. 기존 `test_ctrl_c_does_not_wait_for_calls_in_flight`(같은 프로세스)는 `main()` 이 예외를 내보내는 데까지만 보므로 한계를 주석에 적고 남겼다.

### 2. CLI 경로 Ctrl-C (H-2, attacktrail_cli.py)

결함 셋: `pool.map` 이 결과를 타임아웃 없이 기다려서 Windows 에서 인터럽트가 첫 future 가 끝날 때까지 밀린다. 그 사이 워커가 큐에서 새 호출을 꺼내 시작한다. `with ThreadPoolExecutor` 종료가 `wait=True` 라 진행 중 호출을 기다린다. x3 재현(가짜 `call_claude`, 12파일, 4 workers, 1초에 인터럽트, 호출은 3초): `KeyboardInterrupt` 가 `run()` 밖으로 6.04초에 나왔고 인터럽트 뒤에 호출이 1개 더 시작됐다.

수정:
- `pool.submit` 으로 모두 올리고 `scan.wait_result(f)`(0.5초 폴링, scan.py 의 것을 재사용)로 파일 순서대로 결과를 읽는다. 어댑터는 이미 scan.py 모듈을 import 해서 ROLE·SCHEMA·`verify()` 를 쓴다.
- 풀은 `interruptible_pool(cwd)` 컨텍스트 매니저다. 어떤 이유로 빠져나가든(Ctrl-C 포함) `shutdown(wait=False, cancel_futures=True)` 로 큐를 취소하고 실행 중인 `claude` 프로세스를 `kill_live(cwd)` 로 죽인다. with 의 닫는 순서(풀과 프로세스, 결과 파일, 임시 cwd)가 이 순서가 되게 맨 뒤에 열었다.
- 프로세스 추적: 모듈 레지스트리 `_live[cwd]`(실행마다 임시 cwd 가 하나라 그것을 키로 쓴다). `call_claude` 가 `subprocess.run` 대신 `Popen` + `communicate(prompt, timeout)` 으로 부르고 `_live` 에 등록한다. `kill_live` 는 등록된 프로세스를 죽이고 닫힘 표식(`None`)을 남긴다. 닫힌 실행은 새 프로세스를 시작하지 않고 `RuntimeError("interrupted ...")` 를 낸다. 워커가 "다음 파일을 꺼냈지만 아직 `claude` 를 띄우지 않은" 순간에 인터럽트가 닿는 경합을 막는다. 죽인 뒤 프로세스를 최대 5초 기다린다(Windows 는 프로세스가 cwd 로 쓰는 폴더를 못 지운다). 임시 폴더는 `ignore_cleanup_errors=True` 로 지워서 정리 실패가 `KeyboardInterrupt` 를 가리지 않게 했다.
- `KeyboardInterrupt` 는 그대로 위로 올라간다(`run()` 의 `except Exception` 에 안 걸린다). eval 안에서 도는 코드라 `os._exit` 는 쓰지 않았다.
- `call_claude` 의 인자 7개는 그대로다. 시험 대체물(`call_claude` 를 바꾸는 시험과 블랙박스 하네스)이 그 시그니처를 쓴다.

호출 형태: 명령줄(`cli_argv`), stdin(prompt), env(`cli_env()`), 인코딩, cwd, timeout 이 `subprocess.run` 때와 같다. `Popen` 이어도 호출 형태가 아니라 `cli-v1` 을 유지한다. `test_cli_call_shape_is_unchanged_by_popen` 이 `Popen` 에 들어가는 인자를 고정한다. 한계: `Popen.kill` 은 `claude` 프로세스만 죽이고 자손은 안 죽인다. ~~툴·MCP 를 끈 호출이라 자손이 없을 것으로 보지만 확인하지 못했다.~~ (정정, D-22: 4차 리뷰가 측정했다. 파이프를 상속한 자손이 있으면 kill 뒤에도 `communicate()` 가 그 파이프의 EOF 를 기다려 Ctrl-C 뒤 워커가 60.7초, 5초 프로젝트 timeout 이 60.3초 걸렸다. 옛 `subprocess.run` 도 같았다(60.1초). 실제 `claude` 가 그런 자손을 만드는지는 여전히 모른다. D-22 가 출력을 임시 파일로 돌려 자손과 무관하게 했다.) 이 수정으로 `prompt_hash`(옛 신원, 파일 바이트)는 바뀐다. `fingerprints()` 의 `prompt_sha256` 은 그대로다.

x3 재실행 전후: `KeyboardInterrupt` 가 `run()` 밖으로 6.04초 → 1.05초(주입 1.0초), 인터럽트 뒤 시작된 호출 1개 → 0개.

시험(`tests/test_attacktrail_cli.py`): x3 의 회귀(`call_claude` 를 30초 붙잡는 가짜로 바꿔 5초 안에 `KeyboardInterrupt` 가 나오고, 풀린 뒤에도 호출이 workers 수 그대로이며, 결과 파일이 온전함), 진짜 자식 프로세스(argv 만 가짜 `claude` 스크립트로 바꾸고 `Popen`·stdin·env·레지스트리는 실제 경로. 스크립트가 0.1초마다 심장박동 파일을 갱신해서 죽었는지 본다)가 인터럽트 뒤 모두 죽고 새로 안 뜬다, 닫힌 실행은 프로세스를 안 띄운다, `Popen` 인자가 옛 `subprocess.run` 과 같다.

한계(중요): 러너가 에이전트를 스레드 풀에서 돌린다(`runner/run.py` `_single` 의 `ThreadPoolExecutor` + `pool.map`, `--jobs 1` 도 마찬가지). `agent.run` 은 메인 스레드가 아니라서 `eval run` 에서 Ctrl-C 를 누르면 `KeyboardInterrupt` 는 러너의 `pool.map` 대기(메인 스레드)에 오고 어댑터의 `_run_cli` 에는 닿지 않는다. 러너의 `with ThreadPoolExecutor` 도 `wait=True` 라 진행 중 프로젝트를 기다린다. 스크래치 실험으로 확인했다(`_run_project` 를 8초 걸리는 가짜 에이전트 호출로 바꾸고 `_single` 을 직접 부름): `agent.run` 은 `ThreadPoolExecutor-0_0`·`-0_1` 에서 돌았고, 1.0초에 넣은 `KeyboardInterrupt` 는 `_single` 밖으로 8.0초에 나왔다(에이전트가 끝난 시각). 이 수정은 어댑터를 메인 스레드에서 직접 돌릴 때(시험, 스크립트)의 Ctrl-C 를 고친다. `eval run` 에서도 `claude` 호출이 멈추려면 러너에 같은 처리가 필요하다. 범위 밖이라 고치지 않았고 "아직 안 된 것"에 올렸다.

### 3. 부분 실패 오분류 (M-3, render.py)

결함: `_partial` 이 `agent_error` 메모만 있어도 "부분 실패"로 올렸다. 파일을 전부 스캔했고 메모가 "후보 N개 탈락" 같은 안내뿐인 프로젝트도 부분 실패로 보였다.

수정: 부분 실패는 `files_scanned < files_total`, 또는 `files_failed`·`files_skipped` 가 0 보다 클 때만이다(값이 없으면 0 으로 본다). 메모가 있어도 그 조건이 아니면 같은 절 아래 "Agent notes (정보, 부분 실패 아님)" 목록에 따로 나온다. 열은 프로젝트·커버리지(`53/53`, 기록이 없으면 `미측정`)·메모다. 커버리지가 `미측정`이면 부분 실패인지 이 리포트로는 알 수 없고, 목록에 그렇게 보인다. 동작 변경 하나: 커버리지 필드가 없는 옛 run 의 메모(D-19 형식 "N of M files failed...")는 이제 부분 실패가 아니라 이 목록에 `미측정`으로 나온다. 정보는 사라지지 않는다. `_partial` 은 `(부분 실패 목록, 메모 목록)` 을 돌려준다(리포트 안에서만 쓴다).

시험(`tests/test_report.py`): 전부 스캔 + 정보 메모는 부분 실패에 없고 메모 목록에 `53/53` 과 함께 나옴, 실패·건너뜀·부족 각각 하나만으로 부분 실패, 메모만 있고 커버리지 미측정이면 메모 목록에 `미측정`. 기존 시험 둘(메모만으로 부분 실패를 기대하던 것)은 커버리지 필드를 넣어 새 규칙에 맞췄다.

### 4. 데드라인 감시 경합 (블랙박스 관찰 2, scan.py)

결함: `wait_result` 가 `FutureTimeout` 을 낸 직후 `f.running()` 을 보기 전에 호출이 끝나면 `running()` 이 False 라 실제로 나간 호출이 `skipped: hard deadline reached` 로 집계되고 결과와 usage 가 빠졌다.

수정: 감시 로직을 `settle(f, rel, until)` 로 뺐다. 시간 초과 뒤 `running()` 을 먼저 읽고 `done()` 을 나중에 읽는다. 끝났으면 결과를 정상 처리한다. 순서가 중요하다. `done()` 을 먼저 읽고 `running()` 을 나중에 읽으면 그 사이에 끝난 호출이 다시 "시작도 못 함"으로 읽힌다.

시험: 가짜 future 로 결정적으로 재현한다(`tests/test_scan_parse.py`, `--selftest` 에도 4건). `RacyFuture` 는 `result()` 가 시간 초과를 내자마자 끝나고, `FlipFuture` 는 `running()` 을 읽은 직후 끝난다. 둘 다 결과를 받아야 한다. ~~(옛 로직은 skipped)~~ (정정, D-22: `RacyFuture` 는 옛 로직에서 skipped 였고, `FlipFuture` 는 "call still running" 으로 집계됐다.) 그 밖에 실행 중이면 버림, 대기열이면 skipped, 이미 끝났으면 결과.

### 5. 버린 호출 수 (L-1, scan.py)

결함: 감시가 "호출 N개를 버리고"를 `not f.done()` 으로 세서 시작 안 한 대기열 파일도 셀 수 있었다(워커가 큐에서 꺼냈지만 아직 시작 안 한 틈).

수정: `settle` 이 실행 중이라 버린 호출만 `True` 로 돌려주고 `main()` 이 그 수를 센다. 대기열 파일은 요청이 안 나갔으니 `skipped: hard deadline reached` 이고 세지 않는다. 시험: 워커 2개가 둘 다 매달린 6파일 실행에서 "호출 2개", 진행 줄 둘은 `call still running`, 넷은 `skipped`, 서버가 본 요청은 2개.

### 6. 버린 호출의 토큰 (M-1)

데드라인 + 2초 뒤에 버린 호출은 응답을 못 읽어 `_usage` 가 없다. 호출은 나갔고 과금됐으니 토큰 합이 모자란다. 세 곳에 남긴다.
- 진행 줄: `ERROR timeout: hard deadline reached (call still running); 응답을 못 읽어 토큰 미집계`. 앞부분은 D-20 문구 그대로다.
- 요약: `데드라인 뒤에도 끝나지 않은 호출 N개를 버리고 종료합니다. 응답을 못 읽어 그 호출의 토큰은 집계되지 않았다(usage 과소 집계 가능)`.
- 어댑터 메모(`Meta.error`, 리포트의 부분 실패 절): `N call(s) still running at the hard deadline were abandoned: no response was read, so their tokens are not in tokens_in/tokens_out (undercount)`. 어댑터가 진행 줄의 `call still running` 으로 센다(scan.py 의 `STILL_RUNNING` 과 어댑터의 `ABANDONED_MARK`, 시험이 둘의 결합을 고정한다).

같은 이유로 SDK timeout 이 끊은 호출(`ERROR timeout: no response within Ns`)에도 응답이 없어 usage 가 없다. 이 항목은 버린 호출만 다룬다. 서버가 일부 생성분을 청구했을 수 있는 그 경우는 "아직 안 된 것"에 적었다.

### 7. 호출 시간 로그 (H-3 b)

호출 시간 실측이 없어 호출별 timeout 설계(`CALL_TIMEOUT_S`, `MIN_RETRY_ATTEMPT_S`)를 판정하지 못한다. 파일럿에서 분포를 보려고 `_usage` 행에 `call` 을 더했다.

```
{"_usage": {"file": ..., 토큰 키들..., "call": {"started_at_s": 2.204, "elapsed_s": 41.2, "timeout_s": 588.667, "max_retries": 2}}}
```

| 키 | 뜻 |
|---|---|
| `started_at_s` | 호출 시작 시각, `main()` 진입 기준 초(`time.monotonic`) |
| `elapsed_s` | `messages.create` 가 돌려줄 때까지 걸린 초. SDK 재시도와 재시도 사이 대기를 포함한다 |
| `timeout_s`, `max_retries` | 그 호출에 준 시도당 timeout 과 재시도 허용 횟수(`call_limits`). `--deadline-s` 가 없으면 SDK 기본(600초, 2회)과 같은 값을 준 것으로 적는다 |

- 실제 재시도 횟수는 남기지 않았다. `messages.create` 의 반환값(`Message`)에는 없다. `with_raw_response.create` 의 `retries_taken` 으로만 얻는데, 그러면 요청에 `X-Stainless-Raw-Response: raw` 헤더가 붙는다(같은 가짜 서버에 두 형태로 호출해 헤더 차이를 확인했다). 호출 형태(`api-v4`)가 바뀌므로 넣지 않았다. 재시도가 있었는지는 `elapsed_s` 가 `timeout_s` 를 넘는지로 짐작할 수 있다(정확하지는 않다).
- 응답을 받은 호출만 남는다. 응답이 없는 호출(timeout, 4xx·5xx)은 `_usage` 행이 없고(D-19) `call` 도 없다. 그 호출의 시간은 진행 줄의 사유(`timeout: no response within Ns`)로만 본다.
- 어댑터 합산은 안 깨진다. `sum_usage` 는 이름으로 키를 읽고, 행은 여전히 키가 `_usage` 하나뿐이다(`_only`). `call` 이 없는 옛 행도 읽힌다. 시험이 둘 다 고정한다.
- 시험: `--deadline-s` 없음(600초, 2회)과 작은 데드라인(재시도 0, 남은 시간으로 줄어든 timeout)에서 값, 순차 실행의 시작 오프셋 순서, `timeout_s` 가 SDK 가 서버에 보낸 `x-stainless-read-timeout` 과 같음.

### 8. 시험 시간 여유 (M-5)

`elapsed < deadline + 4` 처럼 감시 대기(2초)에 종료 시간을 더하면 여유가 1.5초 안팎인 단언이 있었다. 한도를 상수에서 끌어왔다: 하네스가 죽이는 시각은 `deadline + DEADLINE_MARGIN_S`(어댑터가 데드라인과 하네스 타임아웃 사이에 두는 15초), 종료 시간 상한은 `deadline + scan.HARD_GRACE_S + 5초`(`SLACK_S`), 감시 경로의 하한은 `deadline + scan.HARD_GRACE_S − 0.5`. 상수를 바꾸면 시험 한도가 같이 움직인다. 감시 경로의 종료(데드라인 + 2.2~2.7초, D-20 측정) 대비 상한 여유는 약 1.5초에서 약 4.3초로 늘었다.

### 9. 문서 정정 (M-6)

취소선과 정정 문구로 고쳤고 역사는 지우지 않았다.
- D-20 §1: "소프트 한도가 이 호출이 끝날 시간이 있는가를 판단한다"(판단하지 않는다. 한도 직전에 시작한 호출은 약 165초만 돌고 그보다 길면 끊긴다), "진행 중 호출은 위 timeout 으로 제한된다"(프로세스는 그 시간까지 산다), "M7 해결" 표기(§1, §2 로 완결), 같은 단락의 "실제 Ctrl-C 로는 시험하지 않았다"와 D-20 시험 목록·"하지 않은 것"의 Ctrl-C 문장.
- README: "시간이 넉넉하면 SDK 기본(600초)"(1816초 이상일 때만이다. 1800초 하네스의 첫 호출은 약 589초), 소프트 예산 설명, "뒤 파일의 완료 결과가 사라지지 않는다"(데드라인 경로에 한정. 외부 강제 종료나 크래시 때는 순서 쓰기 때문에 손실될 수 있다). Ctrl-C, `call` 필드, 버린 호출 메모, 부분 실패 분류를 현재 동작에 맞춰 적었다.
- 이 절의 값 몇 개는 `scan.py` 상수에서 계산한 것이다: 소프트 예산 `max(t − max(60, 0.1t), t/2)`, 데드라인 `max(t − 15, t/2)`. t=1800 이면 1620과 1785(차 165초), t ≤ 30 이면 둘 다 t/2 로 같다. 데드라인의 첫 호출 남은 시간이 248초 이상이어야 재시도 1회가 되므로 t 가 263초 미만이면 재시도 0 이다(`call_limits`).

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 89건(D-20 의 85건에 `settle` 4건).
- `tests/test_scan_parse.py`: `settle` 가짜 future 시험 둘(경합 둘, 실행 중·대기열·끝남).
- `tests/test_scan_fake_api.py`: 자식 프로세스 Ctrl-C 둘(flag, signal), 호출 시간 로그 둘(데드라인 없음/작음), 버린 호출 수(대기열 제외), 진행 줄·요약의 "토큰 미집계". 데드라인 시험 셋의 시간 한도를 상수에서 끌어왔다.
- `tests/test_attacktrail_cli.py`: Ctrl-C 둘(가짜 `call_claude`, 진짜 자식 프로세스), 닫힌 실행, `Popen` 호출 형태.
- `tests/test_adapters.py`: 버린 호출 메모, `call` 필드가 합산에 영향 없음.
- `tests/test_report.py`: 부분 실패와 메모 목록 분리 셋, 기존 둘 수정.
- mutation 확인(스크래치 복사본에서만 코드를 바꾸고 레포 파일은 불변. 하네스는 스크래치패드의 `mut5/run_mut.py`). 아래 16개 변이가 모두 잡힌다(첫 실패 시험).

| 변이 | 잡은 시험 |
|---|---|
| `__main__` 의 KeyboardInterrupt 처리를 `raise` 로 | 자식 프로세스 Ctrl-C(flag) 가 25초 안에 안 끝나 실패 |
| `settle` 이 `done()` 분기를 뺌 | `RacyFuture` 시험 |
| `settle` 이 `done()` 을 먼저 읽음 | `FlipFuture` 시험 |
| 대기열 파일을 버린 호출로 셈 | `settle` 대기열 시험 |
| `call` 필드 제거 | `_usage` 행 시험 |
| 요약의 "토큰 미집계" 문구 제거 | 데드라인 감시 시험 |
| 어댑터가 버린 호출 메모를 안 씀 | 어댑터 메모 시험 |
| 렌더 옛 규칙(메모만으로 부분 실패) | 정보 메모 시험 |
| 렌더가 `files_failed` 를 안 봄 | 파일 수 기준 시험 |
| CLI 가 `pool.map` + `with ThreadPoolExecutor` 로 복귀 | CLI Ctrl-C 가짜 호출 시험(60초) |
| CLI 가 폴링 대신 `f.result()` | 같은 시험 |
| CLI 가 `kill_live` 를 안 함 | 진짜 자식 프로세스 시험 |
| CLI 닫힘 표식 제거 | 닫힌 실행 시험 |
| CLI 가 `shutdown(wait=True)` | CLI Ctrl-C 가짜 호출 시험 |
| CLI 가 `cancel_futures` 를 뺌 | 같은 시험 |
| `Popen` 에서 `stdin=PIPE` 제거 | 호출 형태 시험 |

- 3차 재리뷰 실험 재실행: x2(자식 프로세스 Ctrl-C)와 x3(CLI Ctrl-C)을 새 코드로 다시 돌려 위 전후 수치를 얻었다.
- 블랙박스 회귀(스크래치패드 `blackbox_round3/bb3.py` 의 `current` 묶음 c1·c2·c3 각 1회, 하네스는 고치지 않음): 54건 중 48건 PASS, 6건 FAIL. 6건 모두 새 동작이 하네스의 기대값과 어긋난 것이고 코드 결함이 아니다.
  - `_usage` 행을 키까지 정확히 비교하는 기대 셋(c1 `x`, c2 `v4`·`v1`): 행에 `call` 이 더해졌다. `call` 을 빼면 행이 기대와 같고(`x` 5행, `v4`·`v1` 8행) 토큰 합도 같다. `v4` 와 `v1` 의 행이 서로 다르다는 하네스 점검(`usage_rows 다름`)도 `call` 의 시작 오프셋·소요가 실행마다 다르기 때문이다. `call` 을 빼면 같다.
  - 옛 메모 문구를 부분 문자열로 기대하는 셋(c3 `ak`·`al`·`am2`): 하네스는 `AL1.sol (timeout: hard deadline reached (call still running))` 을 찾는데 새 메모는 `AL1.sol (timeout: hard deadline reached (call still running); 응답을 못 읽어 토큰 미집계)` 다. 같은 시나리오에서 커버리지(`[3, 2, 1, 0]`), usage 가 있는 파일, 토큰 합(20, 10), exit 0, 버린 호출 요약 줄은 기대와 같고, 어댑터 메모에 버린 호출 문구가 붙었다.

### 하지 않은 것

- 러너(`runner/run.py`)의 Ctrl-C 처리. `eval run` 에서 `claude` 호출이 Ctrl-C 로 멈추지 않는 문제가 남는다(§2 한계).
- 외부 강제 종료 때 완료 결과 손실을 막는 임시 파일 병합(M-2). 결과는 여전히 파일 순서로 쓴다.
- 실제 재시도 횟수 기록, 응답이 없는 호출의 시간·토큰 기록(§6, §7).
- 시간 상수(600초, 120초, 8건, 15초)와 호출별 timeout 설계의 변경. 실측 전이라 그대로다.
- `rescore` 시험(M-4), `--repeat` 의 `rep-k/manifest.json` 커밋(블랙박스 관찰 3), CLI 메모 사유 80자 절단(관찰 4). 범위 밖이다.
- 블랙박스 하네스 수정. `x` 시나리오가 새 필드 때문에 실패하는 것은 하네스 기대값 문제다(아래 검증 결과).
- 실제 Ctrl-C 키 입력 시험, 부하 속 종료 지연 측정.

## 검증 결과

```
pytest                       54/54 통과
목 판정자 end-to-end           3개 프로젝트 실제 clone, run/render/compare/repeat 동작
판정자 nethermind + 키 없음     clone 전에 즉시 중단, 디렉토리 생성 안 됨
holdout 가드                  플래그 없으면 exit 2 + 경고
scan.py                      SHA256 F724874070E5F7FF8D8C4A7F0C9665BC867C23E36573CB553C0C56F7D7383871 전후 동일
```

위 블록은 2026-10 초 시점의 기록이다(pytest 54건, scan.py 파일 해시도 그때 값. D-14 의 `B8B761E2E0F2CB4B…` 도 2026-10-18 시점 값). 지우지 않고 시점을 구분해 아래에 현재 값을 적는다.

```
2026-10-19, D-19 이후
pytest                       130건 통과 (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests -q`, 50.9초)
scan.py --selftest           selftest OK (44 cases)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, D-17·D-18 전후와 같음)
call_shape                   api-v3 (API), cli-v1 (CLI)
scan.py                      SHA256 B68317A6E223D0A7746BAFF49B613AB1BAAD671F8EDC224672F216B6F271D778
```

```
2026-10-20, D-20 이후
pytest                       186건 통과 (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests -q`, 3회 연속, 128~132초)
scan.py --selftest           selftest OK (85 cases)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, D-17~D-19 전후와 같음)
call_shape                   api-v4 (API), cli-v1 (CLI)
scan.py                      SHA256 E2DD895182109343E3D24FB5D574DE53CB3E2EAFAC81FBC9DE9EC95A376D3DAA
```

```
2026-10-20, D-21 이후
pytest                       201건 통과 (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider`, 2회 연속 128.9초·131.4초). D-20 의 186건에서 15건 늘었다. 그 전에 돌린 한 번은 1건 실패(기존 병렬 순서 시험이 새 `call` 필드를 행 비교에 포함)였고 시험을 고친 뒤 다시 돌렸다
scan.py --selftest           selftest OK (89 cases)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, D-17~D-20 전후와 같음)
call_shape                   api-v4 (API), cli-v1 (CLI)
scan.py                      SHA256 8E2C980D282738E7FDA21D58C3CCDDC1AAF19FEAE921A4B288708B879AFC7A1A
블랙박스 bb3 current 묶음      c1 25/26 PASS, c2 10/12, c3 13/16 (FAIL 6건은 하네스 기대값 문제, D-21 테스트 절)
```

```
2026-10-21, D-23 이후
pytest                       249건 통과, 실패 0건. 전체를 한 번에 돌리면 지난번 메모리 부족으로 죽어서 파일별로 나눠 돌렸다
                             (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests\<파일>.py -q -p no:cacheprovider`)
                             adapters 41, attacktrail_cli 17, cli 24, report 24, runner 20,
                             scan_fake_api 48 (189초), scan_parse 65, scan_select 4, scoring 6
                             D-22 의 204건에서 45건 늘었다 (D-23 테스트 절)
scan.py --selftest           selftest OK (95 cases)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, D-17~D-22 전후와 같음)
call_shape                   api-v5 (API), cli-v1 (CLI)
effort (manifest agent)      high (API), n/a (CLI)
scan.py                      SHA256 960C33D39316051D07956ACFE045F8C9385E076A879E6128D034A831A6E3C62E
                             (D-22 끝 시점은 E0BA4BAD63052F115E861C7B10CD233DD31251E9CA9E5EF7BB7D1C983A96A744)
```

```
2026-10-21, D-24 이후
pytest                       283건 통과, 실패 0건. 파일별로 나눠 돌렸다
                             (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests\<파일>.py -q -p no:cacheprovider`)
                             adapters 51, attacktrail_cli 18, cli 25, report 24, runner 25,
                             scan_fake_api 61 (235초), scan_parse 69, scan_select 4, scoring 6
                             D-23 의 249건에서 34건 늘었다 (D-24 테스트 절)
scan.py --selftest           selftest OK (107 cases)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, D-17~D-23 전후와 같음)
call_shape                   api-v6 (API), cli-v1 (CLI)
model / effort (manifest)    claude-fable-5-1 / xhigh (API), claude-fable-5-1 / n/a (CLI)   (D-24 시점은 claude-mythos-5-1, D-25 에서 바꿨다)
default_timeout_s            10800 (API), 1800 (CLI)
scan.py                      SHA256 C6EAA91DB8438D520A73B605FE84C0FF337514A6FB021110B2E3CDABE9F04E2F
                             (D-23 끝 시점은 960C33D39316051D07956ACFE045F8C9385E076A879E6128D034A831A6E3C62E)
```

목 실행 숫자는 전부 의미 없다. 판정이 내용과 무관한 카운터로 정해진다.

## 리뷰에서 잡아 고친 것 (13건 중 9건 확인, 4건 반박)

- blocker: 판정 안 된 truth 가 분모에서 빠지던 문제 (D-9)
- blocker: compare 가 finding 0건 run 의 회귀를 놓치던 문제
- major: compare 가 두 run 의 채점 대상 프로젝트 집합이 같은지 확인하지 않던 문제
- major: POSIX 에서 대상 저장소의 심볼릭 링크가 체크아웃되던 문제 → `core.symlinks=false` + 사후 검사
- minor: git 하위 프로세스가 API 키를 상속하던 문제 → 허용목록 env
- 테스트 중 발견: `pytest` 가 클론된 대상 저장소의 테스트 파일을 수집해 import 했다. 대상 코드를 실행하는 경로다. `conftest.py` 에 `collect_ignore = ["datasets", "runs"]`

## D-22. CLI 출력을 임시 파일로, 커버리지 미측정 메모를 부분 실패로, wait_result 를 고친다 (2026-10-20)

4차 리뷰(조건부)가 남긴 것만 고쳤다. 시스템 프롬프트 본문, `SCHEMA`, `select_files`, 모델은 그대로다. `prompt_sha256` 앞 16자 `2d0bb457ca2c605a`, `call_shape` API `api-v4`, CLI `cli-v1`. 실제 API 호출과 외부 네트워크 연결은 없다.

1. CLI 출력은 임시 파일로 (`adapters/attacktrail_cli.py` `call_claude`). stdout·stderr 를 파이프 대신 `tempfile.TemporaryFile` 로 받고 끝난 뒤 읽는다. 파이프면 그 파이프를 상속한 `claude` 자손이 kill 뒤에도 쥐고 있어 `communicate()` 가 EOF 를 기다렸다(Ctrl-C 뒤 워커 60.7초, 5초 timeout 이 60.3초). 이제 `communicate()` 는 `claude` 본체만 기다린다. `claude` 가 읽는 것(명령줄, stdin, env, cwd, 인코딩)은 그대로라 `cli-v1` 유지. 임시 파일은 호출이 끝나면 닫히고 지워진다.
2. 임시 cwd 정리 실패를 알린다. `ignore_cleanup_errors=True` 로 조용히 남던 `at-cli-*` 폴더를 지우지 못하면 stderr 에 경로와 이유 한 줄을 남긴다.
3. 커버리지 미측정 메모는 부분 실패로 (`report/render.py` `_partial`). D-21 은 커버리지를 기록하지 않은 프로젝트의 메모를 "Agent notes (정보, 부분 실패 아님)" 에 넣었다. 리포트가 모르는 상태를 "아님"이라고 쓰는 셈이고, 옛 D-19 형식의 "N of M files failed" 도 부분 실패 표에서 빠졌다. 이제 이런 메모는 부분 실패 표에 `[coverage 미측정] <메모>` 로 오른다. Agent notes 는 커버리지가 기록되고 완전한 프로젝트의 메모만 담는다. 보수적인 쪽을 택했다. 분모 정책은 그대로다.
4. `wait_result` (`scan.py`). `FutureTimeout` 은 내장 `TimeoutError` 라 작업 자신이 `TimeoutError` 를 던지면 "아직 안 끝남"으로 삼켜졌고 `until=None` 이면 무한히 돌았다. 지금 경로(`scan_one`, CLI `one()`)는 예외를 밖으로 내지 않아 실제로 일어나지는 않았다. 시간 초과를 잡은 뒤 `f.done()` 이면 `f.result()` 로 작업의 결과나 예외를 그대로 낸다. 대기 방식(`f.result(step)`)은 그대로 둬서 `settle` 의 경합 시험(`RacyFuture`, `FlipFuture`)이 계속 같은 지점을 본다.
5. 문서 정정: D-21 제목 범위(위 D-21 머리 인용), D-21 의 자손 한계 문장과 `RacyFuture`/`FlipFuture` 설명, README 의 "토큰 미집계" 문구.

시험: `test_cli_timeout_is_not_held_by_a_descendant_of_claude`(파이프를 쥔 자손이 있어도 5초 timeout 이 약 5초에 끝남), `test_cli_ctrl_c_is_not_held_by_a_descendant_of_claude`(Ctrl-C 뒤 풀 워커가 5초 안에 끝남), `test_wait_result_raises_a_task_timeout_error_instead_of_waiting`, `test_render_partial_follows_the_file_counts_not_the_memo` 갱신(미측정 메모는 부분 실패, 완전한 프로젝트의 메모는 Agent notes), `test_cli_call_shape_is_unchanged_by_popen` 갱신(stdout·stderr 는 파이프가 아닌 임시 파일).

하지 않은 것: 러너 Ctrl-C, 자손 프로세스 종료(kill 은 여전히 `claude` 본체만), 실제 키 입력 시험.

## D-23. 모델 claude-sonnet-5-5, effort 명시, 캐시 TTL 5분, 서버 측 폴백, 콘솔 부분 실패 요약 (2026-10-21)

사용자 결정 여덟 가지를 반영했다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, `select_files`, `max_tokens` 16000 은 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). 실제 API 는 한 번도 호출하지 않았다(키 없음, 사용자 승인 전). 시험은 127.0.0.1 가짜 서버와 가짜 응답 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021/` 에 있다.

### 1. 결정과 반영 위치

| 결정 | 반영 |
|---|---|
| 모델 `claude-sonnet-5-5`(날짜 접미사 없음) | scan.py `--model` 기본값과 독스트링. CLI 어댑터는 scan.py 소스의 기본값을 읽어서 `claude -p --model claude-sonnet-5-5` 가 된다 |
| effort 를 `output_config` 에 명시. 기본 `high`, `xhigh` 허용 | scan.py `--effort {high,xhigh}`. 어댑터 `effort=` 인자, `eval run --effort`, manifest `agent.effort` |
| 캐시 TTL `5m` | 기준 문서 system 블록의 `cache_control.ttl` (`CACHE_TTL`) |
| 서버 측 폴백 `"default"` | beta 엔드포인트 호출 + `fallbacks="default"` |
| 부분 실패를 따로 알림 | `eval run` 끝에 stderr 요약. 리포트의 "Partial failures" 절은 유지 |
| 치명 오류·차단기는 언급만 | 동작 그대로. 요약에 crashed 프로젝트와 사유 한 줄 |
| Ctrl-C 동작 유지 | 손대지 않았다 |
| 호출 시간 설계는 파일럿 뒤 | 손대지 않았다 |

### 2. 호출 형태 (SDK 1.15.0 소스와 번들 문서로 확인)

- 호출은 `client.with_options(...).beta.messages.create(...)` 다(데드라인이 없으면 `client.beta.messages.create`). 인자는 `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`, `output_config={"effort": ..., "format": {...}}`. SDK 의 `resources/beta/messages/messages.py` `create` 시그니처에 `betas`·`fallbacks`·`output_config` 가 있고, `types/beta/beta_fallbacks_param.py` 가 `Union[Iterable[BetaFallbackParam], Literal["default"]]`, `types/anthropic_beta_param.py` 가 `server-side-fallback-2026-07-01` 을 안다.
- SDK 는 `POST /v1/messages?beta=true` 로 보낸다(소스의 `_post("/v1/messages?beta=true", ...)`). 가짜 서버는 쿼리를 뗀 경로로 라우팅하고 쿼리는 그대로 기록한다. 옛 가짜 서버는 `self.path == "/v1/messages"` 로 비교해서 beta 경로에 404 를 줬다. 시험이 `path == "/v1/messages?beta=true"` 를 고정한다.
- 헤더 `anthropic-beta` 는 `server-side-fallback-2026-07-01` 하나다. `"default"` 형태가 쓰는 값이다. `-2026-06-01` 은 배열 형태 전용이고 섞으면 400 이다(`shared/model-migration.md` "Header depends on the form you use"). 시험이 값이 정확히 그 하나인지 본다.
- Sonnet 5.5 에서 `"default"` 는 `cyber`·`frontier_llm` 거절만 Claude Sonnet 5 로 다시 보낸다. `bio`·`reasoning_extraction`·`general_harms` 는 재시도하지 않는다(같은 문서 "Safeguards and fallback"). 폴백은 Claude API 전용이고 Batches API 는 거부한다. scan.py 는 Batches 를 쓰지 않는다.
- 응답은 `BetaMessage` 다. fallback 블록과 `BetaFallbackMessageIterationUsage` 가 든 응답을 SDK 의 `BetaMessage.model_validate` 로 만들어 `parse_candidates`·`usage_of` 에 넣어 확인했다. `content` 에 `type == "fallback"` 블록이 들어가도 text 블록만 읽으므로 그대로 읽힌다. 폴백 체인 전체가 거절하면 최종 `stop_reason` 이 `refusal` 이라 기존 refusal 처리(`ScanError`)로 간다.
- `classify` 와 `Breaker` 는 상태 코드와 예외 클래스 이름만 본다. beta 리소스도 같은 예외 클래스를 낸다. 가짜 서버 시험이 이제 전부 beta 경로로 가는데, 401·402·403·404 치명 처리, 429·5xx·400·413 분류, 차단기, 데드라인 시험이 고치지 않고 통과한다.
- beta 엔드포인트가 400 을 주는 경우(예: 계정이 폴백을 허용하지 않음)는 기존 분류대로다. 400 은 치명(401~404)이 아니라 파일별 `ERROR` 이고 연속 8건이면 차단기가 멈춘다(exit 2). 시험 `test_beta_rejection_400_is_classified_like_any_400_not_fatal` 이 모든 요청에 400 을 줘서 요청 8건에서 멈추고 stderr 에 `aborted after 8 consecutive API errors (400)` 가 나오는 것을 고정한다. 첫 400 에서 멈추게 바꾸지 않았다(사용자 지시: 기존 분류).
- 안 바뀐 것: `thinking` 을 보내지 않는다. Sonnet 5.5 는 `thinking: {type: "disabled"}` 와 강제 `tool_choice` 가 400 이지만 둘 다 안 보내고, 시험이 본문에 `thinking`·`tools`·`tool_choice` 가 없는 것을 고정한다. 빈 thinking 블록이 text 앞에 오는 응답은 D-17 의 text 블록만 읽기로 그대로 처리된다.
- 폴백 모델이 rate limit 이거나 과부하면 폴백 시도 없이 앞의 거절이 그대로 오고 `stop_details.recommended_model` 이 채워질 수 있다(문서). 그 값은 읽지 않는다. 이 경우도 `stop_reason=refusal category=<값>` ERROR 로 남아서 체인 전체 거절과 구별되지 않는다.

### 3. `_usage` 행: 응답 모델, 폴백 여부, 토큰 집계

행마다 키 둘을 더했다. `model` 은 메시지를 만든 모델(`response.model`), `fallback` 은 폴백 모델이 그 응답을 냈는지(bool)다. `fallback` 은 `usage.iterations` 에 `type == "fallback_message"` 항목이 있고 `stop_reason` 이 `refusal` 이 아닐 때만 `true` 다. 폴백 content 블록은 판정에 쓰지 않는다. 문서가 sticky 라우팅 턴에는 블록이 없을 수 있다고 적는다(시험: 거절 시도 항목 없이 `fallback_message` 하나뿐인 응답도 `true`). 폴백 모델까지 거절한 응답은 `false` 이고 그 파일은 `ERROR` 다.

토큰은 과소 집계하지 않는 쪽으로 잡는다. 근거(문서): 최상위 `usage` 는 반환된 메시지를 만든 시도 하나만 담고, `usage.iterations` 가 시도별 기준이다. 출력 전에 거절된 시도의 과금 여부는 범주에 따라 다르고, 폴백 시도는 폴백 모델 요율로 과금된다. 선택:

| 키 | 값 |
|---|---|
| `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens` | 시도가 둘 이상이면 키마다 시도별 합계와 최상위 값 중 큰 쪽. 시도가 하나이거나 iterations 가 없으면 최상위 값 그대로 |
| `cache_creation` | 시도가 둘 이상이고 시도별 값이 있으면 시도별 합계(`ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens`) |
| `returned` | 시도가 둘 이상일 때만. 최상위 usage 의 네 값(응답을 낸 시도) |
| `attempts` | 시도가 둘 이상일 때만. `[{type, model, 네 토큰 키}]` |

이유:
- 비용 상한($100, 사용자 결정 2026-10-21 "파일럿 범위, 비용상한: 100불") 추적에서는 과소 집계가 과대 집계보다 나쁘다. 출력 전 거절이 과금되지 않는 범주면 시도별 합계는 실제 청구보다 크다. 상한으로는 맞는 방향이다.
- 최상위 값만 쓰면 폴백이 일어난 파일에서 거절된 시도의 입력(프롬프트 전체와 캐시 쓰기)이 통째로 빠진다. 시험의 가짜 응답: 거절 시도 입력 100, 폴백 시도 입력 10 이면 최상위는 10, 합계는 110.
- 키마다 `max` 를 쓰는 것은 응답이 모순될 때(합계가 최상위보다 작을 때)의 안전판이다. 문서대로면 합계는 최상위 이상이다.
- 최상위 값과 시도별 값을 둘 다 남겨서, 나중에 거절된 시도와 응답한 시도를 가를 수 있다. `returned`·`attempts` 는 감사용이고 어댑터 `sum_usage()` 는 네 키만 읽는다. 그래서 이중으로 더해지지 않고(시험) 어댑터의 합산 코드는 바뀌지 않았다.
- 캐시는 모델별이라 폴백 쪽은 차가운 캐시이고, 서버가 크레딧식 재가격을 자동으로 적용한다(문서). 이 합계는 그 재가격을 반영하지 않은 상한이다.
- 시도가 하나인 일반 응답의 행은 전과 같다. `model` 과 `fallback: false` 만 늘었다.

비용 식(D-19 §4 정정): TTL 이 5분이라 쓰기 배수는 1.25 다. 단가는 사용자가 확인한 Sonnet 5.5 값이다($2/$10 per MTok, 캐시 읽기 $0.10, 5분 쓰기 $2.50, 1시간 $4). Sonnet 5 와 입출력·쓰기 가격이 같아서 폴백 시도에도 같은 식을 쓴다. 정정(D-29 리뷰 후 수정, 2026-10-23): 이 줄은 처음 캐시 읽기를 $0.20, 아래 식의 읽기 배수를 0.1 로 적었다. $0.20 은 Sonnet 5 의 값이고 Sonnet 5.5 는 입력의 0.05배인 $0.10 이다(공식 가격표 `https://platform.claude.com/docs/en/about-claude/pricing` 의 각주 2, 2026-10-23 확인. 번들 claude-api 문서의 2026-10-06 캐시 표는 5.5 도 $0.20 으로 적어 틀렸다). 식의 배수도 0.05 로 고쳤다.

```
비용 상한 = P_in × (input_tokens
                  + 1.25 × cache_creation.ephemeral_5m_input_tokens
                  + 2.0  × cache_creation.ephemeral_1h_input_tokens
                  + 0.05 × cache_read_input_tokens)
          + P_out × output_tokens            (P_in = $2/MTok, P_out = $10/MTok)
```

`cost_usd` 는 여전히 계산하지 않는다(None).

### 4. 어댑터, 리포트, 콘솔 요약

- `AttackTrailAgent(effort=...)`: 값이 있으면 scan.py 에 `--effort` 로 넘긴다. 없으면 넘기지 않고 `effort()` 가 scan.py 소스의 `--effort` 기본값을 읽는다(모델과 같은 방식. `_scan_constants` 가 `--model` 과 `--effort` 의 `default` 를 읽는다). `fingerprints()` 가 `effort` 키를 더한다. D-14 의 세 지문 밖에 키가 하나 늘었고 run 비교 기준은 여전히 `prompt_sha256` 이다. 현재 값은 `high`.
- CLI 어댑터: `effort()` 가 `"n/a"` 를 돌려준다. `claude -p` 에는 effort 도 폴백도 보내지 않는다. `eval run --agent attacktrail-cli --effort ...` 는 거부한다(모델에 닿지 않는 설정을 기록하지 않으려고). 폴백 응답 수는 None(미측정)이다. 호출 방식은 그대로라 `call_shape` 는 `cli-v1` 이다. `claude -p --model claude-sonnet-5-5` 는 scan.py 기본값이 바뀐 결과일 뿐이고, CLI 가 이 모델 이름을 받아들이는지는 실제 호출로 확인하지 못했다.
- `Meta` 에 선택 필드 `files_fallback`, `fallback_models`(기본 None)를 더했다. API 어댑터가 `_usage` 행에서 `fallback_usage()` 로 센다(D-24 정정: 센 것은 "폴백 모델이 응답을 낸 파일 수"다. 그 응답이 파싱에 실패하거나 `max_tokens` 로 잘려 ERROR 가 된 파일도 포함한다. 폴백 모델까지 거절한 파일만 응답이 없어 뺀다). 행이 하나도 없으면 None, 행이 있는데 폴백이 없으면 `0` 과 `[]`, 옛 scan.py 가 쓴 행(`fallback` 키 없음)은 폴백이 아닌 것으로 센다. 러너의 `META_KEYS`(커버리지 4개 + 이 둘)가 manifest `projects[]` 로 옮긴다. `COVERAGE_KEYS` 는 기존 시험이 쓰므로 그대로 뒀다.
- 리포트: "Partial failures" 절 아래에 "Fallback responses (폴백 응답)" 절을 더했다. 프로젝트별로 `폴백 응답 12 파일 (모델 claude-sonnet-5)`. 기록한 프로젝트가 하나도 없으면 `미측정`, 기록했고 0 이면 `없음`. Manifest 표에는 `effort (output_config.effort)` 행이 생긴다(manifest 에 있을 때만).
- 콘솔 요약: `runner/run.py` `run()` 이 끝에 `_print_summary()` 로 stderr 에 낸다. stdout 은 `eval run` 이 찍는 run 경로 한 줄 그대로다. `--repeat` 이면 rep 마다 하나다. 내용은 `report/render.py` `run_summary()` 가 만든다.

```
[run 요약] <run 디렉터리 이름>
부분 실패 프로젝트 2개
  <프로젝트>: 실패 3/53 파일, 건너뜀 2, 커버리지 48/53
  <프로젝트>: 커버리지 미측정
crashed 프로젝트 1개
  <프로젝트> [crashed]: <사유 한 줄, 200자까지>
폴백 응답 프로젝트 1개
  <프로젝트>: 폴백 응답 12 파일 (모델 claude-sonnet-5)
```

  부분 실패의 기준은 리포트 "Partial failures" 절과 같은 함수(`_partial`)라서 둘이 어긋나지 않는다. "crashed 프로젝트" 줄은 agent 가 ok 로 끝나지 못한 프로젝트를 모두 낸다. 사용자 지시는 crashed 였는데 `timeout` 도 `[timeout]` 으로 같은 줄에 나온다(둘 다 지표 분모에서 빠지는 실패라 하나만 빼면 요약이 모자란다). 제목은 있는 상태만 이어서 `crashed/timeout 프로젝트 N개` 가 된다. 폴백 줄은 폴백 수를 기록하는 에이전트(API 어댑터)에서만 나온다. 요약 생성이 실패해도 끝난 run 을 오류로 바꾸지 않고 `요약을 만들지 못했다` 한 줄만 낸다.
  치명 오류(401·402·403·404)와 연속 오류 차단기의 동작은 바꾸지 않았다. 그때 scan.py 가 exit 2 로 끝나고 어댑터가 `crashed` 로 기록한 결과가 요약에 한 줄로 보일 뿐이다.

### 5. 호출 형태 버전

`call_shape` 를 `api-v4` → `api-v5` 로 올렸다(D-14 규칙). 엔드포인트(beta), 헤더, 요청 본문(`fallbacks`, `output_config.effort`), 캐시 TTL, 기본 모델이 바뀌었다. 이전 API run 은 없다(키 없음). CLI 는 `cli-v1` 이다.

### 6. 바꾸지 않은 것

(D-24 정정: 아래 호출 시간 설계와 `max_tokens` 는 D-24 에서 사용자 결정으로 바꿨다. 이 절은 D-23 시점 기록이다.)

- 호출 시간 설계: `CALL_TIMEOUT_S` 600, `MIN_RETRY_ATTEMPT_S` 120, 연속 오류 8건, 어댑터 여유 15초, 소프트·하드 한도. 파일럿으로 호출 시간 분포를 잰 뒤 사용자에게 묻기로 했다.
- `max_tokens` 16000. effort 를 `xhigh` 로 올리면 사고 토큰 사용이 늘어 이 한도에 먼저 닿는지는 파일럿에서 본다. 모자란 파일은 `ERROR stop_reason=max_tokens` 로 남으므로 그 빈도로 잰다.
- Ctrl-C 동작(러너의 한계 포함), 치명 오류·차단기 동작, 부분 실패 프로젝트의 분모 정책.
- ROLE, 기준 문서, `SCHEMA`, 사용자 프롬프트 틀, `select_files`, 후보 검증.

### 7. 알려진 한계

- 문서의 sticky 라우팅(한 번 폴백한 요청 계열은 약 1시간 폴백 모델이 바로 받는다)이 이 스캔의 파일들에 걸리는지는 실측 전이다. 걸리면 `fallback` 인 파일 수는 실제 거절 횟수보다 크다. 리포트 절 설명에도 적었다.
- `--repeat` 실행은 rep 별 `files_fallback` 을 본다. 0 이 아니면 그 rep 의 일부 파일은 요청한 모델이 아니라 다른 모델의 결과로 본다(sticky 라우팅 가능성, 실측 전).
- 폴백 모델은 Sonnet 5.5 의 thinking 블록 없이 돈다(문서). 폴백 응답의 후보 품질이 요청한 모델과 같다고 볼 수 없다. 리포트가 그 파일 수를 보이는 이유다. 폴백 파일의 후보를 따로 채점하지는 않는다.
- 5분 TTL: 같은 기준 문서를 읽는 호출이 5분 안에 이어지면 읽을 때마다 갱신된다. 호출 간격이 5분을 넘는 구간(느린 파일, 재시도)에서는 캐시가 식어 쓰기가 다시 난다. 병렬 시작의 중복 쓰기(D-20 §10)는 그대로이고 쓰기 단가만 2배에서 1.25배로 내려갔다. 실제 쓰기·읽기 분포는 파일럿의 `_usage` 합으로 본다.
- 가짜 서버 시험은 SDK 가 beta 요청을 만드는 모양과 응답 해석까지만 본다. 서버가 `fallbacks="default"` 를 받아들이는지, `claude-sonnet-5-5` 가 이 계정에서 열려 있는지, 폴백이 실제로 도는지는 키가 없어 확인하지 못했다. 첫 파일럿에서 볼 것: 요청이 400 없이 통과하는지, `usage.iterations` 가 오는지, `_usage.model` 이 요청한 모델과 같은지. 모델 이름이 틀리거나 열려 있지 않으면 404 라 첫 요청에서 치명 처리로 멈춘다.

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 95건(D-21 의 89건 + 폴백 파싱 3 + `usage_of` 폴백 3). fallback 블록이 앞에 온 응답을 후보로 읽음, 체인 전체 거절이 refusal, 폴백 응답의 `model`·`fallback`·시도별 합계·`returned`·`attempts`, 체인 거절의 `fallback` False, iterations 없음·시도 하나는 `fallback` False 에 키 추가 없음. 기존 `usage_of` 기대값은 `model`·`fallback` 을 포함하게 고쳤다.
- `tests/test_scan_parse.py`(65건, +8): 같은 케이스를 SDK 의 `BetaMessage` 타입으로. iterations 없음·시도 하나 3종, sticky 형태, 합계가 최상위보다 작아도 최상위 이상, SDK `beta.messages.create` 시그니처에 `betas`·`fallbacks`·`output_config` 가 있고 베타 문자열을 SDK 가 안다.
- `tests/test_scan_fake_api.py`(48건, +10): 가짜 서버가 beta 경로를 받는다. 요청 본문(`model` `claude-sonnet-5-5`, `fallbacks` `"default"`, `output_config.effort` `high`, system 블록 `cache_control.ttl` `5m`), 헤더 `anthropic-beta`, 경로. `--effort xhigh` 가 본문과 `_run` 헤더에 반영되고 `high`·`xhigh` 밖의 값 5종은 exit 2. `--model` 이 기본값을 덮어씀. 폴백 시나리오 하나(일반 파일, 폴백이 응답한 파일, 체인 전체 거절 파일): 후보 읽기, `_usage` 행의 `model`·`fallback`·`returned`·`attempts`, `sum_usage` 합계 `(380, 15)`, `fallback_usage` `(1, ["claude-sonnet-5"])`. beta 400 은 파일별 ERROR 이고 연속 8건에서 차단기. 기존 시험에서 `api-v4`·`1h`·`claude-sonnet-5`·`/v1/messages` 를 기대하던 것을 고쳤다. 인터럽트 시험의 가짜 클라이언트에는 `beta` 속성을 더했다.
- `tests/test_adapters.py`(41건, +7): 폴백 파일 수와 모델, 0 과 미측정의 구분, 모델 정렬·중복 제거, 키가 `_usage` 하나가 아닌 행 무시, `sum_usage` 가 `returned`·`attempts` 를 더하지 않음, `--effort` 가 있을 때만 argv 에 들어감, `effort()` 가 scan.py 소스에서 읽힘, `api-v5`.
- `tests/test_attacktrail_cli.py`(17건, +1): CLI 어댑터가 scan.py 기본 모델을 따르고, argv 에 effort·fallback 이 없고, manifest effort 가 `n/a`, 폴백 필드가 None. `call_shape` 기대값을 `api-v5`/`cli-v1` 로 고쳤다. D-22 에서 고치고 재실행하지 못했던 `test_cli_call_shape_is_unchanged_by_popen` 은 통과한다.
- `tests/test_cli.py`(24건, +9): `--effort` 선택지가 scan.py 와 같음, `attacktrail` 에서만 허용(CLI 어댑터·baseline 은 거부), `rescore` 가 model·effort 를 에이전트 옵션(`agent_opts`)으로 넘김(시험 `test_rescore_passes_model_and_effort_to_the_agent`. manifest 에 무엇이 기록되는지는 에이전트의 몫이고 이 시험은 전달까지만 본다).
- `tests/test_report.py`(24건, +6): 폴백 절, 미측정과 0 의 구분, effort 행, `run_summary` 세 가지.
- `tests/test_runner.py`(20건, +4): 폴백 필드가 manifest 레코드로 옮겨짐, 옛 findings.json 이 읽힘, 끝 요약이 stderr 에 정확히 이 줄들로 나오고 stdout 은 비어 있음, `--repeat` 이면 rep 마다, 요약 실패가 run 을 깨지 않음.
- mutation 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치패드 `mutate.py`·`mutate2.py`, 복구 후 scan.py 해시가 변이 전과 같은 것을 확인). scan.py 변이 11개(non-beta 호출, TTL 1h, fallbacks 제거, 옛 기본 모델, effort 미전송, 배열 형태 헤더, 최상위 토큰만 사용, `max` 제거, refusal 무시, 폴백 블록으로 판정, effort 선택지 확대)와 eval 쪽 변이 18개(폴백 전부 계산, effort 미전달, call_shape 되돌림, Meta 필드 누락, effort 기본값 하드코딩, CLI 가 effort 기록, 지문 effort 누락, 러너가 폴백 필드 안 옮김, 요약 미출력·stdout 출력·가드 제거, 요약이 건너뜀·timeout·미측정 부분 실패를 빠뜨림, 리포트 폴백 절 제거·미측정을 없음으로, CLI 가 effort 허용, rescore 가 effort 무시)가 전부 시험 실패로 잡혔다. 처음에는 변이 둘이 살아남았는데 그 시험이 없어서가 아니라 `-k` 선택식이 시험 이름을 못 골라서였고, 선택식을 고쳐 다시 돌려 잡혔다. 첫 실행은 첫 변이에서 420초 타임아웃으로 중단됐다. 같은 변이를 다시 돌리면 7초라 원인은 확인하지 못했고 재현되지 않았다.

## D-24. 스트리밍 호출, xhigh + max_tokens 64000, 호출 시간 재설계, 키 변수 별칭, D-23 리뷰 수정 (2026-10-21)

사용자 결정과 D-23 리뷰 지적 7건을 반영했다. 키가 28일에 만료되니 키를 쓰는 동안 모델과 effort 를 높여 달라는 요청이다. 처음 고른 Opus 5.5 + xhigh 는 같은 날 Claude Mythos 5.1 + xhigh 로 바뀌었고, Mythos 가 이 조직에서 404 라 D-25 에서 Claude Fable 5.1 + xhigh 가 됐다. 아래 기본 모델 이름은 D-25 기준으로 고쳤다. `max_tokens` 64000 은 사용자가 비용 위험을 알고 고른 값이다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, `select_files`, 후보 검증, Ctrl-C 동작(`os._exit(130)`), 치명 오류·차단기의 동작(분류만 넓혔다), 부분 실패 프로젝트의 분모 정책은 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). 실제 API 는 한 번도 호출하지 않았다(키 없음, 사용자 승인 전). 시험은 127.0.0.1 가짜 서버와 가짜 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021b/` 에 있다.

### 1. 결정과 반영 위치

| 결정 | 반영 |
|---|---|
| 기본 모델 `claude-fable-5-1`(날짜 접미사 없음. D-24 는 `claude-mythos-5-1`, D-25 에서 변경), 기본 effort `xhigh` | scan.py `--model`·`--effort` 기본값. 어댑터는 이 기본값을 scan.py 소스에서 읽는다. CLI 어댑터는 `claude -p --model claude-fable-5-1`(effort 는 계속 n/a) |
| `max_tokens` 64000 | scan.py `MAX_TOKENS` 상수 하나 |
| 스트리밍 호출 | `client.beta.messages.stream(...)` 컨텍스트 매니저와 `get_final_message()` (§3) |
| 호출 시간 재설계 | 데드라인에 스트림을 닫는 타이머, 부분 usage, `classify` 확장, 어댑터 시간 수치 (§5) |
| 프로젝트 timeout 기본값 | API 어댑터 10800초. CLI 어댑터와 baseline 은 1800초 |
| `call_shape` | API `api-v6`, CLI `cli-v1` |
| 키 변수 별칭 | `ATTACKTRAIL_ANTHROPIC_API_KEY` (§7) |
| 비용 상한 | $100 그대로. 도구 안에 비용 차단기는 만들지 않았다 (§6) |

### 2. 모델, effort, max_tokens (번들 문서 `shared/model-migration.md` 로 확인)

- `claude-fable-5-1`(D-24 는 `claude-mythos-5-1`)은 Claude Mythos 5.1 과 같은 모델이고 API 동작도 같다(1757행). 둘의 차이는 D-25 §1 에 있다. 가격은 $10/$50 per MTok, 5분 캐시 쓰기 $12.50, 1시간 쓰기 $20, 캐시 읽기 $0.25(1689행). Opus 5.5($4/$20)의 2.5배다.
- thinking 은 항상 켜져 있다. `thinking` 은 생략하거나 adaptive 만 허용하고 disabled·budget_tokens 는 400 이다(1683행). scan.py 는 `thinking` 을 안 보내서 그대로다. 빈 thinking 블록이 text 앞에 올 수 있는 것은 D-17 의 "text 블록만 읽기"가 이미 처리한다.
- effort 는 low~max 를 지원하고 API 기본은 `high` 다. 우리는 항상 명시한다(Opus 5.5 는 기본이 `medium` 이라 명시가 필수였다). 허용값은 `high`·`xhigh` 로 그대로다. 문서는 `high` 이상에서 큰 `max_tokens` 를 두라고 한다. 상한이 thinking 과 응답의 합이라서다(1769행). 64000 은 문서가 긴 턴의 시작값으로 드는 값이다.
- 문서 1771행: xhigh 와 max 에서 긴 산출물은 thinking 에서 초안을 쓰고 응답에 다시 써서 출력 토큰이 약 2배가 될 수 있다. 문서는 사용자 메시지 끝에 붙이는 안내문(1773~1774행)을 권한다. 그 안내문은 사용자 프롬프트 틀을 바꾸므로 이번에는 넣지 않았다. 파일럿에서 출력 토큰을 재 본 뒤 넣을지 정한다. 넣으면 `scan_py_sha256` 이 바뀐다. `prompt_sha256` 은 사용자 프롬프트 틀을 포함하지 않아 그대로다(D-14 한계).
- 안전장치는 접근 프로그램에 따라 다르다. `stop_reason: "refusal"` 과 `stop_details.category` 처리는 같다. 서버 측 폴백 `fallbacks: "default"` 와 `server-side-fallback-2026-07-01` 은 그대로 유효하고 대상은 `claude-opus-4-8`, `claude-opus-5` 다. 범주별 라우팅은 비공개고 일부 범주는 폴백이 없다(1685행). 폴백 모델은 Fable 5.1 의 thinking 블록을 못 읽어 API 가 버린다. 폴백 시도는 폴백 모델 요율로 과금된다(Opus 5 는 $5/$25, 1877행).
- 접근과 데이터 보존: Mythos 5.1 은 검증 프로그램(Cyber Verification Program 등) 승인 조직에 제공되고, Console/API 조직은 워크스페이스 단위로 배정된다(공개 문서 platform.claude.com/docs/en/models/mythos-5-1/overview, support.claude.com/en/articles/14604842). "Project Glasswing 전용"은 번들 문서(1759행)의 옛 표현이다. Fable 5.1 은 공개 모델이라 이 접근 조건이 없다(D-25 §1). 번들 문서(1686행)는 이 모델을 30일 데이터 보존이 필요한 Covered Model 이라고 적는다. 이 요건이 Fable 5.1 에도 걸리는지는 확인하지 못했다. 보존이 없는 조직·워크스페이스는 400 `invalid_request_error`("In order to access this model, your organization or workspace must have data retention enabled.")를 받는다(1686행). 문서는 초안이 404 로 적었다고 밝히고, 404 가 오면 보존 설정부터 보라고 한다. 우리 분류에서 404 는 치명이라 첫 요청에서 멈춘다. 400 은 연속 8건에서 차단기가 멈춘다(요청 8건, 비용 없음). 어느 쪽이든 파일럿의 첫 확인 항목이었다. D-25 정정: 이 조직에서 `claude-mythos-5-1`·`claude-mythos-5` 는 count_tokens 404 `not_found_error` 였다(D-25 §1).
- 그 밖: history-editing 검사는 안 한다(단일 턴이라 무관). Priority Tier 미지원. Claude Platform on AWS 에서는 제공 안 함(Claude API 는 됨). 속도 한도는 Mythos 계열 별도 풀이다(Fable 5.x 는 Fable 5.x 풀. D-25). 토크나이저는 Opus 4.7/4.8 계열이라 Sonnet 계열 기준 토큰 수의 약 1~1.35배다.
- 이전 기본인 Sonnet 5.5 는 `--model claude-sonnet-5-5 --effort high` 로, Opus 5.5 는 `--model claude-opus-5-5` 로 계속 돈다. 가짜 서버 시험이 두 조합의 요청 본문(모델, effort, `stream`, `max_tokens`, 폴백, 베타 헤더, TTL, thinking 없음)을 고정한다.

### 3. 스트리밍 호출 (SDK 1.15.0 소스와 이 머신의 측정)

- 호출은 `with c.beta.messages.stream(...) as opened:` 와 `opened.get_final_message()` 다. `stream()` 의 시그니처에 `betas`, `fallbacks`, `output_config`, `system`, `max_tokens`, `timeout` 이 있고 본문에 `stream: true` 를 넣는다(`resources/beta/messages/messages.py` 1686~1823행). 경로는 같은 `POST /v1/messages?beta=true`. 시험이 인자 이름을 SDK 시그니처로 고정한다. 보내는 betas·fallbacks·output_config·system(캐시 `5m`)은 D-23 그대로다.
- 이유: 문서(`python/claude-api/streaming.md`)는 큰 `max_tokens` 의 비스트리밍 요청을 SDK 가 거부하는 이유를 유휴 연결 끊김이라고 적고 스트리밍을 권한다. SDK 1.15.0 은 `with_options(timeout=...)` 로 timeout 을 주면 그 가드를 건너뛰어서(D-17) SDK 가 막아 주지 않는다. 64000 토큰은 비스트리밍 한도(약 21,333)의 3배다.
- 측정(스크래치패드 `exp24/e1.py`, 가짜 SSE 서버, Windows 11, Python 3.12.10, SDK 1.15.0):
  1. SDK 는 `ping` 이벤트를 호출자에게 내주지 않는다(`_streaming.py` 157행의 `continue`). ping 만 오는 6초 동안 이벤트가 0개였고 읽기 timeout 3초는 걸리지 않았다(ping 바이트가 시간을 리셋한다). 그래서 "이벤트마다 시각을 검사"하는 방식은 서버가 ping 만 보내는 동안 데드라인을 못 막는다. D-25 정정: 모델이 생각하는 동안 ping 이나 바이트가 실제로 오는지는 공식 문서에 없고 미확인이다. 앞 판은 "`display` 기본이 omitted 라 바이트는 오는데 이벤트가 없다"고 적었는데 근거가 없었다. 파일럿에서 `_usage.call` 의 `first_event_s`·`max_gap_s` 로 잰다.
  2. 스트림을 읽는 도중의 오류는 SDK 가 감싸지 않는다. 바이트가 2초 안 오면 `httpx2.ReadTimeout`(TimeoutException 계열)이 그대로 나왔다. 감싸는 곳은 연결 단계뿐이다(`_base_client.py` 1441~1450행). 옛 `classify` 는 이것을 중립으로 봤다.
  3. 스트림 안의 SSE `error` 이벤트는 `status_code` 200 인 `APIStatusError` 다(`_streaming.py` 160~173행이 200 응답의 상태 코드로 예외를 만든다). 상태 코드로는 과부하(529)와 구별되지 않아 `body["error"]["type"]` 으로 본다.
  4. 다른 스레드에서 `stream.close()` 를 부르면 읽기에서 막힌 워커가 깨어난다(요청 2.0초 뒤에 close, 2.03초에 `httpx2.ReadError` `WinError 10038`). ping 만 오는 경우와 완전히 조용한 경우 둘 다 그랬다. 다른 OS 는 확인하지 못했다.
  5. SDK 는 `fallback` 블록을 일반 `content_block_start` 로 받아 content 에 추가하고 스냅샷의 `model` 을 `to.model` 로 바꾼다(`lib/streaming/_beta_messages.py` 517~520행). 시험이 이 경로를 진짜 SDK 로 통과시킨다.
  6. `current_message_snapshot` 은 `message_start` 전에는 `AssertionError` 이고 그 뒤에는 그 시점의 usage 를 돌려준다.
- 부분 usage(`partial_usage`): 스트림이 끊기면(데드라인에서 닫음, 연결 끊김, 무응답, error 이벤트) 스냅샷의 usage 를 `_usage` 행에 `"partial": true` 로 남긴다. 입력·캐시 토큰은 `message_start` 가 담은 시도의 값이고(D-25 정정: 앞 판은 "온전하다"고 적었다. 끊기기 전에 폴백이 있었으면 다른 시도의 입력은 빠질 수 있다) `output_tokens` 는 그 시점 값이라 하한이다. 최종 출력 토큰은 스트림 끝의 `message_delta` 에만 실려서 끊기면 알 수 없다. 값을 추정해서 채우지는 않았다. `shared/evals/build-eval.md` 도 "billed-but-failed spend is still spend" 라며 실패한 시도의 usage 를 지출에 세라고 한다(그 문서가 말하는 것은 호출이 끝난 경우다). 어댑터는 partial 행 수를 `error` 메모에 적는다("`tokens_out` 은 하한"). 호출 시간 로그 `call` 은 partial 행에도 붙는다.
- 동작 변경: 비스트리밍은 SDK 가 연결 오류를 재시도했다. 스트림 도중 끊기면 SDK 는 재시도하지 않는다(재시도는 연결 단계의 429·5xx·연결 오류뿐). 그 파일은 `ERROR stream interrupted: ...` 이고 scan.py 는 다시 요청하지 않는다. 15분 넘게 생성한 스트림이 끊기면 부분 usage 만 남고 결과는 없다. 재시도를 넣을지는 파일럿에서 끊김 빈도를 본 뒤 정한다.

### 4. 출력 도중 거절의 text 이어 붙이기

문서(`model-migration.md` 1426행, "Echoing fallback turns back")의 스트리밍 설명은 이렇다. 출력 전 거절이면 `message_start` 가 폴백 모델 이름을 담고 `fallback` 블록이 content 맨 앞에 온다. 출력 도중 거절이면 "a mid-stream block keeps the partial, marks the boundary with the block, and continues - only the partial's `text` blocks are passed to the fallback model as continuation context". 비스트리밍은 "Non-streaming mid-output declines omit the declined partial entirely" 다.

결정: content 의 text 블록을 경계 앞뒤 구분 없이 순서대로 이어 붙인다. `parse_candidates` 의 기존 join 이 이미 그렇게 한다. 경계 뒤만 읽지 않는다.

근거:
1. "continuation context" 와 "continues". 폴백 모델은 부분 출력의 text 를 이어쓰기 문맥으로 받고, 같은 스트림에서 이어 쓴다. 이어 썼다면 경계 뒤 text 는 JSON 의 뒷부분이다.
2. 비스트리밍과의 대비. 비스트리밍은 거절된 부분 출력을 content 에서 통째로 뺀다. 그러면 응답에는 폴백 모델이 쓴 것만 남으니 처음부터 쓴 전체여야 일관된다. 스트리밍은 "already-received content is never invalidated"(같은 원문)라 부분 출력이 content 에 남고 폴백이 그 뒤를 잇는다.
3. 되돌려 보낼 때의 규칙. 경계 앞의 text 블록은 그대로 에코하고 경계 뒤 내용도 정상으로 에코한다. 부분 출력과 이어쓰기가 한 어시스턴트 턴이라는 뜻이다.

확실하지 않은 것: "이어쓰기"라고 한 문장으로 적은 곳은 없다. 위 세 근거에서 추론했다. 틀려도 조용히 틀린 후보가 나오지는 않는다. 폴백 모델이 처음부터 다시 썼다면 이어 붙인 문자열이 JSON 이 아니라 `ERROR JSON 파싱 실패` 로 남는다. 반대로 경계 뒤만 읽기로 했다면, 이어쓰기일 때 JSON 조각이라 항상 실패한다. 이어 붙이기는 어느 해석이 맞아도 틀린 후보를 내지 않는다.

시험: selftest 2건(이어쓰기는 후보로, 다시 쓴 응답은 `JSON 파싱 실패`), SDK 타입 `BetaMessage` 로 같은 두 경우와 "경계 뒤만 읽으면 실패", 가짜 SSE 서버의 두 시나리오. 출력 전 거절은 `message_start` 가 폴백 모델 이름을 담고 `fallback` 블록이 맨 앞에 온다. 출력 도중 거절은 thinking, 부분 text, `fallback` 블록, 이어쓰기 text 와 `usage.iterations` 시도 둘이다. 후자는 후보를 읽고, `_usage` 행이 `fallback` true, 모델 `claude-opus-5`, 토큰이 시도별 합계(입력 110, 출력 45: 거절된 부분 출력 40토큰도 정상 요율로 과금)인 것까지 고정한다. 다시 쓴 응답의 시나리오는 ERROR 와 `fallback` true 를 고정한다.

파일럿 확인 항목: 출력 도중 거절이 나는 파일이 있으면 그 파일이 후보를 냈는지, `ERROR JSON 파싱 실패`(와 `_usage.fallback` true)인지 본다. 파싱 실패가 나오면 폴백 모델이 처음부터 다시 쓴 것이므로 경계 뒤 text 만 읽도록 바꾼다. 거절이 어느 범주에서 얼마나 나는지, 출력 도중 거절이 실제로 나는지도 미측정이다.

### 5. 호출 시간 재설계

5.1 스트리밍에서 기존 장치가 성립하는가.

| 기존 장치 | 스트리밍에서 | 조치 |
|---|---|---|
| `call_limits` 의 호출별 timeout | httpx read timeout 은 읽기 사이의 간격에 걸린다. 바이트(ping 포함)가 오는 한 총시간을 못 묶는다. 연결 단계(응답 헤더까지)의 대기와 재시도는 계속 묶는다 | 계산식은 그대로, 의미를 "연결 단계와 무응답의 상한"으로 바꿨다. 값 600초는 유지(SDK 기본, ping 간격 미측정). D-25 정정: 1800초로 올렸다(D-25 §2 항목 1) |
| 하드 데드라인에 호출이 끝남 | 안 끝난다. 스트림은 바이트가 오는 동안 안 끊긴다 | 스트림이 열리면 `threading.Timer` 가 데드라인에 `stream.close()` 를 부른다(측정 4). 끊긴 호출은 `ERROR timeout: hard deadline reached (call limited to Ns; ...)` 이고 차단기에 세지 않는다 |
| main() 의 감시(데드라인 + 2초에 버리고 `os._exit`) | 그대로 유효 | 닫을 스트림이 없는 호출(SDK 의 Retry-After 대기)과 close 가 깨우지 못하는 경우의 백스톱이다. 이 경로는 이제 Retry-After 를 단 429 로 시험한다. 옛 시험의 "응답을 흘리는 서버"는 스트림이라 타이머가 닫는다 |
| 연속 오류 차단기 | 스트림 도중 오류가 중립으로 새서 영원히 안 셌다 | `classify` 를 넓혔다. httpx2 `TimeoutException` 은 timeout, `TransportError` 는 connection, 200 응답의 error 이벤트는 body 의 `error.type`(`overloaded_error`·`api_error` 는 5xx, `rate_limit_error` 는 429) |
| usage 기록 | 응답 객체가 없으면 usage 가 없었다 | 부분 usage (§3) |
| Ctrl-C (`os._exit(130)`, 사용자 결정으로 불변) | 그대로 성립한다. 워커가 스트림 읽기에서 막혀 있어도 프로세스를 바로 끝낸다 | 자식 프로세스 시험에 "스트림이 열린 채 ping 만 오는 상태"를 추가 |
| SDK 재시도 | 연결 단계만 | 동작 변경으로 §3 에 적었다 |

5.2 어댑터 수치. 전부 추정이고 실측이 없다. 문서는 Mythos 5.1(Fable 5.1 과 같은 모델)의 어려운 작업이 요청 하나에 수 분에서 수십 분 걸릴 수 있다고만 적고 속도는 주지 않는다. 가정 둘이다. 최악의 호출을 30분(`MAX_CALL_S` 1800초)으로 본다. `max_tokens` 64000 을 다 쓰면 유효 속도는 약 36 tokens/s 다(thinking 도 출력 토큰이다). 같은 속도에서 평균 출력 1만 토큰이면 호출당 약 280초다.

| 항목 | 이전 | 지금 | 근거 |
|---|---|---|---|
| `MAX_CALL_S` | 없음 | 1800 | 위 가정. `max_tokens` 나 모델을 바꾸면 같이 본다(손으로 맞춘다) |
| 소프트 여유(`--time-budget` = timeout − 여유) | max(60초, 10%) | max(1815초, 10%), 하한 timeout/2 | 하드 여유 15초 + `MAX_CALL_S`. 소프트 한도 직전에 시작한 최악 호출도 하드 데드라인 안에 끝난다. timeout 이 3,630초 아래면 하한이 먼저 걸린다 |
| `--deadline-s` | timeout − 15초 | 그대로 | 15초는 D-20 측정(종료 지연 최대 2.7초의 약 5.5배). 스트림을 닫는 경로의 깨어남은 측정 4 에서 0.03초 |
| API 어댑터 기본 프로젝트 timeout | 1800초 | 10800초 | 아래 계산 |
| `CALL_TIMEOUT_S` 600, `MIN_RETRY_ATTEMPT_S` 120, 연속 오류 8건, `HARD_GRACE_S` 2초 | | 그대로 (D-25: `CALL_TIMEOUT_S` 만 1800 으로 올렸다) | 실측 없이 정한 값이라 바꿀 근거도 없다 |
| CLI 어댑터와 baseline 의 기본 timeout | 1800초 | 1800초 | CLI 는 호출별 한도가 없고 호출이 프로젝트의 남은 시간을 통째로 받는다 |

기본 timeout 10800 의 계산. secondswap 36 파일, workers 4 라서 9 라운드다.
- 평균 출력 1만 토큰: 9 × 280초 = 2,520초(42분).
- 평균 출력 2만 토큰: 9 × 560초 = 5,040초(84분). 10800 은 이것의 약 2.1배다.
- 64000 토큰을 전부 쓰는 최악: 9 × 1,800초 = 16,200초(4.5시간). 10800 안에 못 끝나고, 소프트 한도(8,985초)부터 남은 파일이 `skipped: time budget reached` 로 남아 커버리지에 보인다. 조용히 사라지지 않는다.
- timeout 10800 이면 `--time-budget` 8985, `--deadline-s` 10785 다.
- 시간 한도는 비용 상한이 아니다. 같은 가정에서 4 workers × 10785초 × 36 tokens/s × $50/MTok ≈ $77(출력만)이고, 속도가 2배면 2배다. `--jobs` 기본 2 는 프로젝트 둘을 동시에 돌려 동시 호출이 8개가 된다. Fable 5.x 풀의 속도 한도에 닿으면 429 가 나고, 429 는 SDK 재시도와 차단기가 받는다. 파일럿은 프로젝트 1개다.

파일럿 실측 뒤 사용자에게 다시 물을 항목은 전부 이 절의 추정값이다: 프로젝트 기본 timeout 10800, `MAX_CALL_S` 1800, 소프트 여유, `CALL_TIMEOUT_S`(무응답 상한. D-25 에서 600 → 1800), `--jobs` 기본 2. 실측 소스는 `_usage.call.elapsed_s` 와 같은 행의 `output_tokens` 다. `output_tokens / elapsed_s` 가 생각 시간을 포함한 유효 속도다. 응답이 없는 호출은 행이 없어서 진행 줄의 사유로만 본다. (D-25 정정: "600초 무응답으로 끊긴 호출이 나오면 ping 이 생각하는 동안 안 온다는 뜻"이라는 앞 판의 추론은 접는다. 값을 1800 으로 올렸고, `ERROR timeout: stream stalled, no data for 1800s` 가 나오면 30분 무응답이라 장애로 본다. 생각 중 이벤트는 `first_event_s`·`max_gap_s` 로 잰다.)

### 6. 비용 (Fable 5.1. Mythos 5.1 과 같은 가격)

단가는 입력 $10, 출력 $50 per MTok, 캐시 5분 쓰기 $12.50, 캐시 읽기 $0.25 다. 폴백 시도는 폴백 모델 요율이다. 식은 D-23 §3 의 구조 그대로이고 값만 바뀐다.

```
비용 상한 = P_in × (input_tokens
                  + 1.25  × cache_creation.ephemeral_5m_input_tokens
                  + 2.0   × cache_creation.ephemeral_1h_input_tokens
                  + 0.025 × cache_read_input_tokens)
          + P_out × output_tokens            (P_in = $10/MTok, P_out = $50/MTok)
```

secondswap 36 파일의 출력 비용(출력 토큰 × $50/MTok)이다. 입력은 따로 더한다. 파일당 입력을 약 8천 토큰(가정, 미측정)에 토크나이저 1.2배로 보면 36 × 9,600 × $10/MTok ≈ $3.5 이고, 캐시 읽기는 무시할 만하다.

| 파일당 평균 출력 | 36 파일 출력 비용 | 입력을 더한 대략 |
|---|---|---|
| 1만 토큰 | $18 | 약 $21~22 |
| 2만 토큰 | $36 | 약 $39~40 |
| 3.2만 토큰 | $57.6 | `max_tokens` 를 32000 으로 낮춰 전부 쓴 최악 |
| 4.8만 토큰 | $86.4 | `max_tokens` 를 48000 으로 낮춰 전부 쓴 최악 |
| 6.4만 토큰 | $115.2 | 64000 을 전부 쓴 최악, 1회 |

최악 1회가 약 $115 라서 $100 상한을 한 번에 넘을 수 있다. Console 워크스페이스 지출 한도 설정을 권고했고, 사용자가 이를 알고 `max_tokens` 64000 을 골랐다(2026-10-21). 도구 안에 비용 차단기는 만들지 않았다. 시간 한도(§5)도 비용을 막지 못한다. 끊긴 스트림의 `output_tokens` 는 하한이라 `tokens_out` 으로 센 지출은 실제보다 작을 수 있다. 진짜 상한은 Console 의 지출 한도다. xhigh 에서는 긴 산출물의 출력이 약 2배가 될 수 있다(문서 1771행). 우리 출력은 후보 JSON 이라 짧지만 thinking 도 출력 토큰이라 "평균 1만 토큰"은 가정이다.

### 7. 키 환경변수 별칭

사용자가 Claude Code 과금 혼선을 피하려고 키를 사용자 레벨 환경변수 `ATTACKTRAIL_ANTHROPIC_API_KEY` 로 저장한다. Claude Code 는 `ANTHROPIC_API_KEY` 만 읽고 그 변수에 키가 있으면 그 키로 과금하기 때문이다. `runner/run.py` `agent_env()` 가 attacktrail 에이전트의 자식 env 에 넣는 값을 `os.environ["ATTACKTRAIL_ANTHROPIC_API_KEY"]` 에서 먼저 가져오고, 없거나 비어 있으면 기존처럼 `ANTHROPIC_API_KEY` 에서 가져온다. 자식이 보는 이름은 `ANTHROPIC_API_KEY` 그대로다. scan.py 와 SDK 는 바꾸지 않았다. 별칭 이름은 자식에게 넘기지 않는다.

- 키 유무 검사: `adapters/attacktrail.py` 의 검사와 `scoped_env` 는 `ctx.env` 를 먼저 보므로 코드를 안 바꿔도 동작한다. 시험이 별칭만 있는 환경에서 가짜 실행기가 받는 env 에 `ANTHROPIC_API_KEY` 가 들어가고 별칭 이름은 없는 것을 본다.
- 키 값이 manifest·로그·스냅샷에 안 들어가는 것은 전과 같다. manifest 의 정확한 값 가리기 목록(`_SECRET_ENV`)에 별칭 이름을 더했다. 키 형식이 `sk-ant-...` 가 아니어도 가려진다. CLI 어댑터(`cli_env()`)는 키를 안 넘기는 허용 목록이라 별칭도 안 넘어간다(시험이 두 변수를 모두 심고 확인한다).
- 한계: 별칭은 `eval run`(러너) 경유일 때만 읽는다. `AttackTrailAgent` 를 직접 만들어 부르면 `os.environ` 의 `ANTHROPIC_API_KEY` 만 본다. 키가 둘 다 없을 때의 어댑터 오류 문구에 별칭 이름을 덧붙였다.
- README 의 키 안내를 이 이름으로 바꾸고 "setx 로 `ANTHROPIC_API_KEY` 를 저장하지 말 것(Claude Code 가 그 키로 과금)" 한 줄을 넣었다.
- 시험: `test_attacktrail_key_alias_wins_and_the_child_sees_the_standard_name`(별칭 우선, 별칭만 있어도 동작, 빈 별칭은 폴백, 자식 env 의 이름, 별칭 값 가리기).

### 8. D-23 리뷰 지적 수정 (7건)

1. 폴백 집계 문구. `fallback_usage`·`files_fallback` 은 "폴백 모델이 응답을 낸 파일 수"다. 그 응답이 파싱에 실패하거나 `max_tokens` 로 잘려 ERROR 가 된 파일도 센다. 옛 문구("그 파일의 후보는 이 모델이 냈다", "the response that was read")는 ERROR 파일에서 맞지 않았다. 독스트링(`adapters/attacktrail.py` `fallback_usage`)과 리포트의 "Fallback responses" 절을 이 정의로 고쳤다(D-23 §4 에도 정정을 붙였다). 시험: 잘린 폴백 행이 `files_fallback` 에 들고 커버리지는 failed 인 것(`test_fallback_files_count_responses_that_were_not_usable_too`), 리포트 시험이 정의 문구를 고정한다.
2. D-23 §3 의 "비용 상한($100)" 출처를 사용자 결정(2026-10-21, "파일럿 범위, 비용상한: 100불")이라고 적었다.
3. `AttackTrailAgent._consts()` 가 `OSError` 만 잡아서 scan.py 의 `--effort`·`--model` 기본값이 리터럴이 아니면(`ast.literal_eval` 의 `ValueError`) `fingerprints()` 가 죽었다(manifest 를 쓰는 중에 호출된다). `(OSError, SyntaxError, ValueError)` 로 넓혀 `"unknown"` 이 된다. 시험 4종: 변수 기본값, 호출 기본값, 문법 오류, UTF-8 이 아닌 바이트. 명시한 `model=`·`effort=` 는 그대로 이긴다.
4. D-23 §7 에 "`--repeat` 는 rep 별 `files_fallback` 을 보고, 0 이 아니면 다른 모델의 결과로 본다(sticky 라우팅 가능성, 실측 전)" 한 줄을 넣었다.
5. `test_rescore_records_model_and_effort` 를 `test_rescore_passes_model_and_effort_to_the_agent` 로 바꿨다. 이 시험은 `agent_opts` 전달만 본다. D-23 테스트 절의 문구도 맞췄다.
6. `AttackTrailCLIAgent.__init__` 이 `effort` 가 None 이 아니면 `ValueError` 를 낸다(cli.py 경로와 같은 규칙). `get_agent("attacktrail-cli", effort=...)` 도 같다. 이 규칙과 충돌한 기존 단언(`AttackTrailCLIAgent(effort="xhigh").fingerprints()["effort"] == "n/a"`)은 고쳤다. 시험 `test_cli_adapter_refuses_an_effort_argument`.
7. scan.py `usage_of` 의 `cache_creation` 합산에 "5m/1h 분해는 분해를 가진 시도만 합산하므로 `cache_creation_input_tokens` 와 어긋날 수 있다" 주석을 달았다.

### 9. 바꾸지 않은 것

- ROLE, 기준 문서, `SCHEMA`, 사용자 프롬프트 틀, `select_files`, 후보 검증, `thinking` 미전송, effort 선택지(`high`·`xhigh`), 캐시 TTL `5m`, 폴백 형태(`"default"`), `fallback_credit_token`(서버 측 폴백을 쓰면 직접 쓸 일이 없다).
- Ctrl-C 동작, 치명 오류(401·402·403·404)의 즉시 중단, 연속 오류 차단기 8건(분류만 넓혔다), 부분 실패 프로젝트의 분모 정책, 러너의 Ctrl-C 한계.
- 사용자 메시지 끝의 문서 권장 안내문(§2), 스트림 도중 끊김의 재시도(§3), 비용 차단기(§6).

### 10. 알려진 한계와 파일럿 확인 항목

- 출력 도중 거절의 text 이어 붙이기가 맞는지(§4).
- (D-25 정정) 모델 접근: Mythos 5.1 은 이 조직에서 404 였고 Fable 5.1 은 count_tokens 가 통과했다. 남은 것은 Fable 5.1 의 30일 데이터 보존 요건 여부와 400/404 의 실제 모습(§2). `/v1/models` 의 `allowed_fallback_models` 는 2026-10-21 에 `claude-opus-4-8`, `claude-opus-5` 로 확인했다. `claude -p --model claude-fable-5-1` 을 CLI 가 받는지.
- 서버가 `fallbacks="default"` 를 받는지, 폴백이 실제로 도는지, 범주별 거절·폴백 빈도, sticky 라우팅이 파일 사이에 걸리는지(D-23 §7 그대로).
- 호출 시간 분포와 §5 의 모든 수치. ping 이 생각하는 동안 오는지(D-25: `CALL_TIMEOUT_S` 를 1800 으로 올렸고 `first_event_s`·`max_gap_s` 로 잰다).
- 스트림 도중 끊김 빈도와 재시도 필요성. 끊긴 스트림의 출력 토큰은 하한이다.
- 다른 스레드의 `close()` 가 막힌 읽기를 깨우는 것은 Windows 11 에서만 확인했다. 못 깨우는 OS 에서는 감시가 데드라인 + 2초에 호출을 버리고 usage 를 못 읽는다.
- 키 별칭은 러너 경유일 때만 읽는다(§7). `max_tokens` 와 `MAX_CALL_S`·기본 timeout 은 손으로 맞춘다.
- 가짜 서버 시험의 한계: 실제 서버의 SSE 모양(ping 간격, `message_start` 의 usage 내용, 출력 도중 거절의 이벤트 순서)은 문서와 SDK 소스로 만든 것이다. 실제와 다를 수 있다.

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 107건(D-23 의 95건에서 +12). 이어쓰기·다시 쓴 응답의 text 처리 2, `partial_usage` 3, `classify` 7(httpx2 예외 2, SSE error 이벤트 5)이다. 폴백 시험의 모델 이름을 Mythos 5.1 / Opus 5 로 바꿨다(D-25 에서 Fable 5.1 / Opus 5 로).
- `tests/test_scan_fake_api.py`(61건, +13): 가짜 서버가 SSE(HTTP/1.1 chunked)를 낸다. 기존 메시지 dict 를 진짜 API 모양의 이벤트(`message_start`, 블록별 start/delta/stop, 누적 usage 의 `message_delta`, `message_stop`)로 풀고, 대본(`Script`)으로 ping, 정지, 끝 청크 없는 끊김, error 이벤트를 만든다. 기존 시나리오(정상, refusal, max_tokens, 4xx·5xx 분류, 타임아웃·데드라인, 차단기, Ctrl-C)는 전부 이 경로로 돈다. 본문은 `stream` true, `max_tokens` 64000, 모델 `claude-mythos-5-1`(D-25 에서 `claude-fable-5-1`), effort `xhigh`, `fallbacks` `"default"`, 헤더 `server-side-fallback-2026-07-01`, TTL `5m` 를 단언한다. Sonnet 5.5 + high, Opus 5.5 + xhigh 도 같은 모양으로 도는 것을 본다. 새 시험: 폴백 두 시나리오와 다시 쓴 응답, 연결 끊김이 그 파일만 ERROR 이고 부분 usage 를 남김, 끝 청크는 정상인데 `message_stop` 이 없는 스트림, 끊김·error 이벤트 8건 연속이 차단기를 세움(종류 2), 무응답 스트림이 timeout 이고 차단기에 셈(복사본의 `CALL_TIMEOUT_S` 를 2초로 줄임), 생각만 하는 동안(ping 만 옴) 데드라인이 스트림을 닫고 부분 usage 를 남기며 감시 경로를 안 탐. Ctrl-C 자식 프로세스 시험은 호출이 응답 헤더 전에 있는 경우와 스트림이 열린 채 ping 만 오는 경우 둘(flag·signal 각각)이다. 옛 "응답을 흘리는 서버" 시험은 타이머가 닫는 시험으로 바꾸고, 감시 경로 시험 둘은 Retry-After 를 단 429 로 옮겼다(복사본의 `MIN_RETRY_ATTEMPT_S`·`RETRY_DELAY_S` 를 줄여 짧은 데드라인에서도 재시도 2회가 남게 함).
- `tests/test_scan_parse.py`(69건, +4): 진짜 SDK 의 `stream()` 시그니처와 `MAX_TOKENS`, `BetaMessage` 의 이어쓰기·다시 쓴 응답, 진짜 `BetaMessageStream` 으로 `partial_usage`, 진짜 httpx2 예외와 200 응답 `APIStatusError` 의 `classify`.
- `tests/test_adapters.py`(51건, +10): 시간 값 8종(10800→8985·10785, 7200→5385·7185, 36000→32400·35985, 1800→900·1785 등)과 소프트 여유가 `MAX_CALL_S` 이상인 것, `api-v6`·기본 모델·기본 effort·`prompt_sha256` 앞 16자, 에이전트별 기본 timeout, 읽을 수 없는 scan.py 기본값 4종, 잘린 폴백 행의 집계, partial 행 메모.
- `tests/test_attacktrail_cli.py`(18건, +1): CLI 어댑터의 effort 거부, 모델 기본값 Mythos 5.1(D-25 에서 Fable 5.1), 두 키 변수가 `claude` 환경에 안 감.
- `tests/test_cli.py`(25건, +1): `--timeout` 기본이 None(러너가 에이전트 기본으로 해석), 0 은 거부, 시험 이름 변경.
- `tests/test_runner.py`(25건, +5): 에이전트별 기본 timeout 과 manifest `agent.timeout_s`(4종), 키 별칭.
- `tests/test_report.py`(24건): 폴백 절의 정의 문구를 고정.
- mutation 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치패드 `mutate24.py`, 복구는 sha256 으로 확인). 25개 변이가 모두 시험 실패로 잡혔다.

| 변이 | 잡은 시험 |
|---|---|
| `MAX_TOKENS` 16000 / 기본 effort high / 기본 모델 Sonnet 5.5 / TTL 1h / 폴백 인자 제거 | 요청 본문 시험, 실행 헤더 시험 (각 1) |
| 데드라인 타이머 제거 / 타이머가 `cut` 표시를 안 함 / 타이머가 60초 늦게 / 데드라인 컷을 특별 취급 안 함 | 서버가 ping 만 보내는 동안의 시험(D-25 에서 이름을 바꿨다), 열린 스트림 시험 (각 1) |
| `partial_usage` 가 항상 None | 끊긴 스트림 시험 (1) |
| `classify` 가 `TransportError` 를 안 봄 / `TimeoutException` 을 안 봄 / 200 의 error 이벤트를 안 봄 | 차단기 시험 (각 1) |
| 경계 뒤 text 만 읽음 | 출력 도중 폴백 시험 (1) |
| Ctrl-C 처리에서 `os._exit(130)` 제거 | 자식 프로세스 시험(스트림이 열린 쪽, 32초) (1) |
| `_consts` 가 `OSError` 만 잡음 / 소프트 여유를 옛 60초로 / `partial_rows` 가 0 / API 기본 timeout 1800 / `call_shape` api-v5 / CLI 어댑터가 effort 를 받음 | 어댑터·CLI 시험 (각 1) |
| 러너의 키 별칭 제거 / 기본 timeout 을 에이전트에서 안 가져옴 / manifest 가 별칭 값을 안 가림 / 리포트 옛 문구 | 러너·리포트 시험 (각 1) |

## D-25. 기본 모델을 claude-fable-5-1 로 바꾸고, D-24 리뷰 지적 9건을 고친다 (2026-10-21)

사용자 결정(기본 모델 `claude-fable-5-1`)과 D-24 리뷰 지적 9건을 반영했다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, `select_files`, 후보 검증, effort 선택지, `max_tokens` 64000, 캐시 TTL `5m`, 폴백 형태(`"default"`), 차단기 8건, Ctrl-C 동작(`os._exit(130)`)은 그대로다. `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 같다(`fingerprints()` 로 확인). `call_shape` 는 API `api-v6`, CLI `cli-v1` 그대로다. D-25 이전에 API 로 돈 run 이 없고, 요청 본문이 같고, 요청 헤더에서 바뀐 것은 `x-stainless-read-timeout` 값뿐이라서 올리지 않았다. D-25 작업 중 실제 키로 한 호출은 조회뿐이다: `/v1/models`(allowed_fallback_models 확인)와 `count_tokens`(mythos-5-1·mythos-5 404, fable-5-1 통과), 2026-10-21, 생성 호출 없음. 선행 5파일 실행은 사용자 승인 뒤 17:17 에 1회. 결과는 이 절 끝에 있다. 시험은 127.0.0.1 가짜 서버와 가짜 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021c/` 에 있다.

### 1. 기본 모델: claude-fable-5-1

실측(코디네이터, 2026-10-21):
- 회사 Console 조직(두 키 모두 같은 조직)에서 `claude-mythos-5-1` 과 `claude-mythos-5` 는 count_tokens 404 `not_found_error: model: claude-mythos-5-1` 이다.
- `claude-fable-5-1` 은 count_tokens 가 통과한다. `/v1/models` 에서 이 모델의 `allowed_fallback_models` 는 `claude-opus-4-8`, `claude-opus-5`, max_tokens 는 128000 이다.
- 사용자 결정: 기본 모델을 `claude-fable-5-1` 로 바꾸고, 먼저 5개 파일로 선행 실행(거절률, 응답 시간)한 뒤 본 실행을 한다. effort `xhigh`, `max_tokens` 64000 은 유지한다.

Fable 5.1 과 Mythos 5.1 (번들 `shared/model-migration.md` 1325행부터, 1378행, 1685~1689행, 1757행. 접근 조건은 공개 문서 platform.claude.com/docs/en/models/mythos-5-1/overview, support.claude.com/en/articles/14604842):

| 항목 | Fable 5.1 | Mythos 5.1 |
|---|---|---|
| 모델, 가격 | 같은 모델. $10/$50 per MTok, 5분 캐시 쓰기 $12.50, 읽기 $0.25 | 같음 |
| 서버 측 폴백 대상 | `claude-opus-4-8`, `claude-opus-5` | 같음 |
| 접근 | 공개 모델 | 검증 프로그램(Cyber Verification Program 등) 승인 조직. Console/API 조직은 워크스페이스 단위로 배정 |
| 속도 한도 | Fable 5.x 풀 | Mythos 풀 |
| 플랫폼 | 해당 제한 언급 없음 | Claude Platform on AWS 미제공 |
| 안전 분류기 | 생물학 연구와 대부분의 사이버보안 내용을 겨냥한다. "security tooling" 같은 정당한 작업도 오탐될 수 있다(1378행) | 번들 문서가 Fable 5.1 쪽만 적는다 |
| history-editing 검사 | 한다(단일 턴이라 이 스캔과 무관) | 안 한다 |

- "Project Glasswing 전용"은 번들 스킬 문서의 옛 표현이라 쓰지 않는다(항목 2).
- 가격과 폴백 대상 숫자는 D-24 §2, §6 그대로다. 비용 상한식과 36 파일 표도 같다.
- 이 스캔에 걸리는 차이는 안전 분류기다. AttackTrail 의 입력은 보안 코드라 정당한 작업이 거절될 수 있고, 거절은 서버 측 폴백이 `claude-opus-4-8` 또는 `claude-opus-5` 로 다시 보낸다. 거절률은 미측정이다.
- 반영 위치: scan.py `--model` 기본값과 독스트링·주석, 어댑터의 주석과 독스트링(`attacktrail.py`, `attacktrail_cli.py`), 시험의 단언·주석·픽스처 모델 이름, README, 이 문서의 D-24 절과 "아직 안 된 것". D-24 절의 모델 이름은 D-25 기준으로 고치고 D-24 시점의 선택은 한 문장으로 남겼다. CLI 어댑터는 scan.py 기본값을 읽어서 `claude -p --model claude-fable-5-1` 이 된다(CLI 가 이 이름을 받는지는 실제 호출로 확인하지 못했다).
- `scan_py_sha256` 이 바뀐다. `compare` 는 에이전트 `model`·`call_shape` 가 달라도 경고하지 않는다(D-24 "아직 안 된 것" 표). Mythos 기본 run 은 없다(404 라 만들어지지 않았다). 그래서 모델 차이 때문에 어긋나는 run 쌍은 지금 없다. 이후 run 끼리는 manifest `agent.model` 을 사람이 대조한다.
- 선행 실행 계획: 코디네이터가 secondswap 의 5개 파일(`SecondSwap_Marketplace.sol`, `SecondSwap_StepVesting.sol`, `SecondSwap_VestingManager.sol`, `SecondSwap_Whitelist.sol`, `hardhat.config.ts`)을 임시 디렉터리에 복사해 scan.py 를 직접 돌린다. `eval run` 이 아니고 코드 변경은 없다. 명령 형태는 `scan.py <임시폴더> --out <scratchpad>\probe5\pilot-5.jsonl --time-budget 3000 --deadline-s 3600` 이고, 모델은 기본값 `claude-fable-5-1`, effort `xhigh`, workers 4 다. `--deadline-s` 가 총시간을 묶는다(안 줘도 호출마다 무응답 상한 1800초는 적용된다, 항목 1). 위험 하나: 임시 폴더는 git 저장소 밖이라 commit 이 `unknown` 이고, `--out` 없이 돌리면 결과가 `internal\tools\scan-unknown.jsonl` 에 덮어써진다. 또 소프트 3000초와 하드 3600초의 차이는 600초라 `MAX_CALL_S`(1800)보다 작다. 소프트 한도 직전에 시작한 호출은 하드 데드라인에서 잘릴 수 있고 그 usage 는 partial 값이다. 선행 실행은 D-25 끝의 "재검증 후 수정" 전의 scan.py 로 돌았다고 추정한다. 근거는 파일 시각뿐이다(수정 전 판은 16:18:59, 재검증 수정판은 17:22 에 저장됐고 선행 실행은 17:17:30 에 시작했다). 성공 경로는 수정으로 바뀌지 않아 선행 5파일의 데이터에는 영향이 없다. 추정한 파일의 SHA256 은 `2B6BD8D63127AFC029C3C9DEAB00BECC9BC1AE5C610C54791C232AFF710C29B2` 이고(D-25 검증 기록과 같다) 사본은 스크래치패드 `backup_20261021d\internal__tools__scan.py` 에 있다. `pilot-5.jsonl` 의 `_run` 행에는 scan.py 해시가 없다. `prompt_sha256`(앞 16자)과 model, effort, `call_timeout_s` 같은 설정만 있다.

파일럿 확인 항목(우선순위 순):
1. `files_fallback` 비율(거절되어 폴백으로 간 파일의 비율). 첫 기준선이다. 어느 범주가 거절되는지는 `stop_details.category` 가 있는 `ERROR stop_reason=refusal` 줄로 본다.
2. sticky 라우팅이 파일 사이에 걸리는지. 문서는 폴백한 요청 계열을 약 1시간 폴백 모델이 바로 받는다고 적는다. 거절 시도 항목 없이 `fallback_message` 만 있는 `_usage` 행이 앞 파일의 폴백 뒤에 이어지면 걸리는 것이다. 걸리면 `files_fallback` 은 실제 거절 횟수보다 크다.
3. `_usage.model` 이 요청한 `claude-fable-5-1` 과 같은지. partial 행의 폴백 표시가 이 비교에 기대므로(항목 4) 필수다.
4. 출력 도중 거절이 나는지, 이어쓰기 가정이 맞는지(D-24 §4), 폴백 파일의 후보 품질.
5. 호출 시간 분포: `_usage.call` 의 `elapsed_s`, `first_event_s`, `max_gap_s` 와 `output_tokens`. 생각하는 동안 이벤트나 바이트가 오는지(항목 1).
6. 30일 데이터 보존 요건이 Fable 5.1 에 걸리는지(걸리면 400), `claude -p --model claude-fable-5-1` 의 수용 여부.

선행 5파일의 결과는 아래 "선행 5파일 실행 결과" 소절에, 36파일 본 실행의 결과는 그 뒤 "본 실행 결과" 소절의 같은 순서 표에 있다.

### 2. 리뷰 지적 9건

| # | 지적 | 처리 |
|---|---|---|
| 1 | `CALL_TIMEOUT_S` 600 이 생각 중인 정상 호출을 끊고 차단기에 센다 | 1800 으로 올림. 호출마다 명시. 근거 없는 문구 정정. 측정 로그 추가 |
| 2 | "Project Glasswing 승인 고객 전용" | 검증 프로그램 승인 조직, Console 은 워크스페이스 단위 배정 |
| 3 | 기본 모델 | `claude-fable-5-1` (§1) |
| 4 | partial usage 의 "입력 토큰은 온전하다" 과장 | 문구 정정, 폴백 표시 추가 |
| 5 | README 의 "모델을 못 찾으면 404" | 번들 문서는 404 가 보이면 보존 설정을 먼저 보라고 권하고 최종 오류는 400 이다(재검증 후 정정). 이 조직의 mythos 404 는 실측 |
| 6 | 소프트 여유의 전제(타임아웃 3630초 이상) 누락 | 주석·문서 정정, 경고 한 줄 |
| 7 | SSE error 이벤트의 치명 타입 누락 | 네 타입을 치명으로 |
| 8 | 완료 직전 타이머 close 경합 | 구현(15줄 안) |
| 9 | selftest 건수를 손으로 쓴 합 | 실제 카운터 |

1. 무응답 상한. 스트리밍에서 `CALL_TIMEOUT_S` 는 읽기 사이 간격의 상한이다. 모델이 생각하는 동안 ping 이나 바이트가 오는지는 공식 문서에 없고 미확인이다. 안 온다면 600초를 넘기는 정상 호출이 무응답 timeout 으로 끊기고 차단기에도 센다.
   - 값을 `MAX_CALL_S`(1800)와 같게 올렸다. 총시간은 데드라인 타이머가 묶으니 올려도 하드 한도를 넘지 않는다. 1800초 무응답은 실제 장애라 차단기에 세는 것은 그대로다. 어댑터 `MAX_CALL_S` 를 바꾸면 이 값도 손으로 맞춘다. 시험이 둘의 일치를 고정한다.
   - 요청 범위 밖이지만 같이 고친 것: `--deadline-s` 가 없으면 호출이 SDK 기본 read timeout 600초를 그대로 써서 위 수정이 닿지 않았다. 코디네이터의 선행 5파일 실행이 이 경로일 수 있다. 이제 호출마다 `with_options(timeout=Timeout(t, connect=min(5, t)), max_retries=retries)` 로 명시한다(재시도 수는 SDK 기본 2 와 같다). 없으면 항목 1 이 이 경로에서 성립하지 않아 넣었다.
   - 근거 없던 문구 정정: scan.py 주석, D-24 §3 측정 1, README 의 "`display` 기본이 omitted 라 바이트는 온다"를 "미확인, 파일럿에서 측정"으로 고쳤다. D-24 §5 의 "600초 무응답이 나오면 ping 이 안 온다는 뜻" 추론도 접었다(그 값 자체를 바꿨으므로).
   - 시험 이름: `test_deadline_closes_a_stream_during_silent_thinking_and_keeps_the_partial_usage` 를 `test_deadline_closes_a_stream_that_only_sends_pings_and_keeps_the_partial_usage` 로 바꿨다. 가짜 서버가 ping 만 보내는 경우를 흉내 낼 뿐이라고 주석에 적었다.
   - 파일럿 측정 로그: `_usage.call` 에 `first_event_s`(스트림이 열린 직후부터 첫 이벤트까지)와 `max_gap_s`(이벤트 사이 최대 간격)를 더했다. scan.py `read_stream()` 이 스트림을 `for e in st` 로 읽으며 잰다. SDK 가 ping 을 호출자에게 안 줘서(`_streaming.py` 의 `continue`) 둘 다 호출자가 보는 이벤트 기준이다. `max_gap_s` 가 `timeout_s` 이상인데 무응답 timeout 이 안 났다면 그 사이에 바이트가 왔다는 뜻이라, 생각 중 바이트의 유무를 간접으로 볼 수 있다.
2. Glasswing 표현. DECISIONS D-24 §2, D-24 한계 절, 아직 안 된 것 표와 README 의 문구를 "검증 프로그램(Cyber Verification Program 등) 승인 조직, Console 은 워크스페이스 단위 배정"으로 고쳤다. 번들 문서 1759행의 옛 표현이라는 점을 적었다.
3. 기본 모델은 §1.
4. partial usage.
   - 문구: 입력·캐시 토큰은 message_start 가 담은 시도의 값이다. 끊기기 전에 폴백이 있었으면 다른 시도의 입력은 빠질 수 있다(시도별 값인 `iterations` 는 message_delta 에만 실린다). scan.py `partial_usage`, 어댑터의 `sum_usage` 독스트링과 `error` 메모, D-24 §3, README 를 고쳤다. 메모는 이제 "tokens_out is a lower bound and tokens_in can leave out the input of the other attempt (a fallback before the cut)" 이다. 처음 문구는 "earlier attempt" 로 방향을 정했는데 틀려서 재검증 후 고쳤다(재검증 후 수정 항목 1).
   - 폴백 표시: `partial_usage(st, model)` 이 message_start 의 `model` 과 요청 모델을 비교해서 다르면 그 행의 `fallback` 을 `true` 로 한다(출력 전 폴백 뒤에 끊긴 경우). 출력 도중 폴백도 SDK 가 fallback 블록에서 스냅샷의 model 을 폴백 모델로 바꾸므로 같은 방식으로 잡힌다. 어댑터의 `fallback_usage` 는 이 행도 센다("폴백 모델이 응답을 낸 파일 수"에 더해 "폴백 모델이 답하기 시작한 뒤 끊긴 파일"). 독스트링에 적었다.
5. README 의 404. "모델을 못 찾으면 404" 를 "번들 문서(claude-api 스킬 번들 `C:\Users\정동욱\AppData\Local\Temp\claude\bundled-skills\2.1.299\f01998021b84cff08d755411397bd05b\claude-api\shared\model-migration.md` 1686행. 세션 임시 디렉터리라 사라질 수 있다)는 404 가 보이면 보존 설정을 먼저 보라고 권하고, 최종 오류는 400 이다. 이 조직에서 mythos-5-1 이 count_tokens 404 인 것은 2026-10-21 실측"으로 고쳤다. 처음에 쓴 "접근이나 데이터 보존 문제에서도 404 가 올 수 있다"는 문서보다 강한 표현이라 재검증 후 정정했다(재검증 후 수정 항목 5).
6. 소프트 여유의 전제. 소프트 한도 직전에 시작한 최악 호출(1800초)이 하드 데드라인 안에 끝나는 것은 하네스 타임아웃이 `2 × SOFT_MARGIN_S` = 3630초 이상일 때만이다. 그 아래에서는 하한(timeout/2)이 먼저 걸린다. `adapters/attacktrail.py` 의 주석과 D-24 한계 표의 해당 행을 고쳤다("기본 1800초에서는 해당 없다"는 D-24 에서 소프트 여유를 1815초로 키운 뒤로 틀렸다. API 어댑터에 `--timeout 1800` 을 주면 해당된다). API 어댑터 `_run` 은 타임아웃이 3630 미만이면 stderr 에 한 줄을 낸다(`attacktrail: warning: project timeout 1800s is below 3630s ...`). 실행을 막지는 않는다. 짧은 시험 실행이 필요할 수 있다. CLI 어댑터는 이 계산을 안 쓰므로 경고가 없다.
7. SSE error 이벤트. `SSE_ERRORS` 에 `authentication_error`, `permission_error`, `billing_error`, `not_found_error` 를 `"fatal"` 로 더했다. HTTP 401·403·402·404 와 같이 첫 발생에 남은 호출을 멈추고 exit 2 다. stderr 줄은 `APIStatusError (HTTP 200, 스트림 error 이벤트): ...` 로 나오고 type 은 메시지 안에 있다(200 응답으로 오는 이벤트라서). 처음에는 `(HTTP 200)` 만 찍었다(재검증 후 수정 항목 7).
8. 완료 직전 타이머 close 경합. 구현했다. `read_stream()` 이 스트림을 읽다가 예외가 나면, `message_stop` 까지 받았고 스냅샷의 `stop_reason` 이 `end_turn` 이면 그 스냅샷을 정상 결과로 돌려준다. 안 그러면 끝난 응답이 timeout 으로 버려지고 과금만 남는다. 그 외(`message_stop` 전, 또는 `refusal`·`max_tokens`)와 치명 오류(인증·권한·결제·모델 없음 error 이벤트, 재검증 3차)는 예외 그대로다. 그 파일은 어차피 ERROR 이고, 달라지는 것은 usage 가 partial 값인지뿐이다. 다만 message_stop 을 받은 뒤의 예외는 차단기에 세지 않는다(중립. 치명 error 이벤트는 예외로 첫 발생에 멈춘다, 재검증 2차 B). 처음에는 refusal·max_tokens 의 끊김도 연결 오류로 세서 재검증 후 고쳤다(재검증 후 수정 항목 4).
9. selftest 건수. 손으로 쓴 합 대신 `chk()` 가 검사하고 통과한 건수를 센다. 처음에는 `chk` 안이 `assert ok, msg` 문이라 `python -O` 에서 검사 없이 통과했다. 재검증 후 `if not ok: raise AssertionError(msg)` 로 고쳤다(재검증 후 수정 항목 3). (`selftest()` 안의 `assert` 42개를 `chk` 로 바꿨고 반복문 안의 것은 반복마다 센다). 출력은 `selftest OK (N checks)` 다. 변경 전 코드를 같은 방식으로 세면 112건이다. 손으로 쓴 합 107 은 5건 모자랐다. 새 검사 15건(partial 폴백 표시 4, `read_stream` 6, `call_limits` 1, `classify` 4)을 더해 127건이다.

### 3. 바꾸지 않은 것

- D-24 §9 의 목록 그대로. 거기에 더해 스트림 도중 끊김의 재시도, 비용 차단기, 사용자 메시지 끝 안내문(D-24 §2)도 그대로다.
- 정지 중인 스트림을 차단기에 세는 것(항목 1).
- `refusal`·`max_tokens` 응답이 완성된 뒤 닫히는 경합(항목 8). 그 응답을 정상 결과로 살리지는 않는다. 차단기 분류만 중립으로 바꿨다(재검증 후 수정 항목 4).

### 4. 알려진 한계

- 항목 8 의 경합 자체(타이머가 응답의 마지막 읽기와 겹치는 순간)는 가짜 서버로 재현하지 못했다. 타이밍에 달린 일이다. 같은 예외 경로를 결정적으로 만드는 방식(message_stop 까지 보내고 끝 청크 없이 연결을 끊음)으로 시험했다.
- partial 행의 `fallback` 은 이름 비교다. 요청이 별칭이고 응답 `model` 이 정식 이름이면 같은 모델인데도 `true` 가 된다. 응답 `model` 이 요청 이름과 같은지는 파일럿에서 본다(위 확인 항목 3). partial 행의 `tokens_in` 은 여전히 폴백이 있었다면 message_start 가 담지 않은 쪽 시도의 입력이 빠질 수 있다(앞선 시도인지 나중 시도인지는 정하지 않는다, 재검증 후 수정 항목 1).
- `first_event_s`, `max_gap_s` 는 호출자가 본 이벤트 시각이다. 클라이언트가 첫 이벤트를 처음 파싱하는 데 이 머신에서 0.2~0.3초가 걸려서, 가짜 서버가 0.83초에 쓴 message_start 를 1.03초에 봤다. 그만큼 `first_event_s` 는 늘고 `max_gap_s` 는 준다. 분 단위 호출에서는 값에 거의 안 들어간다. 시험이 이 오차를 감안한 범위로 잰다.
- 이제 `--deadline-s` 없이 직접 돌리면 무응답 호출이 최대 1800초를 기다린다. 총시간은 못 묶는다(원래도 그랬다).
- 모델이 생각하는 동안 ping 이나 바이트가 오는지는 여전히 미확인이다. 1800초가 충분한지는 파일럿의 `max_gap_s` 로 정한다.
- Fable 5.1 의 30일 데이터 보존 요건과 안전 분류기 거절률은 미확인이다(§1 확인 항목).

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 127건(실제 카운터). 위 항목 9 의 내역.
- `tests/test_scan_parse.py`(72건, +3): 진짜 SDK 스트림으로 `read_stream` 경합(message_stop 뒤 닫힘은 정상, message_stop 전과 `refusal` 은 예외 그대로), `CALL_TIMEOUT_S == MAX_CALL_S`, selftest 건수가 소스의 `chk()` 호출 수 이상. 기존 시험에 `partial_usage` 의 요청 모델 비교, error 이벤트 치명 4종, `call_limits` 의 1800 경계(5416/5415)를 더했다.
- `tests/test_scan_fake_api.py`(69건, +8): 호출 로그의 `first_event_s`·`max_gap_s`(서버가 1.0초, 2.5초 쉼), message_stop 까지 보내고 연결을 끊어도 정상 결과이고 usage 가 partial 이 아님, 폴백 모델이 시작한 스트림이 끊기면 partial 행 `fallback` 이 true 이고 `fallback_usage` 가 셈, error 이벤트 치명 4종(첫 발생에 exit 2, 요청 2건), 무응답 스트림 시험을 `--deadline-s` 있음·없음 둘로(없을 때 SDK 기본 600초가 걸리면 100초 한도에서 실패). 기존 시험은 `CALL_TIMEOUT_S` 1800, `x-stainless-read-timeout` 1800(데드라인 없음, 데드라인 20000), `call` 키 6개, 모델 Fable 5.1 로 고쳤다.
- `tests/test_adapters.py`(54건, +3): 타임아웃 3630 경계 행과 소프트 여유 조건(3630 이상이면 하드와 소프트 차이가 `MAX_CALL_S` 이상, 미만이면 작음), 경고 한 줄(1800, 3629 는 경고, 3630, 10800 은 없음, 실행은 막지 않음), 끊긴 스트림의 폴백 파일 집계, partial 메모 문구.
- 나머지 파일의 건수는 그대로다.
- mutation 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치패드 `mutate25.py`, 복구는 sha256 으로 확인). 22개 변이가 모두 시험 실패로 잡혔다.

| 변이 | 잡은 시험 |
|---|---|
| 기본 모델을 Mythos 로 되돌림 | 요청 본문 시험, 어댑터 지문 시험, CLI 모델 시험 |
| `CALL_TIMEOUT_S` 600 | 상수 일치 시험, 기본 실행 헤더 시험 |
| `--deadline-s` 없을 때 timeout 을 안 명시 (2종) | 기본 실행 헤더 시험, 무응답 시험(데드라인 없음) |
| `first_event_s` 0 고정 / `max_gap_s` 0 고정 / `call` 키 삭제 | 호출 로그 시험, `call` 키 시험 |
| partial 폴백 표시 제거 / 요청 모델 미전달 / 항상 true | partial 폴백 시험, `partial_usage` 시험 |
| error 이벤트 치명 4종 제거 / `not_found` 만 / `billing` 만 / 모든 error 이벤트를 치명으로 | error 이벤트 치명 시험, `classify` 시험, 차단기 시험 |
| 경합 처리 제거 / `end_turn` 아닌 것도 수용 / `message_stop` 무시 | 경합 시험, `read_stream` 시험 |
| 검사 카운터가 안 오름 | selftest 건수 시험 |
| 경고 제거 / 3630 에서도 경고 / 짧은 타임아웃 거부 | 경고 시험 |
| partial 메모를 옛 문구로 | partial 메모 시험 |

검증 기록(2026-10-21, D-25 이후):

```
pytest                       297건 통과, 실패 0건. 파일별로 나눠 돌렸다
                             (eval/ 에서 `.\.venv\Scripts\python.exe -m pytest tests\<파일>.py -q -p no:cacheprovider`)
                             adapters 54, attacktrail_cli 18, cli 25, report 24, runner 25,
                             scan_fake_api 69, scan_parse 72, scan_select 4, scoring 6
scan.py --selftest           selftest OK (127 checks)
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
call_shape                   api-v6 (API), cli-v1 (CLI)
model / effort (manifest)    claude-fable-5-1 / xhigh (API), claude-fable-5-1 / n/a (CLI)
default_timeout_s            10800 (API), 1800 (CLI)
scan.py                      SHA256 2B6BD8D63127AFC029C3C9DEAB00BECC9BC1AE5C610C54791C232AFF710C29B2
                             (D-24 끝 시점은 C6EAA91DB8438D520A73B605FE84C0FF337514A6FB021110B2E3CDABE9F04E2F)
```

### 재검증 후 수정 (2026-10-21)

D-25 를 다시 검증하면서 나온 작은 결함 8건이다. 새 D 번호는 만들지 않았다. 이 소절의 수정 작업은 API 를 호출하지 않았다. D-25 작업 중 실제 키로 한 호출은 조회뿐이다: `/v1/models`(allowed_fallback_models 확인)와 `count_tokens`(mythos-5-1·mythos-5 404, fable-5-1 통과), 2026-10-21, 생성 호출 없음. 선행 5파일 실행은 사용자 승인 뒤 17:17 에 1회. 코디네이터의 선행 5파일 실행(파일 시각 기준으로 수정 전 scan.py 를 이미 로드했다고 추정한다)과 그 결과 폴더는 건드리지 않았다.

| # | 결함 | 처리 |
|---|---|---|
| 1 | partial 메모의 "earlier attempt" 가 방향을 단정 | "the other attempt (a fallback before the cut)" |
| 2 | README 에 스트림 안 error 이벤트의 치명 처리가 없음 | 추가 |
| 3 | `python -O scan.py --selftest` 가 검사 없이 통과 | `chk` 를 `raise AssertionError` 로 |
| 4 | message_stop 뒤에 끊긴 refusal·max_tokens 를 차단기가 연결 오류로 셈 | 중립으로 |
| 5 | 404 근거가 번들 문서보다 강함 | 정정 |
| 6 | 선행 5파일 계획에 실제 명령이 없음 | §1 마지막 계획 항목에 기재 |
| 7 | 치명 SSE 로그의 "(HTTP 200)" | "(HTTP 200, 스트림 error 이벤트)" |
| 8 | `tests/test_scan_select.py` 가 문서에 언급 없음 | 기록만 |

1. partial 메모 문구. 출력 도중 거절에서는 message_start 가 요청 모델의 입력을 담고, fallback 블록 뒤에 스냅샷의 model 이 폴백 모델로 바뀐다. 그 뒤에 끊기면 빠지는 쪽은 나중 시도(폴백)의 입력이다. 출력 전 폴백이면 반대로 앞선 시도의 입력이 빠진다(message_start 가 폴백 모델의 값을 담는다). 두 방향 모두 SDK 구현(`_beta_messages.py` 517~520, 580~601행) 기준 추정이고 와이어 관찰은 없다. 그래서 "earlier attempt" 로 방향을 단정한 것은 틀렸다. 방향 없는 "tokens_in can leave out the input of the other attempt (a fallback before the cut)" 로 고쳤다(`adapters/attacktrail.py` 의 `error` 메모, `tests/test_adapters.py`, 위 §2 항목 4 와 §4). 시험은 새 문구가 들어 있고 "earlier attempt" 가 없는지 본다.
2. README 의 `401·402·403·404` 문장 뒤에 스트림 안의 error 이벤트(HTTP 200)도 type 이 `authentication_error`, `permission_error`, `billing_error`, `not_found_error` 면 첫 발생에 멈춘다는 문장을 더했다. 동작은 D-25 §2 항목 7 에서 이미 구현돼 있었고 문서만 빠져 있었다.
3. `python -O`. `chk` 안의 `assert ok, msg` 는 `-O`(와 `PYTHONOPTIMIZE`)에서 지워진다. 그러면 `python -O scan.py --selftest` 는 검사를 하나도 하지 않고 `selftest OK (127 checks)` 를 찍는다(건수는 `chk` 호출 수라 그대로 오른다). `if not ok: raise AssertionError(msg)` 로 바꿨다. scan.py 에 다른 `assert` 문은 없다. 시험 `test_selftest_still_checks_under_python_O` 는 하위 프로세스로 두 가지를 본다. 그대로 `-O` 로 돌리면 통과하고, `msg.sender.call` 발췌 항목의 기대값 `"OK"` 를 `"EXCERPT_MISMATCH"` 로 바꾼 소스 복사본(파일이 아니라 메모리에서 `exec`)을 `-O` 로 돌리면 `AssertionError` 로 실패한다. `chk` 를 `assert` 문으로 되돌리면 이 시험이 실패한다(변이로 확인).
4. close 경합 뒤의 차단기 분류. `read_stream` 은 message_stop 까지 받은 뒤의 예외를 `end_turn` 일 때만 삼킨다(§2 항목 8. 치명 오류는 `end_turn` 이어도 올린다, 재검증 3차). refusal·max_tokens 응답이 message_stop 까지 온 뒤 연결이 끊기면 예외가 그대로 나가고, `classify` 가 `TransportError` 를 연결 오류로 보고 차단기에 셌다. 그런 파일이 8개 연속이면 API 가 멀쩡한데 스캔이 exit 2 로 멈춘다. 방침은 message_stop 을 받은 뒤의 예외는 중립(None)이다. 응답이 HTTP 200 으로 완결됐으므로 API 장애가 아니다. `scan_one` 의 except 에서 `ev["stop"]` 이 참이면 `classify` 를 부르지 않고 `cat = None` 으로 둔다. 중립이라 카운터를 올리지도 0 으로 되돌리지도 않는다. 그 파일은 전처럼 `ERROR stream interrupted: ...` 이고 usage 는 partial 행이다. message_stop 전의 끊김은 그대로 연결 오류로 센다. 시험은 refusal 과 max_tokens 응답을 교대로 12개 파일에 보내고 매번 message_stop 뒤에 연결을 끊는다. 차단기 N 이 8 이니 옛 동작이면 8번째에서 exit 2 이고 요청이 8건이다. 지금은 exit 0, 요청 12건, 전부 `ERROR stream interrupted` 다(변이로 확인). 이 중립 처리에서 치명 error 이벤트는 2차에서 뺐다(2차 항목 B).
5. 404 근거. claude-api 스킬 번들 문서 `C:\Users\정동욱\AppData\Local\Temp\claude\bundled-skills\2.1.299\f01998021b84cff08d755411397bd05b\claude-api\shared\model-migration.md` 의 1686행(세션 임시 디렉터리라 사라질 수 있다)은 Fable 5.1·Mythos 5.1 이 보존 요건 때문에 400 `invalid_request_error` 를 낸다고 적고, 초안에 404 라는 설명이 있었지만 최종 문구는 400 이며 "If you do see a 404 on the ID, check retention before anything else" 라고 한다. 그래서 "접근이나 데이터 보존 문제에서도 404 가 올 수 있다(1686행)"는 문서보다 강한 표현이었다. README 42행과 §2 항목 5 를 "번들 문서(model-migration.md 1686행)는 404 가 보이면 보존 설정을 먼저 보라고 권하고, 최종 오류는 400 이다. 이 조직에서 mythos-5-1 이 count_tokens 404 인 것은 2026-10-21 실측"으로 맞췄다(2차 재검증에서 두 곳 모두 위 경로를 풀어 썼다). 2차 재검증(2026-10-21)에서 위 경로의 1686행을 Read 로 직접 열어 확인했다: "In order to access this model, your organization or workspace must have data retention enabled." 라는 400 문구가 있다. mythos 404 가 접근 배정 때문인지 보존 설정 때문인지는 구분하지 못했다.
6. 선행 5파일 계획. §1 의 마지막 계획 항목에 실제 명령 형태, 기본 모델·effort·workers, `--out` 을 빼면 `internal\tools\scan-unknown.jsonl` 을 덮어쓰는 위험, 수정 전 scan.py 의 해시를 적었다. 지시는 그 해시가 `pilot-5.jsonl` 의 `_run` 행에 있다고 적으라는 것이었지만 scan.py 의 `_run` 행(`run` 딕셔너리)에는 scan.py 해시가 없다. 있는 것은 `prompt_sha256` 앞 16자와 설정값이다. 그래서 "없다"고 적었다. 수정 직전 scan.py 의 수정 시각(16:18:59)은 선행 5파일 폴더(`probe5\secondswap5`)의 수정 시각(16:25:52)보다 앞이고 해시가 D-25 기록과 같다.
7. 치명 SSE 로그. stderr 줄을 `APIStatusError (HTTP 200, 스트림 error 이벤트): ...` 로 바꿨다(HTTP 상태 오류에는 붙지 않는다). 시험은 `HTTP 200` 부분 문자열만 보므로 그대로 통과한다.
8. `tests/test_scan_select.py`(4건)는 새 파일이 아니다. D-22 이전부터 있던 기존 파일이다(2026-10-19 작성, 주석은 D-16 을 가리킨다). `select_files` 시험 3건(루트 경로에 `lib` 가 있어도 선택, 비웹 규칙 유지, `--web` 확장)과 CLI 어댑터가 파일 0개를 `crashed` 로 기록하는 시험 1건이다. 이 문서의 검증 기록에는 건수(4)만 있고 설명이 없었다. README 에는 테스트 파일 목록이 없어 고치지 않았다. git 에는 아직 추적되지 않는다.

테스트(실제 API 호출 없음):

- `tests/test_scan_parse.py`(73건, +1): `test_selftest_still_checks_under_python_O`.
- `tests/test_scan_fake_api.py`(70건, +1): message_stop 뒤에 끊긴 refusal·max_tokens 12건이 차단기에 안 센다.
- `tests/test_adapters.py`(54건, 변동 없음): partial 메모 시험의 단언을 새 문구로 바꿨다.
- mutation 확인(스크래치패드 `mutate_d.py`, 복구는 sha256 으로 확인): `chk` 를 `assert` 문으로 되돌린 변이와 `classify` 를 message_stop 과 무관하게 부르는 변이가 각각 새 시험 실패로 잡혔다.

검증 기록(2026-10-21, 재검증 후 수정 뒤):

```
scan.py --selftest           selftest OK (127 checks)
python -O scan.py --selftest selftest OK (127 checks)   (검사가 실제로 도는지는 위 시험으로 본다)
pytest                       299건 통과, 실패 0건 (파일별로 나눠 돌렸다)
                             adapters 54, attacktrail_cli 18, cli 25, report 24, runner 25,
                             scan_fake_api 70, scan_parse 73, scan_select 4, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
scan.py                      SHA256 102E17BFE924158FA920BC1C168C187C0393D7BCD2A5B07FF37AA25104E694A6
                             (재검증 수정 전은 2B6BD8D63127AFC029C3C9DEAB00BECC9BC1AE5C610C54791C232AFF710C29B2)
```

#### 2차 (2026-10-21)

재검증 2차가 남긴 코드 2건과 문서 8건을 고쳤다. 새 D 번호는 만들지 않았다. 이 작업은 API 를 호출하지 않았다(D-25 작업 중 실제 키로 한 호출은 조회뿐이다. §1 첫 문단과 같다). 시험은 127.0.0.1 가짜 서버와 가짜 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021f/` 에 있다. 선행 5파일 결과 폴더(`probe5\`)는 읽기만 했다.

| # | 지적 | 처리 |
|---|---|---|
| A | message_stop 뒤에 끊긴 refusal·max_tokens 의 ERROR 에 stop_reason·category 가 없어 확인 항목 1 의 거절률 집계에 안 잡힘 | ERROR 끝에 `; stop_reason=<값> category=<값>` 을 붙임 |
| B | message_stop 뒤의 예외를 전부 중립으로 둬서 그 뒤에 온 치명 error 이벤트도 묻힘 | 치명은 그대로 두고 나머지만 중립 |
| 1 | 36 파일 구성이 실제와 다름 | `select_files` 실행 결과로 다시 씀(`.sol` 15 + `.ts` 21). `test/`·`ignition/` 은 D-16 의 제외 규칙(.git, node_modules, lib 만 제외)에 따라 포함된다 |
| 2 | 비용·시간 산식이 한 줄씩 없고 "20~30분"에 근거가 없음 | 산식을 한 줄씩 적고 "20~30분" 삭제(이론 약 11분, 실측 전). 출력 범위는 정정한 구성 기준 17.1만~21.3만 토큰, 합 약 $10.5~12.6 으로 통일 |
| 3 | "API 는 한 번도 호출하지 않았다"가 조회 호출과 어긋남 | 조회 호출과 선행 5파일 1회를 명시(D-25 첫 문단, 재검증 후 수정 첫 문단) |
| 4 | `max_gap_s` 를 무이벤트 구간 전체로 읽음. 1800 근거가 "같아야 한다" | 단일 최장 간격으로 정정, 생각 구간 해석은 추정으로 표시, 근거를 865초의 2배 이상과 234.2초의 7.7배로. README 도 같이 |
| 5 | 404 근거 출처가 경로 없이 "번들 문서" | 경로를 적고 1686행을 직접 열어 400 문구를 확인 |
| 6 | 선행 실행 시각의 출처와 "수정 전 scan.py" 가 단정 | 시작은 `_run.started` 17:17:30, exit 0 은 코디네이터 보고값, 수정 전 판은 파일 시각 기준 추정 |
| 7 | 출력 전·도중 폴백의 입력 방향 서술이 SDK 구현 기준 추정임을 안 밝힘 | 표시 추가(재검증 후 수정 항목 1, `test_adapters.py` 주석) |
| 8 | `-O` 시험 설명이 실제와 다름 | `msg.sender.call` 발췌 기대값을 바꾼 소스 복사본으로 정정(재검증 후 수정 항목 3) |

코드 A. `scan_one` 의 except 에서 `ev["stop"]` 이 참이고 치명이 아니면, `stream interrupted` 메시지 끝에 `; stop_reason=<값> category=<값>` 을 붙인다. 값은 `st.current_message_snapshot` 의 `stop_reason` 과 `stop_details.category` 이고 category 가 없으면 `null` 이다(`parse_candidates` 의 `stop_reason=refusal category=null` 과 같은 꼴). 예: `ERROR stream interrupted: RemoteProtocolError: ...; 받은 만큼만 집계; stop_reason=refusal category=cyber`. 파일럿 확인 항목 1 이 `stop_reason=refusal category=` 줄로 거절률을 세므로 이 경로도 잡혀야 해서 넣었다. 이 분기에는 `end_turn` 이 아닌 응답만 온다(`end_turn` 응답 뒤의 치명이 아닌 오류는 `read_stream` 이 살리고, 치명은 3차부터 `read_stream` 이 올려서 치명 경로로 간다). max_tokens 면 `stop_reason=max_tokens category=null` 이다. 데드라인 타이머가 닫은 호출(`ERROR timeout: hard deadline reached ...`)과 치명 경로에는 안 붙는다.

코드 B. 전에는 `ev["stop"]` 이 참이면 `classify` 를 부르지 않고 `cat = None` 이었다. 그러면 refusal 응답이 끝난 뒤에 온 `authentication_error` error 이벤트 같은 치명 오류가 중립으로 묻혀 스캔이 멈추지 않는다. 이제 `cat = classify(e)` 를 먼저 구하고 `if ev["stop"] and cat != "fatal": cat = None` 이다. 치명(인증·권한·결제·모델 없음)은 첫 발생에 exit 2 이고 연결 끊김 같은 나머지는 전처럼 중립이다. 다만 이 수정은 `end_turn` 응답 뒤에는 닿지 않았다. `read_stream` 이 `end_turn` 이면 예외를 전부 삼켜서 치명 error 이벤트도 이 분기에 오지 않았고, 응답이 정상으로 돌아가 다음 파일이 같은 오류로 계속 나갔다(3차 항목 1 에서 고쳤다). 치명이 아닌 예외는 지금도 `read_stream` 이 삼킨다(§2 항목 8).

테스트(실제 API 호출 없음):

- `tests/test_scan_fake_api.py`(71건, +1): 기존 시험(refusal·max_tokens 12건이 message_stop 뒤에 끊김)에 단언을 더했다. ERROR 가 refusal 6건은 `; stop_reason=refusal category=cyber`, max_tokens 6건은 `; stop_reason=max_tokens category=null` 로 끝난다. 새 시험 `test_a_fatal_error_event_after_message_stop_still_stops_the_scan` 은 refusal 응답을 message_stop 까지 보내고 `authentication_error` 이벤트를 붙인다. 첫 발생에 exit 2, 요청 1건, 나머지 2파일은 `skipped: aborted after fatal API error` 이고 차단기 문구는 없다.
- `tests/test_adapters.py`(54건, 변동 없음): 주석만 고쳤다(항목 7). 나머지 파일의 건수도 그대로다.
- mutation 확인: 수정 전 scan.py(`backup_20261021f`)로 잠깐 되돌려 두 시험을 돌리면 둘 다 실패했고(ERROR 에 stop_reason 이 없음, 치명 이벤트가 안 멈춤) 수정판으로 복구한 뒤 통과했다. 복구는 `cmp` 와 sha256 으로 확인했다.

검증 기록(2026-10-21, 2차 수정 뒤):

```
scan.py --selftest           selftest OK (127 checks)
python -O scan.py --selftest selftest OK (127 checks)   (검사가 실제로 도는지는 test_selftest_still_checks_under_python_O 로 본다)
pytest                       300건 통과, 실패 0건 (파일별로 나눠 돌렸다)
                             adapters 54, attacktrail_cli 18, cli 25, report 24, runner 25,
                             scan_fake_api 71, scan_parse 73, scan_select 4, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
scan.py                      SHA256 DBEFCC2631B62843EAB0FFC0CCE332C11C8E33C8541EDCBC7A77D203EF64CFB3
                             (2차 수정 전은 102E17BFE924158FA920BC1C168C187C0393D7BCD2A5B07FF37AA25104E694A6)
```

#### 3차 (2026-10-21)

재검증 3차가 남긴 결함 4건과 선택 1건을 고쳤다(코드 2, 문서 2, 시험 1). 새 D 번호는 만들지 않았다. 이 작업은 API 를 호출하지 않았다. 시험은 127.0.0.1 가짜 서버와 가짜 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021g/` 에 있다. 이 수정을 하는 동안 36 파일 본 실행이 돌고 있었다. 실행 중인 run 폴더와 스크래치패드 `fullrun\`·`probe5\` 는 건드리지 않았고, 본 실행은 시작할 때 로드한 scan.py 로 도니 이 수정은 그 실행에 반영되지 않는다(코디네이터 설명에 따른 것이고 이 기록을 쓰며 직접 확인하지 못했다).

| # | 지적 | 처리 |
|---|---|---|
| 1 | `end_turn` 응답 뒤의 치명 SSE 가 `read_stream` 에 삼켜져 치명 분기에 못 감 | 치명이면 `end_turn` 이어도 예외를 올림 |
| 2 | 1800 근거에 속도 조건이 없고 "무응답 기준"이 실제(이벤트 간격)와 다름 | 74 tok/s 유지 조건을 붙이고 36 tok/s 가정이면 약 1,778초임을 적음. "최장 이벤트 간격 기준"으로 정정 |
| 3 | 표기 3건: Marketplace.test.ts 입력 어림이 둘, 14,091 의 단위, D-16 표현 | 바이트 비율 기준 약 4.1만~4.4만만 남기고 추정 표시. 14,091 은 5파일 평균 출력으로, 36 파일 평균 약 5천은 가정 합계 ÷ 36 으로 명시. D-16 의 제외 규칙으로 서술 |
| 4 | 시험이 응답은 도착 순번 홀짝으로, 기대는 파일명 번호로 고름 | 응답도 파일명 번호로 고름 |
| 5 | 치명 SSE 의 진행 줄이 `ERROR {'type': 'error', ...}` 원문 dict | stderr 치명 줄과 같은 문구로 통일 |

코드 1. 2차 B 는 refusal·max_tokens 응답 뒤의 치명 error 이벤트만 잡았다. `read_stream` 의 except 가 `end_turn` 이면 예외를 전부 삼켜서, `end_turn` 응답 뒤에 `authentication_error` 가 오면 응답이 정상으로 돌아가 `scan_one` 의 치명 분기에 못 갔다. 스캔이 멈추지 않고 다음 파일이 같은 오류로 계속 나갔다. 조건을 `if getattr(snap, "stop_reason", None) != "end_turn" or classify(exc) == "fatal": raise` 로 바꿨다. 치명이 아닌 예외는 전처럼 삼킨다. 변수 이름에 주의할 점이 있다. 지시문에는 `classify(e)` 로 적혀 있었지만 그 except 는 예외를 변수로 받지 않았고, `e` 는 위 for 루프의 마지막 이벤트(`message_stop`)다. 그대로 쓰면 이벤트를 분류해서 치명이 안 잡히고, 실제로 그렇게 적용했을 때 `end_turn` 시험이 exit 0 으로 실패했다. 지금은 `except Exception as exc` 로 받아 `classify(exc)` 를 쓴다.

코드 5. `scan_one` 의 치명 경로가 돌려주는 사유를 stderr 치명 줄과 같은 문구(`<예외 클래스> (HTTP <코드>[, 스트림 error 이벤트]): <메시지 300자>`)로 통일했다. SSE error 이벤트의 진행 줄은 `ERROR APIStatusError (HTTP 200, 스트림 error 이벤트): {'type': 'error', ...}` 가 된다. 뒤의 메시지는 SDK 가 이벤트 본문 dict 를 `str()` 로 만든 값이라 dict 표기는 그대로 남는다. 앞에 예외 클래스와 HTTP 코드가 붙는 것이 바뀐 점이다. 지시는 SSE 였지만 같은 분기라 HTTP 401·402·403·404 의 진행 줄도 같은 꼴(`AuthenticationError (HTTP 401): ...`)이 된다. 어댑터는 `ERROR` 뒤 문구를 표시용으로만 읽고 `skipped:` 접두사만 구분하므로 영향이 없다.

문서. 항목 2 는 선행 5파일 소절의 관찰 1과 결정 근거, README 의 `--deadline-s` 항목에서 속도 조건을 붙였고, 같은 소절의 5.1배 문장과 "아직 안 된 것" 표의 호출 timeout 행에 남은 "무응답"도 "이벤트 간격"으로 맞췄다. 수치(865, 7.7배, 6.1배, 5.1배)는 그대로다. 항목 1 은 2차 블록의 코드 A·B 설명, §2 항목 8, 재검증 후 수정 항목 4, "아직 안 된 것" 표의 닫는 경합 행, README 의 usage 항목과 치명 오류 항목을 동작에 맞췄다.

테스트(실제 API 호출 없음):

- `tests/test_scan_fake_api.py`(73건, +2): `test_a_fatal_error_event_after_message_stop_still_stops_the_scan` 을 `stop_reason` 세 가지(refusal, max_tokens, end_turn)로 매개변수화했다. 모두 exit 2, 요청 1건, 나머지 2파일은 `skipped: aborted after fatal API error` 다. 진행 줄이 stderr 치명 줄과 같은 문구인지도 단언한다(도우미 `assert_progress_matches_fatal_line`. 스트림 error 이벤트 시험과 HTTP 치명 시험에도 같은 단언을 더했다). refusal·max_tokens 12파일 시험은 응답을 `int(name[1:3]) % 2` 로 골라 단언과 같은 기준이 됐다.
- `scan.py --selftest`(127 → 128건): `read_stream` 이 `end_turn` 응답 뒤의 치명 오류(status_code 401)를 올리는지 한 건 더했다.
- mutation 확인: 바꾼 조건을 옛 `if getattr(snap, "stop_reason", None) != "end_turn": raise` 로 되돌린 사본으로 돌리면 `end_turn` 시험과 selftest 의 새 검사가 실패하고(refusal·max_tokens 은 통과), `return rel, None, desc, usage` 를 `msg` 로 되돌리면 치명 시험 11건이 실패한다. 변이는 스크래치패드 사본에서만 했고 실제 scan.py 는 바꾸지 않았다(사본은 지웠다).

검증 기록(2026-10-21, 3차 수정 뒤):

```
scan.py --selftest           selftest OK (128 checks)
python -O scan.py --selftest selftest OK (128 checks)
pytest                       adapters 54, scan_fake_api 73, scan_parse 73 통과, 실패 0건 (파일별로 나눠 돌렸다)
                             cli·report·runner·scoring·attacktrail_cli·scan_select 는 이번 수정 범위 밖이라 다시 돌리지 않았다
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
scan.py                      SHA256 4661B1124E40246A1E0666E79E0CA703680F38BB716CE474F3E5FE292C1FF68D
                             (3차 수정 전은 DBEFCC2631B62843EAB0FFC0CCE332C11C8E33C8541EDCBC7A77D203EF64CFB3)
```

### 선행 5파일 실행 결과와 시간 설계 결정 (2026-10-21)

§1 의 선행 실행 계획을 코디네이터가 돌렸다. 새 D 번호는 없고 코드는 바꾸지 않았다. 아래는 결과와 사용자 결정의 기록이다. 결과 원본은 스크래치패드 `probe5\pilot-5.jsonl`, `pilot-5.stdout.txt`, `pilot-5.stderr.txt` 이고 건드리지 않았다. 수치는 jsonl 로 다시 계산해 맞췄다(29행: `_run` 1, `_usage` 5, 후보 23).

실행:
- 시작은 `_run.started` 기준 2026-10-21T17:17:30 이다(프로세스 시작 17:17:26 은 코디네이터 셸 기록). 종료는 `pilot-5.jsonl`·`pilot-5.stdout.txt` 의 마지막 수정 시각이 17:22:23 이다(코디네이터 셸 기록은 17:22:25). 약 5분이다. exit 0 은 코디네이터 보고값이고 이 기록을 쓰며 다시 확인하지 못했다.
- 명령은 §1 계획 그대로다: `scan.py <scratchpad>\probe5\secondswap5 --out <scratchpad>\probe5\pilot-5.jsonl --time-budget 3000 --deadline-s 3600`. 모델 `claude-fable-5-1`, effort `xhigh`, `max_tokens` 64000, `fallbacks` default, workers 4 는 기본값이다.
- scan.py 는 재검증 수정 전 판이라고 추정한다(SHA256 `2B6BD8D63127AFC029C3C9DEAB00BECC9BC1AE5C610C54791C232AFF710C29B2`). 근거는 파일 시각이다(수정 전 판 16:18:59, 수정판 17:22, 시작 17:17:30). `_run` 행에 scan.py 해시가 없어 실행된 판을 직접 확인하지는 못했다. 성공 경로는 수정으로 바뀌지 않아 데이터에는 영향이 없다. `_run` 행의 `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 지금과 같다.
- 대상 폴더가 git 저장소가 아니라 `commit=unknown` 경고가 stderr 에 한 줄 나왔다. stderr 에는 이것뿐이다.

결과:
- 파일 5개를 스캔 5, 실패 0, 건너뜀 0 으로 끝냈다. 후보 23개(`SecondSwap_Marketplace.sol` 10, `SecondSwap_VestingManager.sol` 6, `SecondSwap_StepVesting.sol` 5, `SecondSwap_Whitelist.sol` 2, `hardhat.config.ts` 0). 위치검증 실패로 버린 후보 0.
- 거절 0, 폴백 0. 다섯 `_usage` 행 모두 `model` 이 `claude-fable-5-1`, `fallback` 이 false 이고 `partial` 행은 없다. 응답 `model` 문자열이 요청 이름과 같아서 partial 행의 이름 비교 한계(D-25 §4)는 이 5건에서 드러나지 않았다. 400 도 없이 5건 모두 응답을 받았다.
- `service_tier` standard, `speed` standard, `inference_geo` global.

| 파일 | 후보 | input | output | cache_read | cache_write | elapsed_s | first_event_s | max_gap_s |
|---|---|---|---|---|---|---|---|---|
| `hardhat.config.ts` | 0 | 1,081 | 797 | 0 | 5,154 | 15.187 | 0.0 | 10.093 |
| `SecondSwap_Marketplace.sol` | 10 | 10,429 | 21,734 | 0 | 5,154 | 293.344 | 0.0 | 222.578 |
| `SecondSwap_StepVesting.sol` | 5 | 5,173 | 22,307 | 0 | 5,154 | 289.047 | 0.125 | 234.219 |
| `SecondSwap_VestingManager.sol` | 6 | 4,492 | 19,720 | 0 | 5,154 | 258.281 | 0.0 | 211.469 |
| `SecondSwap_Whitelist.sol` | 2 | 1,787 | 5,896 | 5,154 | 0 | 77.938 | 0.0 | 55.359 |
| 합계 | 23 | 22,962 | 70,454 | 5,154 | 20,616 | | | |

`cache_write` 는 `cache_creation_input_tokens` 이고 전부 5분 TTL 이다. 비용은 토큰 × 단가(Fable 5.1, 입력 $10, 출력 $50, 캐시 읽기 $0.25, 5분 쓰기 $12.50 per MTok)로 입력 $0.23, 출력 $3.52, 캐시 읽기 $0.001, 캐시 쓰기 $0.26, 합계 약 $4.01 이다.

관찰:
1. 가장 긴 무이벤트 구간이 호출 길이의 66~82% 다. `max_gap_s` 는 호출자가 본 이벤트 사이의 단일 최장 간격이다(무이벤트 구간의 합이 아니다). `elapsed_s` 대비 76%, 81%, 82%(큰 계약 3건: 222.6/293.3, 234.2/289.0, 211.5/258.3)이고 Whitelist 는 71%, hardhat 은 66% 였다. 그 구간이 모델이 생각하는 구간이라는 해석은 추정이다(구간 안에서 무슨 일이 있었는지는 기록하지 않았다). 1800 유지의 근거는 이렇다. 선행 실측 74 tok/s 가 유지되면 호출 길이는 64000 토큰 ÷ 74 ≈ 865초를 넘기 어렵고 1800 은 그 2배 이상이다. 속도가 D-24 의 가정(약 36 tok/s)으로 떨어지면 64000 토큰 출력이 약 1,778초라 1800 과 거의 같다. 최장 이벤트 간격 기준(SDK 가 ping 을 숨긴 뒤의 간격)으로는 최장 234.2초의 7.7배다. `CALL_TIMEOUT_S` 를 호출의 최악 길이(`MAX_CALL_S`)와 같게 둔 것은 보수적 선택이다. 옛 600 이어도 이번 5건은 안 끊겼지만(최장 234.2초) 더 큰 파일에서는 위험했다. 다만 이 구간이 그 실행의 시도당 timeout(1193.4초, 아래 결정 항목)보다 짧아서, 그 사이에 ping 이나 바이트가 왔는지는 여전히 모른다. D-25 §2 항목 1 의 간접 판별은 `max_gap_s` 가 timeout 이상일 때만 쓸 수 있다.
2. `first_event_s` 가 0.0~0.125초라 `message_start` 는 즉시 온다.
3. 출력은 최대 22,307 토큰(StepVesting)으로 `max_tokens` 64000 의 35% 다. `max_tokens` 로 잘린 파일은 없다.
4. workers 4 가 동시에 시작해서 첫 라운드 4건이 모두 캐시 쓰기(5,154 토큰 × 4)를 했다. 5번째 파일(Whitelist)만 캐시 읽기였다. 36 파일 본 실행도 첫 라운드 4회 쓰기 뒤 읽기로 바뀔 것이다. 이번 캐시 쓰기 합이 $0.26 이라 영향은 작다. 개선(첫 호출이 끝난 뒤 워커 시작)은 안 했다("아직 안 된 것"의 병렬 시작 행).
5. 후보 제목의 언어가 파일마다 다르다. Marketplace 와 Whitelist 는 영어, StepVesting 과 VestingManager 는 한국어다. 설명(`why`)은 후보가 있는 네 파일 모두 한국어 문장에 코드 식별자가 섞여 있다. scan.py 에 출력 언어를 정하는 문구가 없다(ROLE 포함). 고정하려면 프롬프트를 바꿔야 하고 그러면 `prompt_sha256` 이 달라지므로 이번엔 하지 않았다("아직 안 된 것"에 기록).
6. 후보 행에 `severity` 키는 없고 모델 추정 `severity_guess` 만 있다(High 1, Medium 5, Low 9, Informational 8). 스키마상 Step-01 은 후보만 내고 확정 판정은 사람이 AUD-SEV-01 로 하므로 정상이다.

§1 확인 항목별 결과(5파일, 호출당 1회 범위):

| 항목 | 결과 |
|---|---|
| 1. `files_fallback` 비율 | 0/5. 5파일 기준 첫 값이고 36 파일 비율이 아니다. 거절이 없어 범주(`stop_details.category`)는 못 봤다 |
| 2. sticky 라우팅 | 폴백이 없어 관찰할 대상이 없었다. 미확인 |
| 3. `_usage.model` 이 요청과 같은지 | 5/5 같다 |
| 4. 출력 도중 거절, 이어쓰기 가정, 폴백 파일 품질 | 거절이 없어 못 봤다. 미확인 |
| 5. 호출 시간 분포 | 위 표와 관찰 1~3. 시간 설계는 유지로 결정(아래) |
| 6. 데이터 보존 요건(400), CLI 수용 | API 는 5건 모두 400 없이 응답을 받았다. `claude -p --model claude-fable-5-1` 은 CLI 를 돌리지 않아 미확인 |

36 파일 본 실행 추정(secondswap). 파일 크기로 어림한 값이고 5파일 실측으로 검증한 것이 아니다.
- 구성(2차 재검증에서 고쳤다). `select_files(root, web=False)` 를 `datasets/sources/code4rena_secondswap_2025_02` 에 직접 돌린 결과이고 36 파일이다(`too_big` 없음, 합계 244,402 B).
  - `.sol` 15개(87,130 B). 큰 5개는 Marketplace 25,225 B, StepVesting 11,920, MarketplaceSetting 11,280, VestingManager 10,249, VestingDeployer 9,898 이다. 작은 10개는 최대 Whitelist 3,974 B 이고 `interface/` 5개를 포함한다.
  - `.ts` 21개(157,272 B). `test/` 4개(139,586 B, 최대 `Marketplace.test.ts` 98,662 B), `ignition/modules/` 16개(15,807 B), `hardhat.config.ts` 1개(1,879 B).
  - 앞 판의 "작은 .sol 6개 + .ts 25개"는 실제와 달랐다. 작은 .sol 은 10개, .ts 는 21개다.
- (D-26 에서 바뀜: 비웹 제외에 `test`·`ignition` 등이 들어가 이 36 파일은 16 파일이 된다.) `test/`·`ignition/` 가 들어가는 것은 이 시점의 결정이었다. 비웹 스캔의 제외는 `.git`, `node_modules`, `lib` 뿐이고, `vendor`·`dist`·`build`·`out` 같은 추가 제외 규칙은 `--web` 에만 걸린다(D-16, scan.py `SKIP_DIRS`·`SKIP_DIRS_WEB`). 일반 스캔의 파일 집합은 eval 비교를 위해 그대로 둔다.
- `Marketplace.test.ts`(98,662 B)의 입력은 약 4.1만~4.4만 토큰으로 어림한다(추정). 선행 5파일의 `.sol` 은 2.22~2.42 B/토큰(hardhat.config.ts 는 1.74)이라, 같은 바이트 비율을 대면 98,662 ÷ 2.42 ≈ 4.08만에서 98,662 ÷ 2.22 ≈ 4.44만이다. 선행 5파일의 최대 입력(10,429 토큰)의 약 4배다. `.ts` 에 이 비율이 맞는지는 측정하지 않았다. 출력은 `max_tokens` 64000 안에 든다고 보지만(선행 5파일의 최대 출력은 22,307 토큰) 추정이다. xhigh 의 사고 토큰도 64000 을 쓴다.
- 비용·시간 산식(추정, 실측 전):
  - 출력: 17.1만~21.3만 토큰(큰 5 × 2만 + 작은 .sol 10 × 5천 + .ts 21 × 1천~3천) × $50/M = $8.55~10.65.
  - 입력: 선행 실측 평균 4,592 토큰/파일 × 36 파일 × $10/M ≈ $1.65.
  - 캐시: 쓰기 4회(첫 라운드, workers 4) × 5,154 토큰 × $12.50/M ≈ $0.26 와 읽기 32회 × 5,154 토큰 × $0.25/M ≈ $0.04, 합 약 $0.3.
  - 합: $8.55~10.65 + $1.65 + $0.3 = $10.5~12.6, 약 $10.5~12.6. 입력을 바이트 환산으로 보면 하한 약 $9.9($8.55 + $1.06 + $0.3).
  - 시간: 19.2만 토큰(출력 범위의 중간) ÷ (4 워커 × 75 tok/s) = 640초 ≈ 11분. 이론값이고 실측 전 추정이다. 본 실행에서 잰다.
- 이 산식의 약한 곳. 출력 범위는 앞 판의 구성(큰 5 × 약 2만 + 작은 .sol 6 × 약 5천 + .ts 25 × 약 1천~3천, 15.5만~20.5만 토큰)에서 정정한 구성 기준 17.1만~21.3만으로 통일했다. 파일당 가정(큰 5 × 2만, 작은 .sol × 5천, .ts × 1천~3천)은 그대로이고 실측이 아니다. 입력 평균 4,592 토큰은 큰 계약 위주인 5파일의 값이다. 36 파일 합계 244,402 B 를 .sol 비율 약 2.3 B/토큰으로 나누면 약 10.6만 토큰($1.06)이라 더 낮을 수도 있다(.ts 비율은 미측정).
- D-24 §6 표의 가정(파일당 평균 1만 토큰, 약 $21~22)과 §5.2 의 계산(9라운드 × 280초 = 42분)보다 낮다. 5파일 평균 출력 14,091 토큰(70,454 ÷ 5)은 큰 계약 위주라 36 파일 평균과 다르다. 36 파일 평균 약 5천 토큰은 출력 가정 합계 17.1만~21.3만 ÷ 36 ≈ 4,750~5,900 에서 나온 값이고 실측이 아니다.

사용자 결정(2026-10-21): 호출 시간 설계는 실측을 본 뒤 지금 값을 유지한다.
- 유지하는 값은 `CALL_TIMEOUT_S` 1800, `MAX_CALL_S` 1800, API 어댑터 기본 프로젝트 timeout 10800, `max_tokens` 64000 이다. 소프트 여유 1815초는 `MAX_CALL_S` 에서 나온 값이라 같이 유지된다.
- 근거는 실측의 6배 이상 여유다. 1800 은 최장 호출 293.3초의 6.1배, 최장 이벤트 간격 234.2초(SDK 가 ping 을 숨긴 뒤의 간격)의 7.7배다. 이 여유는 선행 실측 74 tok/s 가 유지될 때의 값이다. 출력이 64000 토큰까지 가는 호출은 실측에 없고, 속도가 D-24 의 가정(약 36 tok/s)이면 그런 호출은 약 1,778초라 1800 과 거의 같다.
- 이 기록을 쓰며 jsonl 에서 계산한 값(결정 근거로 받은 것은 아니다): 출력 ÷ 소요가 큰 계약 3건은 74.1, 77.2, 76.4 tokens/s, Whitelist 는 75.6 이다(호출이 짧은 hardhat 은 52.5). `MAX_CALL_S` 를 잡은 D-24 §5.2 의 가정(약 36 tokens/s)의 약 2배다. 이 속도면 64000 토큰을 전부 써도 약 830~860초라 1800 안이다.
- 선행 실행에서 실제로 걸린 시도당 timeout 은 1800 이 아니었다. `--deadline-s 3600` 이라 `call_limits` 가 (남은 시간 − 재시도 2회 × 8초) ÷ 3 으로 줄여서 `call.timeout_s` 가 1193.4초(5번째 호출은 1188.3초)였다. 최장 이벤트 간격 234.2초는 그 한도의 5.1배다. 1800 자체를 겨눈 실측은 아니지만 더 짧은 한도(1193.4초)를 넘은 호출이 없었으니 1800 에서도 같다. 한도가 1800 인 것은 남은 시간이 5,416초 이상일 때이고(D-25 시험의 경계), API 어댑터 기본 10800(`--deadline-s` 10785)이면 시작 후 약 5,370초까지다. 본 실행의 이론 추정 약 11분(실측 전)은 그 안이다.
- 범위 밖이라 그대로 두고 못 잰 것: `--jobs` 기본 2(동시 8호출)와 Fable 5.x 속도 한도, `MIN_RETRY_ATTEMPT_S` 120, 연속 오류 8건, 어댑터 여유 15초. 이번 실행은 프로젝트 하나, workers 4 였고 실제 재시도 횟수는 기록에 없다.

알려진 한계:
- 5파일, 호출당 1회 측정이다. 출력이 64000 근처인 호출은 없었다(최대 22,307). 속도는 시각과 부하에 따라 달라질 수 있다.
- 후보 23개의 정확도는 이 기록의 범위 밖이다. 사람이 AUD-SEV-01 로 판정하고 PoC 를 만들기 전이다.
- 이 실행은 `eval run` 이 아니라 scan.py 직접 실행이라 manifest 와 `eval` 지표가 없다.

"아직 안 된 것" 표의 호출 timeout 설계 행은 "결정됨(D-25, 실측 근거)"으로 고쳤다. 시간 상수, 생각 중 ping·바이트, 병렬 시작 중복 쓰기 행에는 이 실행의 결과를 덧붙였고, 후보 언어 행을 맨 끝에 더했다. "effort 수준과 max_tokens 64000 한도", "서버 측 폴백의 실동작", "모델 선택" 행은 5파일 결과로 일부 답이 났지만 고치지 않았다(위 확인 항목 표 참고).

### 본 실행 결과 (2026-10-21)

선행 5파일 실행 뒤 코디네이터가 secondswap 36파일 본 실행을 돌렸다. manifest 의 `cli_argv` 는 `cli.py run --agent attacktrail --dataset scabench --projects code4rena_secondswap_2025_02 --judge mock --out runs\pilot-secondswap-fable` 이다. 새 D 번호도 코드 변경도 없다. 원본은 `eval/runs/pilot-secondswap-fable/` 의 `manifest.json`, `projects/code4rena_secondswap_2025_02/raw/scan.jsonl`, `raw/scan.stdout.txt`, `scores.json` 이고 읽기만 했다. 아래 수치는 코디네이터 집계를 이 파일들로 다시 계산해 맞춘 것이다(차이는 아래 "집계와 다른 곳"에 적었다).

실행:
- 코디네이터 셸 기록은 시작 18:33:46, 종료 18:45:58, exit 0 이다(exit 0 은 코디네이터 보고값이고 이 기록을 쓰며 다시 확인하지 못했다). 원본 파일의 시각은 manifest `started_at` 09:33:47Z(18:33:47 KST), `finished_at` 09:45:57Z, scan.py 헤더 `_run.started` 18:33:49, 프로젝트 `wall_seconds` 729.2 다. D-25 선행 소절의 이론 추정(약 11분, 실측 전)에 견주면 실측은 약 12분 9초다.
- 모델 `claude-fable-5-1`, effort `xhigh`, `max_tokens` 64000, workers 4, `call_timeout_s` 1800, 시간 한도 소프트 8985초와 하드 10785초(`--time-budget`·`--deadline-s`). 헤더의 `prompt_sha256` 앞 16자는 `2d0bb457ca2c605a` 로 전과 같다.
- 돈 scan.py 는 D-25 2차 수정판이다(스냅샷 `agent_snapshot/tools/scan.py` 의 SHA256 `DBEFCC2631B62843EAB0FFC0CCE332C11C8E33C8541EDCBC7A77D203EF64CFB3`, 2차 수정 뒤 기록과 같다). 실행 중에 3차 수정(`4661B1124E40246A…`)이 같은 경로에 저장됐다. 프로세스는 시작할 때 소스를 한 번 읽으므로 이 run 은 3차 수정을 쓰지 않았다.

결과:
- 36파일을 스캔 36, 실패 0, 건너뜀 0 으로 끝냈다. 거절, 폴백, partial 은 0 이다(`ERROR` 줄 0, `_usage` 행 36개 모두 `fallback` false 이고 `partial` 행 없음, stderr 는 빈 파일). 후보는 80개다. 검토 대기 79, 위치검증 실패로 폐기 1(`test/VestingManager.test.ts` 116행, EXCERPT_MISMATCH). manifest `n_findings` 는 79 다.
- `_usage.model` 은 36행 모두 `claude-fable-5-1` 이다. `service_tier` standard, `speed` standard, `inference_geo` global 이 36행 모두 같다.

토큰과 비용(Fable 5.1, 단가는 D-24 §6):

| 항목 | 토큰 | 단가 per MTok | 비용 |
|---|---|---|---|
| input | 99,734 | $10 | $1.00 |
| output | 179,852 | $50 | $8.99 |
| cache read | 164,928 | $0.25 | $0.04 |
| cache write (5분) | 20,616 | $12.50 | $0.26 |
| 합계 | | | $10.29 |

선행 5파일 $4.01 을 더한 추정 누적(손계산: 토큰 × 공식 단가, 청구서 값이 아니다)은 $14.30 이다. 선행 소절의 추정($10.5~12.6, 바이트 환산 하한 약 $9.9)과 견주면 합계는 추정 하한 아래이고 바이트 환산 하한 위다. 출력 179,852 토큰은 추정 범위 17.1만~21.3만 안이다. manifest `tokens_in` 285,278 은 `input_tokens` 와 두 캐시 항목의 합이다(99,734 + 164,928 + 20,616). API 행의 `input_tokens` 와 뜻이 다르다.

호출 시간과 출력(36호출. p90 은 작은 쪽부터 33번째 값):

| | 최소 | 중앙값(18·19번째 평균) | 19번째 값 | p90 | 최대 | 합 |
|---|---|---|---|---|---|---|
| `elapsed_s` | 2.3 | 31.4 | 34.0 | 211.2 | 315.4 (`test/StepVesting.test.ts`) | 2,405.5 |
| `max_gap_s` | 1.25 | 17.6 | 19.5 | 156.9 | 278.6 (`test/StepVesting.test.ts`) | |
| `output_tokens` | 78 | 2,330 | 2,493 | 15,727 | 24,391 (`test/StepVesting.test.ts`) | 179,852 |

- `first_event_s` 는 36호출 모두 0.0~0.11 이다.
- 60초를 넘긴 11호출의 출력 ÷ 소요는 69.6~79.6 tokens/s, 중앙값 73.5 다. 선행 5파일의 74~77 과 같은 대역이다. 최대 출력 24,391 은 `max_tokens` 64000 의 38% 이고 `max_tokens` 로 잘린 파일은 없다.
- 캐시: 첫 라운드 4호출이 5분 TTL 로 5,154 토큰씩 썼고(`hardhat.config.ts`, `contracts/SecondSwap_Artifact_Testing_Purposes.sol`, `contracts/SecondSwap_Marketplace.sol`, `contracts/SecondSwap_MarketplaceSetting.sol`) 이후 32호출이 5,154 토큰씩 읽었다. 1시간 TTL 쓰기는 0 이다.
- 집계와 다른 곳: 코디네이터 집계의 중앙값 34.0, 19.5, 2,493 은 36개 중 19번째 값이다. 중앙값을 18·19번째 평균으로 정의하면 31.4, 17.6, 2,330 이다. 나머지 수치(최소, p90, 최대, 합)는 일치한다.

출력의 절반이 비계약 파일이다:

| 구간 | 파일 | 출력 토큰 | 비율 | 후보 |
|---|---|---|---|---|
| `test/` | 4 | 49,581 | 27.6% | 17 |
| `ignition/` | 16 | 38,939 | 21.7% | 27 |
| 나머지 (`contracts/` 15, `hardhat.config.ts` 1) | 16 | 91,332 | 50.8% | 36 |
| 합계 | 36 | 179,852 | 100% | 80 |

- `test/` 와 `ignition/` 의 출력 합은 88,520 토큰으로 49.2% 다. 코디네이터 집계의 "약 89K/180K" 와 맞는다(89K 는 `hardhat.config.ts` 780 토큰을 더한 값).
- 후보 80 중 `test/`·`ignition/` 은 코디네이터 집계의 42 가 아니라 44(`test/` 17, `ignition/` 27)다. 위치검증으로 폐기된 1건이 `test/VestingManager.test.ts` 라서 manifest 79 기준으로는 43 이다.
- `severity_guess` 는 후보 80행 기준 High 2, Medium 10, Low 17, Informational 51 이다(검토 대기 79행 기준 Informational 50). 확정 등급이 아니고 사람이 AUD-SEV-01 로 판정한다.
- mock judge 의 TP 30, FN 0, FP 18(`scores.json` overall: matched 30, false_negatives 0, false_positives 18)은 배관용 수치다. 지적 내용과 무관한 결정값이라 탐지율로 읽으면 안 된다(README 알려진 한계). 실제 탐지율은 `eval rescore` 로 nethermind 재채점을 해서 본다. 재채점은 코디네이터가 진행하고 이 기록을 쓸 때까지 끝나지 않았다.

D-25 §1 파일럿 확인 항목의 결과:

| 확인 항목 | 본 실행(36파일) 결과 |
|---|---|
| 1. `files_fallback` 비율, 거절률 | 0/36. 거절 0 이고 `ERROR stop_reason=refusal` 줄이 없어 거절 범주(`stop_details.category`)는 못 봤다 |
| 2. sticky 라우팅이 파일 사이에 걸리는지 | 폴백이 없어 관찰할 수 없었다. 미확인 |
| 3. `_usage.model` 이 요청과 같은지 | 36/36 `claude-fable-5-1` |
| 4. 출력 도중 거절, 이어쓰기 가정, 폴백 파일 품질 | 거절이 없어 관찰할 수 없었다. 이어쓰기 가정은 미확인 |
| 5. 호출 시간 분포, 생각 중 이벤트·바이트 | 위 표. 생각 중 바이트의 유무는 여전히 미확인이다. `max_gap_s` 최대 278.6초가 시도당 timeout 1800초보다 짧아서 그 사이에 바이트가 왔는지는 가려지지 않는다 |
| 6. 30일 데이터 보존(400), CLI 수용 | API 36호출 모두 400 없이 응답을 받았다. `max_tokens` 잘림 0. `claude -p --model claude-fable-5-1` 은 CLI 를 안 돌려서 미확인 |

사용자 결정(2026-10-21): 호출 시간 설계는 이 결과를 보고도 유지한다(`CALL_TIMEOUT_S` 1800, `MAX_CALL_S` 1800, API 기본 프로젝트 timeout 10800, `max_tokens` 64000. 선행 소절의 결정과 같다). 다음 단계로 nethermind 재채점과, D-26 의 제외 규칙을 넣은 뒤의 재실행을 고른다.

이 run 의 manifest 지문. manifest `agent.scan_py_sha256` 은 run 이 끝난 시점의 라이브 파일 해시 `4661b1124e40246a…`(3차 수정판)이다. 이 run 이 실제로 시작한 코드의 해시는 `agent_snapshot/tools/scan.py` 의 `DBEFCC2631B62843…`(2차 수정판)이다. 레거시 `prompt_hash`(`c968a8bf…`)도 라이브 바이트로 계산된 값이라 스냅샷 기준 값과 다르다. `prompt_sha256` 은 `2d0bb457…` 이고 ROLE·기준 문서가 두 판에서 같아서 맞는 값이다(헤더 `_run.prompt_sha256` 앞 16자와 일치). 이 manifest 는 고치지 않았고 이 문단이 정정이다. 원인과 수정은 D-26 §5 에 있다.

## D-26. 비웹 스캔에서 테스트·배포·목 폴더를 빼고, 지문을 스냅샷에서 계산한다 (2026-10-21)

사용자 결정(2026-10-21)과 본 실행이 드러낸 지문 버그 수정이다. 본 실행에서 비계약 파일이 출력의 약 절반이었고, 사용자가 제외 규칙을 넣은 뒤 재실행하기로 했다. 시스템 프롬프트 본문(ROLE, 기준 문서, 사용자 프롬프트 틀), `SCHEMA`, 후보 검증, 호출 형태, 시간 설계, 모델, effort, `max_tokens` 는 그대로다. 실제 API 는 호출하지 않았다. 시험은 임시 폴더, 가짜 서버, 가짜 객체로만 했다. 수정 전 파일은 스크래치패드 `backup_20261021h/` 에 있다. `eval/runs/pilot-secondswap-fable/` 와 스크래치패드 `fullrun\`, `probe5\` 는 읽기만 했다. 재채점이 쓰는 `scoring/`, `runner/`, `report/render.py` 는 고치지 않았다.

### 1. 결정 배경

본 실행(36파일)에서 `test/` 4파일과 `ignition/` 16파일의 출력이 88,520 토큰으로 전체의 49.2% 였고, 후보 80 중 44 가 여기서 나왔다. 호출 시간 합은 2,405.5초 중 1,198.9초(49.8%)이고 가장 오래 걸린 호출 둘(315.4초, 211.2초)도 `test/` 였다. 이 파일들은 감사 대상 계약이 아니라 테스트와 배포 스크립트다. 일반 스캔의 파일 집합을 eval 비교를 위해 그대로 둔다던 D-16 의 이유보다, 이 비용과 시간이 더 크다고 사용자가 판단했다.

### 2. 정답 경로 검증

방법. SCABench 정답 항목에는 파일 경로 필드가 없다. 필드는 `finding_id`, `severity`, `title`, `description` 네 개뿐이다(README 알려진 한계와 같다). 그래서 경로를 title·description 글에서 읽었다.
- 29개 저장소 체크아웃(`datasets/sources/`)에서 `select_files` 가 고르는 파일 중 제안 폴더 아래에 있는 것마다, 정답 555건의 글에 그 파일이 이름으로 나오는지 센다. 상위폴더/파일명 또는 전체 상대경로가 글에 있으면 경로 일치, 파일명만 있으면 파일명 일치(프로젝트에 같은 파일명이 더 있으면 겹침)다.
- 나온 곳은 전부 사람이 읽고 "취약 위치"와 "PoC 를 붙이라는 위치"를 가렸다.
- 보조로 정답 글 전체의 `<폴더>/` 경로 토큰(31개 프로젝트 전부, 35건), 컨트랙트 이름(파일명 stem) 언급, 저장소 README 와 `scope.txt`·`out_of_scope.txt` 를 봤다.
- holdout: `datasets/holdout/projects/` 폴더가 없다(`TEMPLATE.json`, `validate.py`, `README.md` 뿐). 프로젝트가 0개라 검증할 정답이 없다. 템플릿의 정답 필드도 같은 네 개다. holdout 이 생기면 같은 검증을 다시 한다.
- 데이터셋 31개 중 `code4rena_initia-move_2025_04`, `code4rena_starknet-perpetual_2025_06` 은 `commit` 이 비어 체크아웃이 없다(README). 이 둘은 파일 목록 없이 글의 경로 토큰만 봤고 해당 언급은 없었다(`.move`·`.cairo` 는 어차피 `EXT` 밖이다).

폴더별 결과(`select_files` 로 고른 파일 기준, 29개 저장소):

| 폴더 | 파일 수 | 프로젝트 수 | 정답 중 이 폴더의 파일을 이름으로 든 것 | 그중 취약 위치 |
|---|---|---|---|---|
| `test` | 557 | 24 | 26건 (경로 일치 9, 파일명 13, 겹침 4. 읽은 결과는 PoC 안내 25, 이름만 겹침 1) | 0 |
| `tests` | 15 | 3 | 0 | 0 |
| `ignition` | 19 | 2 | 0 | 0 |
| `script` | 48 | 9 | 0 | 0 |
| `scripts` | 341 | 14 | 2건 (파일명 겹침) | 0 |
| `deploy` | 45 | 4 | 0 | 0 |
| `mocks` | 94 | 6 | 0 | 0 |
| 합계 | 1,119 | | 28 | 0 |

한 파일이 두 폴더 이름 아래에 있으면 두 줄에 센다(합 1,119, 중복을 빼면 1,113 파일이고 아래 §4 의 제외 수와 같다). 요청 목록 밖의 이름 세 개도 같이 확인했다: `mock` 18파일(4개 프로젝트), `__tests__` 5(1), `e2e` 4(2). 정답이 이름으로 든 것은 모두 0건이다. 사용자가 준 후보가 아니라 넣지 않았다.

글을 읽고 가른 결과:
- `test` 26건은 읽은 결과 PoC 안내 25건(그중 bakerfi H-06·H-07·M-04 3건은 같은 이름의 파일이 프로젝트에 더 있다)과 이름만 겹친 것 1건이다. 표의 겹침 4건은 이 3건과 아래 morph-l2 M-09 다. PoC 안내는 "이 파일에 아래 PoC 를 붙여 실행하라"는 글이다. 예: secondswap M-09 의 `test/VestingManager.test.ts`, loopfi M-01·M-02 의 `src/test/integration/PositionAction20.lever.t.sol`, bakerfi H-02·H-03·H-06·H-07·M-02·M-04·M-10 의 `test/core/` 아래 테스트 파일, virtuals-protocol H-05·M-04·M-21 의 `test/rewardsV2.js`·`test/bonding.js`, perennial H-04 의 `Market.test.ts`. 이름만 겹친 1건(morph-l2 M-09)은 정답이 다른 파일이다. 글의 `Tree.sol` 은 `MerkleTreeFull`·`_MAX_DEPOSIT_COUNT` 가 있는 `contracts/contracts/libraries/common/Tree.sol` 이고, `contracts/contracts/test/Tree.sol` 은 `TestTree` 라는 테스트 컨트랙트다.
- `scripts` 2건(virtuals-protocol H-05, M-21)은 글이 `test/rewardsV2.js` 라고 쓰는데 파일명이 `scripts/arguments/rewardsV2.js` 와 같을 뿐이다.
- 글의 `<폴더>/` 경로 토큰 35건은 전부 PoC 를 둘 위치, import 경로, diff 경로다. blackhole M-01 은 PoC 가 `contracts/mocks/*Mock.sol` 을 새로 만드는 글인데 그 파일은 체크아웃에 없다.
- 컨트랙트 이름 언급은 일반 단어("deposit", "strategy", "token" 등)나 다른 컨트랙트와 이름이 겹친 것뿐이었다. 예: secondswap M-04 의 `VestingDeployer` 는 `ignition/modules/VestingDeployer.ts` 가 아니라 `contracts/SecondSwap_VestingDeployer.sol` 이다.

스콥 문서(확인한 범위: Code4rena 디렉터리의 `scope.txt` 17개와 README 4개(bakerfi, cabal, iq-ai, liquid-ron). 그 밖 저장소는 미확인):
- Code4rena 17개 저장소의 `scope.txt` in-scope 461줄 중 제안 폴더 아래는 16줄이고 전부 mantra-dex 의 `contracts/*/tests/*.rs` 다. Rust 라 `EXT` 에 없어서 선택 대상이 아니다. 선택 대상 확장자(`.sol .ts .js .py .go`)는 0줄이다. `out_of_scope.txt` 564줄 중 160줄이 제안 폴더 아래다.
- bakerfi, cabal, iq-ai, liquid-ron 의 README 에는 이 폴더들의 경로 69개가 "Files out of scope" 표 아래에 있고 "Files in scope" 아래에는 0개다. 나머지 17개 언급은 `npx hardhat run scripts/deploy.ts` 같은 실행 명령 예시다.
- 위 두 항목이 본 전부다. 그 밖 저장소의 스콥 문서는 안 봤다. secondswap `scope.txt` 의 in-scope `.sol` 7개(`contracts/SecondSwap_*.sol`)는 모두 새 규칙에서 남는다.

결론: 정답 555건 중 제안 폴더 아래에 취약 위치를 둔 것은 0건이고, 확인한 스콥 문서(위 범위)도 이 폴더를 범위 밖으로 둔다. 요청한 7개를 전부 넣는다. 뺀 이름은 없다. `mocks` 는 94파일에 정답 0건이고 확인한 스콥 문서에서도 범위 밖이라 포함한다.

검증의 한계:
- 정답에 위치 필드가 없어서, 글이 파일이나 컨트랙트를 이름 없이 설명하는 경우는 못 잡는다. 이름이 나온 곳은 전부 읽었다.
- 이 29개 저장소 밖에서는 `scripts`·`deploy` 가 운영 코드일 수 있다. 29개 안에서 `script/` 의 `.sol` 중 `*.s.sol`·`*.t.sol` 이 아닌 4개는 lambowin 의 `script/quoter/*.sol` 3개(`scope.txt` 밖)와 tally 의 `script/DeployInput.sol`(배포 상수) 이다.

### 3. 규칙 (scan.py)

- `SKIP_DIRS` 에 `test tests ignition script scripts deploy mocks` 를 더했다. 비교 방식은 기존과 같다. root 기준 상대경로의 디렉터리 이름을 하나씩 보고, 깊이는 상관없다. `contracts/test/`, `src/test/integration/` 도 뺀다. 최상위 폴더만이 아니다.
- 안 보는 것: 파일 이름(`contracts/test.sol`), 루트 자체의 경로(`.../test/repo` 아래에서 스캔해도 상대경로만 본다. D-16 의 `lib` 와 같다), 소문자가 아닌 이름(`Test/`), 접두 일치(`testing/`). 29개 저장소에서 대문자 변형은 없었다(위 폴더 이름은 전부 소문자 한 가지로만 있었다). 파일 이름 패턴(`*.t.sol`, `*.test.ts`, `*_test.go`)은 안 본다.
- `--web` 과의 관계. `select_files` 는 `--web` 이면 `{.git, node_modules} | SKIP_DIRS_WEB` 을 쓰고 `SKIP_DIRS` 는 쓰지 않는다. 이 구조는 그대로라서 새 7개는 비웹에만 걸리고 `--web` 의 파일 집합은 바뀌지 않았다. 둘의 차이는 이렇다. 비웹은 `lib` 를 깊이와 상관없이 빼고(Foundry 의존성), `--web` 은 `foundry.toml` 옆 `lib/` 만 뺀다(D-16). `--web` 은 계속 `test` 류 폴더를 선택한다. 웹 대상은 정답셋이 없어서 위 검증을 못 한다. `--web` 에서도 빼려면 `SKIP_DIRS_WEB` 에 합치면 되고 그 전에 검증이 필요하다.
- CLI 어댑터는 `scan.select_files` 를 그대로 불러서 같은 규칙을 따른다. 어댑터 코드는 안 바꿨다.

### 4. 효과

secondswap 에 `select_files(web=False)` 를 돌린 결과다. 36개에서 16개로, 244,402 B 에서 89,009 B 로 줄었다. 남는 16개는 `contracts/` 의 `.sol` 15개와 `hardhat.config.ts` 다(`SecondSwap_Marketplace.sol` 25,225 B, `SecondSwap_StepVesting.sol` 11,920, `SecondSwap_MarketplaceSetting.sol` 11,280, `SecondSwap_VestingManager.sol` 10,249, `SecondSwap_VestingDeployer.sol` 9,898 등). 빠지는 20개는 `test/` 4개와 `ignition/` 16개다. 본 실행의 36개 중 새 선택에 없는 것은 이 20개뿐이고, 새 선택에만 있는 파일은 없다.

본 실행의 `_usage` 로 환산한 값이다(파일마다 본 실행 때의 토큰을 그대로 쓴다. 단가는 D-24 §6):

| | 파일 | 후보 | 입력 | 출력 | 캐시 읽기 | 비용(캐시 쓰기 제외) | 호출 시간 합 |
|---|---|---|---|---|---|---|---|
| 본 실행 전체 | 36 | 80 | 99,734 | 179,852 | 164,928 | $10.03 | 2,405.5초 |
| 빠지는 20개 | 20 | 44 | 61,013 | 88,520 | 103,080 | $5.06 | 1,198.9초 |
| 남는 16개 | 16 | 36 | 38,721 | 91,332 | 61,848 | $4.97 | 1,206.6초 |
| 빠지는 것의 비율 | 55.6% | 55.0% | 61.2% | 49.2% | 62.5% | 50.5% | 49.8% |

- 코디네이터의 "약 절반" 추정은 맞다. 출력 토큰 49.2%, 호출 시간 합 49.8%, 비용 50.5%(캐시 쓰기 제외 기준. 쓰기 $0.26 을 양쪽에 넣으면 $5.06 / $10.29 = 49.2%). 후보는 42/80 이 아니라 44/80 이다(§본 실행 결과).
- 빠지는 비용 $5.06 은 `ignition/` 16개 $2.05(출력 38,939 토큰)와 `test/` 4개 $3.01(출력 49,581 토큰)이다.
- 캐시 쓰기 4회($0.26)는 빠지는 파일이 아니라 남는 파일이 했다(`hardhat.config.ts`, `contracts/SecondSwap_Artifact_Testing_Purposes.sol`, `contracts/SecondSwap_Marketplace.sol`, `contracts/SecondSwap_MarketplaceSetting.sol`). 그래서 이 값은 안 줄고, 재실행 합계는 약 $5.2($4.97 + $0.26)다. 모델 출력은 호출마다 달라서 같은 토큰이 나온다는 가정 위의 추정이다.
- 시간. 4 워커라 호출 시간 합 1,206.6초를 4 로 나눈 약 302초와 남는 호출 중 최장 265.9초(`SecondSwap_Marketplace.sol`)가 하한이다. 본 실행의 프로젝트 시간 729.2초와 견주면 대략 5~6분으로 본다(추정, 실측 전).

데이터셋 29개 전체에 `select_files` 를 새 규칙과 D-16 시점 규칙으로 각각 돌려 비교했다:

```
projects=29  before=3,254  after=2,141  removed=1,113 (34.2%)
```

가장 많이 줄어든 곳은 blackhole 557 → 313(-244), perennial-v2 415 → 213(-202), fenix 231 → 129(-102), bakerfi 195 → 121(-74), idle-finance 277 → 205(-72) 다. `code4rena_cabal-liquid-staking-token_2025_05` 는 1개(`scripts/deploy.ts`)에서 0개가 된다. `coded-estate` 는 원래 0개다. cabal 은 정답 8건이 분모에서 빠져 재현율이 올라갈 수 있다(추정, 크기 미계산). 원인은 in-scope 파일 8개(`scope.txt`)가 전부 `.move` 라 스캐너 확장자에 원래 닿지 않는 것이다. 전에 고르던 1개(`scripts/deploy.ts`)는 범위 밖 파일이었다. 선택 파일이 0개면 scan.py 는 exit 1 이고 어댑터가 `crashed` 로 처리하지만(D-16) 실제 크래시는 아니다. 분모 정책은 사용자 결정 대기다(§7). (D-27 정정: 사용자가 `no_files` 로 분리하기로 결정했다. 0개인 프로젝트는 cabal 뿐이 아니라 coded-estate(정답 33건)도 있어서 합 41건이다.)

### 5. 지문 수정

원인. `AttackTrailAgent.fingerprints()` 는 manifest 를 쓰는 시점(`_score_and_finish` 의 `finally`, run 이 끝난 뒤)에 라이브 `scan.py` 를 읽어 해시했다. `snapshot()` 은 run 시작에 복사본을 만들지만 지문은 그 복사본을 보지 않았다. 36파일 run 은 약 12분 동안 돌았고 그사이 D-25 3차 수정이 scan.py 에 저장됐다. manifest 에는 라이브 해시 `4661b112…` 가 적혔고 실제로 시작한 코드는 스냅샷 `DBEFCC26…` 였다. D-14 의 지문은 실제로 돌아간 코드를 가리켜야 하는데 어긋났다.

수정(`adapters/attacktrail.py`):
- `snapshot()` 이 복사를 마친 직후(락 안) 복사본 경로와 그 파일의 SHA-256 을 에이전트에 저장한다(`_snap_scan`, `_snap_sha`).
- `fingerprints()` 의 `scan_py_sha256` 은 저장한 값을 쓴다. `prompt_sha256` 은 그 복사본을 로드해서 계산한다. 복사본 옆 `docs/` 사본이 scan.py 의 `STD_DIR` 로 풀려서 복사된 기준 문서를 읽는다. 레거시 `prompt_hash()` 도 복사본 바이트와 복사본 옆 `docs/` 로 계산한다. 정의는 그대로고 읽는 원본만 바뀌었다. 요청은 `scan_py_sha256` 이었지만 둘도 같은 결함(manifest 시점에 라이브 파일을 읽음)이라 같은 원본으로 맞췄다.
- `model()`·`effort()` 의 기본값은 초판에서 라이브 scan.py 소스에서 읽었다. 리뷰 후 수정 소절에서 스냅샷 기준으로 바꿨다. scan.py 가 `_run` 행에 model 과 effort 를 남기므로 사후에 대조할 수도 있다.
- 스냅샷이 없으면(`rescore`, 키 없이 중단된 run, 어댑터 직접 호출) 전처럼 라이브 파일을 해시한다.
- `--jobs` 로 한 run 의 프로젝트가 병렬이어도 값은 같다(같은 run dir 의 복사본은 바이트가 같아야 한다). `--repeat` 는 rep 마다 새 run dir 에 복사본을 만들고 그 값으로 바뀐다.
- 한계 하나. scan.py 프로세스는 복사본이 아니라 라이브 경로를 실행한다(`argv` 의 scan.py 는 `self.scan_py`). 보증은 "복사 시점의 라이브 파일과 같다"까지이고, 복사와 프로세스 시작 사이(밀리초)의 편집은 못 잡는다.
- `rescore`(같은 날 후속으로 고침). 초판은 `eval rescore` 가 에이전트를 새로 만들어(`get_agent`) 스냅샷을 모르고, 그래서 재채점 manifest 의 `scan_py_sha256`·`prompt_hash` 가 재채점 시점의 라이브 파일 기준이라고 적고 넘어갔다. 이제 `AttackTrailAgent.adopt_snapshot(run_dir)` 가 `run_dir/agent_snapshot/tools/scan.py` 와, 그 scan.py 의 `STD_BASE`(`--web` 이면 `STD_WEB` 도)가 이름 붙인 `docs/` 사본이 다 있으면 `snapshot()` 이 남기는 것과 같은 값(`_snap_scan`, `_snap_sha`, `snapshot_dir`)을 채우고 True 를 돌려준다. 하나라도 없으면 아무것도 안 바꾸고 False 라서 지문은 전처럼 라이브 파일 기준이다(사본 scan.py 가 파싱되지 않아 상수가 비어도 False 다. 리뷰 후 수정). 파일을 복사하거나 쓰지 않는다(`prompt_sha256` 계산용 import 가 사본 옆에 `__pycache__` 를 남길 수 있고 `snapshot()` 뒤에도 같다). `runner/run.py` 의 `rescore()` 는 `get_agent()` 직후 `adopt_snapshot` 이 있는 어댑터(baseline 은 없다)에만 `adopt_snapshot(run_dir)` 를 부른다. `fingerprints()` 에 `fingerprint_source`(`"snapshot"` 또는 `"live"`)가 새로 생겼고 `eval run` 의 manifest 에도 같이 들어간다(키 없이 중단돼 복사가 없는 run 은 `live`). 리포트 표(`report/render.py`)에는 안 올렸다. 레거시 `prompt_hash()` 는 기준 문서 이름을 읽는 scan.py(스냅샷이 있으면 사본)의 목록에서 정한다. 전에는 항상 라이브 scan.py 의 목록이라 사본과 라이브의 목록이 다르면 `<missing>` 으로 틀렸을 것이다. 확인: `pilot-secondswap-fable/agent_snapshot` 을 스크래치패드로 복사해 채택하면 `scan_py_sha256` 이 `DBEFCC2631B62843…`, `prompt_sha256` 앞 16자가 `2d0bb457ca2c605a`, `prompt_hash` 앞 8자가 `e7d11c6f` 다(그 manifest 의 `c968a8bf…` 는 라이브 바이트 기준 값이다). 그 폴더는 읽기만 했고 manifest 는 고치지 않았다. 남은 것: rescore manifest 에는 `agent.timeout_s` 가 여전히 없다. 원래 run 의 프로젝트 timeout 을 rescore 는 알 수 없고(`eval rescore` 에 `--timeout` 이 없다) 원래 manifest 를 읽어 복사하는 방식은 rescore 의 전제(manifest 없는 폴더)와 맞지 않아 안 했다. 시험: `test_adopt_snapshot_takes_the_copy_an_earlier_run_left_and_writes_nothing`(adapters), `test_rescore_writes_the_run_dirs_snapshot_hashes_into_the_manifest`(runner. 실제 `rescore()` 를 mock judge 와 제외 프로젝트 1개로 돌려 manifest 까지 본다. 사본이 없는 폴더는 `live` 로 나오는 대조 포함). `adopt_snapshot` 을 False 로 덮어쓰면 이 둘만 실패한다. adapters 56, runner 27, cli 25, 전체 315건 통과(이 후속 시점의 수. 리뷰 후 수정을 더한 최종 수는 아래 검증 기록). 실제 API 는 호출하지 않았다.
- 기존 run. `pilot-secondswap-fable` 의 manifest 는 고치지 않았다. 값은 위 "본 실행 결과" 끝 문단에 적었다.

### 6. 바뀐 것과 안 바뀐 것

| 항목 | 전 | 후 |
|---|---|---|
| 비웹 제외 디렉터리 | `.git node_modules lib` | 여기에 `test tests ignition script scripts deploy mocks` |
| `--web` 제외 디렉터리 | `.git node_modules` + `SKIP_DIRS_WEB` | 같음 |
| `prompt_sha256` | `2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f` | 같음(앞 16자 `2d0bb457ca2c605a`) |
| `call_shape` | API `api-v6`, CLI `cli-v1` | 같음 |
| `scan_py_sha256` (scan.py 파일) | `4661B1124E40246A1E0666E79E0CA703680F38BB716CE474F3E5FE292C1FF68D` | `550694799C084F114B7E1F4F13AD4CEF9E8531174621BDEADD518BCEDEA672BF` |
| 지문의 원본 | manifest 시점의 라이브 파일 | 스냅샷이 있으면 그 복사본, 없으면 라이브 파일 |
| 모델, effort, 시스템 프롬프트, `SCHEMA`, 후보 검증, 시간 설계, `max_tokens`, 캐시 TTL | | 같음 |

`call_shape` 를 올리지 않은 이유: 요청 본문과 호출 방식은 그대로이고 바뀐 것은 어떤 파일을 보내는지뿐이다. 그 차이는 `scan_py_sha256` 과 manifest 의 `files_total` 에 남는다. `prompt_sha256` 이 같아서 D-14 의 "프롬프트 동등성" 으로는 이전 run 과 같은 조건처럼 보이지만 파일 집합이 다르다.

### 7. 알려진 한계

- 이전 run(`pilot-secondswap-fable`)과 이후 run 은 파일 집합이 다르다(secondswap 36개 대 16개). `./eval compare` 는 이것을 경고하지 않는다. 비교 가능성 경고는 데이터셋 sha, 채점기 모델·커밋, batch size, 채점된 프로젝트 집합뿐이고 `files_total` 과 에이전트 설정은 안 본다(`report/compare.py`). 두 manifest 의 `projects[].files_total` 과 `agent` 를 사람이 대조한다. 이전 run 의 36파일 후보 중 `test/`·`ignition/` 에서 나온 것은 새 규칙에서 안 나오므로 truth 별 매칭을 비교할 때도 같은 이유를 본다.
- 파일 이름이 테스트·스크립트 모양(`*.t.sol`, `*.s.sol`, `*.test.ts`, `*.spec.ts`, `*_test.go`)인데 제외 폴더 밖에 있는 파일이 29개 저장소에 65개 남는다(morph-l2 22, boost-core 18, superposition 10, lambowin 8, crestal 5, idle-finance 2). 이름 패턴은 안 본다.
- `--web` 은 같은 폴더를 빼지 않는다(§3).
- 선택이 0개가 되는 프로젝트가 생겼다(cabal). 정답 8건이 분모에서 빠져 재현율이 올라갈 수 있다(추정, 크기 미계산). 원인은 in-scope 파일이 전부 `.move` 라 스캐너 확장자에 원래 닿지 않는 것이다. 0개 선택은 scan.py exit 1 → 어댑터 `crashed` 로 처리되며 실제 크래시는 아니다. 분모 정책은 사용자 결정 대기다. (D-27 정정: `no_files` 로 분리하기로 결정됐고 구현했다.)
- 검증은 29개 저장소와 정답 글의 이름 언급에 기댄다(§2 한계). holdout 이 생기면 다시 한다.
- `rescore` manifest 에 `agent.timeout_s` 가 없다(§5).

### 리뷰 후 수정 (2026-10-21)

D-26 구현 리뷰에서 남은 작은 결함 6건(코드 2, 문서 4)을 고쳤다. 코드는 `adapters/attacktrail.py` 만 바꿨다. scan.py, `runner/`, `report/`, `scoring/` 는 고치지 않았고 실제 API 는 호출하지 않았다. 수정 전 파일은 스크래치패드 `backup_20261021j/` 에 있다.

코드:
- `model()`·`effort()` 가 라이브 scan.py 의 `--model`/`--effort` 기본값을 읽었다(§5 초판). `snapshot()` 이 복사본 `agent_snapshot/tools/scan.py` 에서, `adopt_snapshot()` 이 사본에서 상수를 읽어 `_snap_consts` 에 저장하고, 스냅샷이 있으면 `model()`·`effort()` 는 그 값을 쓴다. 사용자가 `--model`/`--effort` 로 준 값이 있으면 그게 먼저다. 스냅샷이 없으면 전처럼 라이브 파일을 읽는다. manifest 의 `agent.model`, `agent.effort` 는 run 이 끝난 뒤 쓰이므로(`_score_and_finish` 의 `finally`) run 도중에 scan.py 기본값을 고쳐도 시작할 때의 값이 남는다.
- `adopt_snapshot()` 이 파싱되지 않는 scan.py 사본에도 True 를 돌려줬다. `_consts()` 가 `{}` 를 주면 확인할 기준 문서 이름이 없어서 `all([])` 이 True 가 됐다. 이제 `{}` 면 False 를 돌려주고 아무것도 안 바꾼다(지문은 라이브). 파싱은 되지만 `STD_BASE`, `STD_WEB`, `--model`, `--effort` 기본값이 하나도 없는 scan.py 도 `{}` 라 False 다.

문서:
- 선택 0개 프로젝트(cabal). §4, §7, 아래 "아직 안 된 것" 표의 행에서 "분모가 다르다" 를 "정답 8건이 분모에서 빠져 재현율이 올라갈 수 있다(추정, 크기 미계산)"로 바꿨다. 원인(in-scope 파일 8개가 전부 `.move`), `crashed` 가 실제 크래시가 아니라는 점, 분모 정책이 사용자 결정 대기라는 점을 같이 적었다. 근거 없던 "이전 run 에서는 ok 로 분모에 있었다" 문장은 지웠다. 정답 8건은 `curated-2025-08-18.json`, `.move` 8개는 cabal `scope.txt` 로 확인했다.
- 정답 분류(§2). 표의 `test` 행(경로 일치 9, 파일명 13, 겹침 4)은 일치 방식이고 본문(PoC 안내 25, 이름만 겹침 1)은 글을 읽은 결과라 기준이 달랐다. 표와 본문 둘 다에 두 기준을 적고, 겹침 4건이 bakerfi H-06·H-07·M-04 3건과 morph-l2 M-09 1건임을 이었다.
- 시험 건수(테스트 소절, 검증 기록). 313건, adapters 55, runner 26 은 rescore 후속 시험이 빠진 값이었다. 이 수정까지 더한 최종은 317건이다. 변형 확인의 "79건"은 당시 수라서 근거를 붙여 남겼다.
- 스콥 근거(§2). "스콥 문서도 이 폴더를 범위 밖으로 둔다" 를 확인한 범위로 한정했다. Code4rena 디렉터리의 `scope.txt` 17개와 README 4개(bakerfi, cabal, iq-ai, liquid-ron)만 봤고 그 밖 저장소는 미확인이다. secondswap `scope.txt` 의 in-scope `.sol` 7개는 모두 새 규칙에서 남는다.

알려진 한계: 이 수정을 넣기 전에 시작한 `eval run`(`pilot-secondswap-fable-d26`)과 `eval rescore`(`pilot-secondswap-fable-nm`)는 옛 어댑터를 이미 import 한 프로세스다. 그 manifest 의 `agent.model`, `agent.effort` 는 라이브 scan.py 기본값 기준이다. 기본값이 그사이 안 바뀌었다면 같은 값이다.

### 테스트 (실제 API 호출 없음)

- `scan.py --selftest`: 132건(128 + 4). 비웹이 7개 폴더를 어느 깊이에서든 빼는지, `--web` 은 안 빼는지, 루트 자체가 `test/` 아래여도 상대경로만 보는지, 대문자 `Test/` 는 안 빼는지(Windows 는 한 폴더에 `test/` 와 `Test/` 를 같이 못 만들어서 따로 시험). `python -O` 에서도 132건이 나온다.
- `tests/test_scan_select.py`(13건, +9): 건너뛰는 목록 고정(`scan.SKIP_DIRS`), 폴더마다 최상위·중간·하위 폴더 안의 파일을 빼고 `--web` 은 4개 다 선택하는 매개변수화 시험 7건, 소문자 정확 일치·상대경로 시험.
- `tests/test_adapters.py`(58건, D-26 에서 +4): `test_fingerprints_name_the_snapshot_not_a_scan_py_edited_during_the_run`. 스냅샷 뒤에 라이브 scan.py(ROLE 포함)와 기준 문서를 고쳐도 `scan_py_sha256`·`prompt_sha256`·`prompt_hash` 가 시작 값과 같고, 스냅샷 없는 새 에이전트는 라이브 값을 낸다. 나머지 셋은 `test_adopt_snapshot_takes_the_copy_an_earlier_run_left_and_writes_nothing`(§5 rescore 후속)과 리뷰 후 수정의 `test_model_and_effort_follow_the_snapshot_not_the_live_scan_py_defaults`, `test_adopt_snapshot_refuses_a_copy_whose_scan_py_does_not_parse` 다. 뒤의 둘은 수정 전 어댑터(백업본)를 끼워 돌리면 둘 다 실패하고(앞의 것은 `('model-v2', 'high') == ('model-v1', 'xhigh')`, 뒤의 것은 `adopt_snapshot` 이 True), 수정 후에는 통과한다.
- `tests/test_runner.py`(27건, D-26 에서 +2): `test_manifest_scan_py_sha256_is_the_snapshot_hash_when_scan_py_is_edited_mid_run`. 실제 `_single` 을 지나 manifest 까지 가고, 실행기 스텁이 도는 중에 scan.py 를 고친다. manifest 의 해시는 스냅샷 해시다. 나머지 하나는 §5 의 `test_rescore_writes_the_run_dirs_snapshot_hashes_into_the_manifest` 다.
- mutation 확인: 수정 전 동작(`_src()` 가 라이브 파일을 돌려주고 스냅샷 해시를 비움)을 스크래치패드 pytest 플러그인으로 클래스에 덧씌워 돌리면 위 두 시험만 실패하고 나머지 79건(그때 adapters 55 + runner 26 에서 둘을 뺀 수)은 통과했다. 그 뒤로 시험이 늘었고 이 변형은 다시 돌리지 않았다. repo 파일은 안 건드렸다.

검증 기록(2026-10-21, D-26 뒤. 건수는 리뷰 후 수정까지 더한 최종 값. adapters, runner, cli 는 이 수정 뒤 다시 돌렸고 나머지 파일의 건수는 직전 전체 실행의 값이다):

```
scan.py --selftest           selftest OK (132 checks)
python -O scan.py --selftest selftest OK (132 checks)
pytest                       317건 통과, 실패 0건 (파일별로 나눠 돌렸다)
                             adapters 58, attacktrail_cli 18, cli 25, report 24, runner 27,
                             scan_fake_api 73, scan_parse 73, scan_select 13, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
call_shape                   api-v6 (API), cli-v1 (CLI)
scan.py                      SHA256 550694799C084F114B7E1F4F13AD4CEF9E8531174621BDEADD518BCEDEA672BF
                             (D-26 전은 4661B1124E40246A1E0666E79E0CA703680F38BB716CE474F3E5FE292C1FF68D)
secondswap select_files      web=False 16개 (36개에서), web=True 36개
```

### 재채점과 재실행 결과 (실행 2026-10-21, 기록 2026-10-22)

이 소절은 이미 끝난 두 run 의 기록이다. 코드는 바꾸지 않았고 API 도 부르지 않았다. 숫자는 `eval/runs/` 의 manifest, scores.json, `raw/scan.jsonl` 에서 다시 계산해 요청서의 값과 맞췄다. 어긋난 곳은 아래에 따로 적었다. 두 run 의 시각은 manifest 의 UTC 를 KST(+9h)로 바꾼 값이다.

#### 1. nethermind 재채점 (`runs/pilot-secondswap-fable-nm`)

- 대상은 36파일 본 실행(`pilot-secondswap-fable`)의 사본이고 D-26 의 제외 규칙이 들어가기 전의 파일 집합이다. 명령은 `cli.py rescore runs\pilot-secondswap-fable-nm --agent attacktrail --dataset scabench --projects code4rena_secondswap_2025_02 --judge nethermind --model claude-fable-5-1 --effort xhigh` 이다. judge `o4-mini`, batch 10, iterations 3.
- manifest 기준 시작 19:32:58, 종료 19:53:09(20분 11초). 요청서의 시작 19:32:57 과 1초 어긋난다. 어느 시계에서 나온 값인지 확인하지 못했고 manifest 값을 적는다. exit 0 은 요청서 값이고 이 기록에서 다시 확인하지 못했다(manifest 에 `scoring_error` 가 없고 리포트가 만들어졌다).
- manifest `agent`: model `claude-fable-5-1`, effort `xhigh`, call_shape `api-v6`, `scan_py_sha256` `dbefcc2631b62843…`(스냅샷), `prompt_sha256` 앞 16자 `2d0bb457ca2c605a`, `fingerprint_source` `snapshot`. rescore 라 `timeout_s` 는 없다. OpenAI 비용은 측정하지 못했다(대시보드에서 확인할 것).

결과(프로젝트 1개라 `per_project` 와 `overall` 이 같다):

| 항목 | 값 |
|---|---|
| 정답(truth) | 30건 (high 3, medium 20, low 7) |
| actual_findings, scan_findings | 30, 30 |
| matched, partial | 9, 1 |
| false_positives, false_negatives | 21, 21 (엄격 기준) |
| precision, recall, f1 (엄격) | 0.3, 0.3, 0.3 |
| precision, recall, f1 (관대) | 각 0.333 |
| high_truths, high_matched, high_partial | 3, 1, 0 (high_recall 0.333) |
| truths_unevaluated, truth_mapping | 0, traced |

`scan_findings` 30 과 후보 79 의 관계. 합치거나 거르는 단계가 아니라 채점기의 계산식이다.
- 스캔 파일이 `{"project", "findings"}` dict 모양이면 채점기는 scan 파일을 읽는 분기를 안 타고(`generate_report.compute_repo_stats`, `scoring/nethermind.py` 독스트링 (c)) `scan_findings = matched + partial + fps` 로 둔다. 9 + 1 + 20 = 30. qa_findings 는 0 이라 Info 를 분모에서 빼는 일은 채점기의 이 단계에는 없다.
- 평가 항목은 50개다. 비-FP 30개(truth 하나에 하나)와 FP 20개. FP 20개는 truth 항목이 가리키지 않은 스캔 항목 중 Info/Best Practices 가 아닌 것이다(`core/evaluate.py` 의 FP 추가 루프. 가리킨 인덱스(`skip_indices`)와 Info/Best Practices 만 건너뛰고 중복을 합치지 않는다). 20개의 severity 는 low 14, medium 5, high 1 이다.
- 후보 79건의 severity(채점 입력 기준)는 info 50, low 17, medium 10, high 2 다. 맞은 10건(matched 9 + partial 1)은 서로 다른 스캔 항목 10개를 가리키고 그중 2개(index 3, 24)는 Info 다. Info 를 거르는 것은 FP 쪽뿐이라 truth 와 맞은 Info 는 맞은 것으로 센다.
- 79건의 행방: 30(맞은 10 + FP 20) + 49. 49 는 안 맞은 Info 48건(50 - 맞은 Info 2)과 medium 1건(index 0)이다. index 0 은 맞지 않은 truth 항목이 가리켰기 때문에 FP 도 아니고 맞은 것도 아니다.
- 그래서 precision 0.3 의 분모는 30 이다. 79 가 아니다(9/79 는 0.114). 관대 precision 0.333 은 10/30 이다. `false_positives` 21 은 엄격 기준(30 - 9)이라 partial 1건을 포함하고, 관대 기준 FP 는 20 이다. `scores.json` 의 `fp` 목록이 20건이고 `fn` 목록이 20건(관대 기준, partial 1건 제외)인 이유다.
- `info_findings` 50건은 우리 `evaluate` 가 스캔 항목 중 Info/Best Practices 로 정규화되는 것을 모은 목록이다(채점기 출력이 아니다). 위 info 50 과 같은 집합이고 그중 2건이 맞은 항목에 쓰였다.

해석에 붙일 단서:
- mock run 의 `TP 30 / FN 0 / FP 18`(36파일 본 실행, precision 0.625)은 판정이 호출 순서로 정해지는 배관 값이다(`scoring/mock_judge.py`). 실제 판정자(o4-mini)의 recall 은 0.30 이다. 둘은 비교 대상이 아니다.
- 이 0.30 은 Fable 5.1 xhigh 의 1회 실행을 1번 채점한 단일 표본이다. 프로젝트 1개, truth 30건이라 truth 하나가 3.3%p 이고 High truth 가 3건이라 High recall 은 33.3%p 단위로만 움직인다. 모델 출력과 LLM 판정이 둘 다 흔들리므로(채점기는 truth 하나에 판정 3회) 반복 없이 분산은 모른다.
- 후보 79건에는 `test/`·`ignition/` 에서 나온 43건이 들어 있다(D-25 본 실행 소절). D-26 규칙의 16파일 run 은 실제 판정자로 채점하지 않았다. 다음 단계는 `pilot-secondswap-fable-d26` 를 같은 방식으로 rescore 하는 것이고 OpenAI 비용이 든다.

#### 2. D-26 규칙 재실행 (`runs/pilot-secondswap-fable-d26`)

- `eval run --agent attacktrail --dataset scabench --projects code4rena_secondswap_2025_02 --judge mock --out runs\pilot-secondswap-fable-d26`. manifest 기준 시작 19:50:36, 종료 19:56:36(6분 0초), 프로젝트 wall 359.5초. 요청서의 19:50:35~19:56:38 과 1~2초 어긋나고 위와 같이 manifest 값을 적는다. exit 0 은 요청서 값이다. judge 가 mock 이라 TP·FP 수치는 배관용이다(scores.json 의 matched 25, partial 1 은 호출 순서가 정한 값이다).
- manifest `agent`: `scan_py_sha256` `550694799c084f11…`(D-26 의 새 해시), `fingerprint_source` `snapshot`, `timeout_s` 10800, `prompt_sha256` 앞 16자 `2d0bb457ca2c605a`.
- 16/16 파일 성공. `ERROR` 줄 0, 거절·폴백·partial 0(`_usage` 16행 전부 model `claude-fable-5-1`, `fallback` false, `partial` 없음, `files_fallback` 0), 커버리지 16/16.
- 후보 41(전부 `triage`, 위치검증 폐기 0). 파일별: Marketplace 10, MarketplaceSetting 8, StepVesting 5, VestingDeployer 5, VestingManager 4, Whitelist 3, USDT 3, TestToken 2, WhitelistDeployer 1. 나머지 7파일(`hardhat.config.ts`, `SecondSwap_Artifact_Testing_Purposes.sol`, `interface/` 5개)은 0건이다. `severity_guess` 는 Informational 24, Low 10, Medium 5, High 2 다.
- 토큰: input 38,721, output 94,809, cache_read 61,848, cache_creation 20,616(전부 5분). manifest `tokens_in` 121,185 는 앞 셋의 합(38,721 + 61,848 + 20,616)이다.
- 비용 $5.40(손계산: 토큰 × 공식 단가, 청구서 값이 아니다). 식은 D-25 의 Fable 5.1 단가($10/$50 per MTok, 5분 쓰기 $12.50, 읽기 $0.25)이고 항목은 입력 $0.387, 쓰기 $0.258, 읽기 $0.015, 출력 $4.740, 합계 $5.4008 이다. D-26 §4 의 추정 $5.2 보다 $0.20(3.9%) 많다. 출력이 같은 16파일의 본 실행값(91,332 토큰)보다 3.8% 많았다.
- 호출 시간(`_usage.call.elapsed_s`): 최대 284.3초(`SecondSwap_StepVesting.sol`. D-26 §4 가 최장으로 예상한 `SecondSwap_Marketplace.sol` 은 248.7초로 두 번째), 합 1,235.7초, 평균 77.2초, 중앙값 30.4초. 요청서의 중앙값 33.0 은 가운데 두 값(27.8초, 33.0초) 중 큰 쪽이고 `statistics.median` 은 둘의 평균 30.4 다. `max_gap_s` 최대 241.2초, `first_event_s` 최대 0.11초, 출력 최대 22,712 토큰.

36파일 본 실행과의 비교:

| | 36파일 본 실행 | 16파일 재실행 |
|---|---|---|
| 파일 | 36 | 16 |
| 후보(검토 대기) | 79 (JSONL 80행, 위치검증 폐기 1) | 41 |
| 같은 16개 계약 파일의 후보 | 36 | 41 |
| 비용(손계산, 캐시 쓰기 포함) | $10.29 | $5.40 |
| 시간(프로젝트 wall) | 729.2초(12분) | 359.5초(6분) |

16개 파일의 후보가 36에서 41로 늘었다. 같은 프롬프트와 설정에서 모델 출력이 호출마다 다른 만큼이고, 제외 규칙이 이 16개 파일에 영향을 주지는 않는다. 추정 누적 Anthropic 지출(손계산: 토큰 × 공식 단가, 청구서 값이 아니다)은 $4.01(선행 5파일) + $10.29(본 실행) + $5.40(재실행) = $19.70 / $100 이다. (D-28: 상한은 $200 으로 올랐고 이후 누적은 D-28 §8 에 이어진다.)

## D-27. 파일 0개 프로젝트를 no_files 상태로 분리하고 지원 밖 정답을 따로 센다 (2026-10-22)

사용자 결정(2026-10-21): "지원 언어 밖 상태로 분리". 시스템 프롬프트 본문, `SCHEMA`, `select_files`, 후보 검증, 호출 형태, 모델, effort, `max_tokens` 는 그대로고 scan.py 는 한 글자도 안 바꿨다. 실제 API 는 호출하지 않았다. 시험은 가짜 실행기, 가짜 scan 출력, 임시 폴더, mock 판정자로만 했다. 수정 전 파일은 스크래치패드 `backup_20261022a/` 에 있다.

### 1. 결정 배경

D-26 의 제외 규칙으로 `code4rena_cabal-liquid-staking-token_2025_05` 의 선택 파일이 1개(`scripts/deploy.ts`)에서 0개가 됐다. scan.py 는 0개면 클라이언트를 만들기 전에 exit 1 과 `스캔할 파일이 0개입니다` 로 끝나고 어댑터는 `crashed` 로 기록했다. 실제 크래시가 아니라 도구가 지원하는 확장자(`.sol .ts .js .py .go`)에 이 프로젝트의 소스가 없다는 한계인데, `crashed` 라서 정답이 분모에서 말없이 빠졌다.

요청서는 cabal 하나만 말했지만 체크아웃이 있는 29개 프로젝트에 `select_files` 를 다시 돌리면 0개인 프로젝트가 둘이다(2026-10-22 실측).

| 프로젝트 | 정답 | 이유 |
|---|---|---|
| `code4rena_cabal-liquid-staking-token_2025_05` | 8 | in-scope 파일 전부 `.move`(체크아웃에 `.move` 29개) |
| `code4rena_coded-estate-invitational_2024_12` | 33 | CosmWasm. `.rs` 15개, 선택 대상 확장자 파일 없음. D-26 전에도 0개였다 |

합 41건은 체크아웃이 있는 29개 프로젝트의 정답 524건의 7.8% 다(데이터셋 31개 555건에서 `pinned_commit_missing` 으로 빠진 initia-move 18, starknet-perpetual 13 을 뺀 값). 29개 전체를 돌리면 지원 밖 줄은 "프로젝트 2개, 정답 41건"이 된다. D-26 §4, §7 의 "cabal 정답 8건이 분모에서 빠진다"는 coded-estate 33건을 빠뜨린 서술이었다.

### 2. 정의

- 지원 밖 = `agent_status` 가 `no_files`: 스캐너가 보낼 파일을 하나도 못 고른 프로젝트.
- 헤드라인(Headline 표와 `scores.json` 의 `overall`)은 `ok` 로 채점된 프로젝트만의 값이다. 분모에 지원 밖 프로젝트의 정답은 없다. 이 정의는 바꾸지 않았다. `overall` 은 전과 같은 식으로 계산되고, 지원 밖 프로젝트를 더해도 값이 안 변한다(시험이 고정).
- 전체 포함 Recall(지원 밖 정답을 전부 미탐으로 넣은 값). 분자는 헤드라인과 같고 분모만 늘린다.
  - 엄격 `matched / (채점된 프로젝트의 truths_in_dataset + 지원 밖 정답)`
  - 관대 `(matched + partial) / 같은 분모`
- 지원 밖 정답 수는 데이터셋 항목의 `vulnerabilities` 길이다(채점기에 `truths_in_dataset` 으로 들어가는 수와 같다).
- `crashed`·`timeout`·체크아웃 실패는 전과 같이 분모에서 빠지고 정답 수도 따로 세지 않는다. `no_files` 만 센다.

### 3. scan.py 를 바꾸지 않은 이유 (D-14 지문)

요청서가 준 두 방법을 D-14 지문 영향으로 비교했다.

| | (a) scan.py 가 0개에서 exit 3 과 stdout 한 줄 | (b) 어댑터가 지금의 exit 1 과 메시지를 읽음 |
|---|---|---|
| `scan_py_sha256` | 바뀐다 (`5506…` 에서 새 값) | 그대로 `550694799c084f11…` |
| 레거시 `prompt_hash`(API) | 바뀐다(scan.py 바이트를 포함) | 그대로 `df7d8013fbd78658…` |
| `prompt_sha256`, `call_shape` | 그대로 | 그대로 |
| 옛 스냅샷 scan.py 로 돈 run 의 rescore | exit 1 도 읽어야 해서 어댑터가 두 방식을 다 알아야 한다 | 같은 방식으로 읽힌다 |
| 약점 | scan.py 와 어댑터 짝이 어긋나면 crashed 로 돌아간다 | 메시지 문구에 기댄다 |

(b)를 골랐다. D-26 에서 막 새 해시가 된 scan.py 를 또 바꾸면 파일럿 d26 run 의 기록(`550694799c…`)과 이후 run 이 갈리고 레거시 해시도 흔들린다. (b)의 약점은 시험으로 막는다. `test_real_scan_py_with_nothing_to_scan_is_no_files_through_the_real_adapter` 가 실제 scan.py 를 서브프로세스로 돌려(API 호출 없음, 키는 쓰이지 않는다) 어댑터가 `no_files` 를 내는지 본다. scan.py 의 문구나 종료 코드가 바뀌면 여기서 깨진다. 어댑터 쪽은 exit 1 과 문구가 둘 다 있을 때만 `no_files` 이고, 한쪽만 맞는 경우(다른 사유의 exit 1, 문구가 있는 exit 2·3, 빈 stderr)는 `crashed` 로 둔다(시험 4건).

CLI 어댑터는 `scan.select_files` 를 직접 부르므로 문구와 무관하게 선택 결과가 비면 같은 상태를 기록한다. 레거시 `prompt_hash`(CLI)는 `attacktrail_cli.py` 바이트를 포함하므로 이 수정으로 바뀐다(앞 16자 `2018be4fcd3fa0b0`). CLI run 은 `prompt_sha256` 로 비교한다(D-14).

`no_files` 가 기록하는 것: `Meta.status` `no_files`, `error` 에 사유(API: `no files selected: ` + scan.py 의 stderr 끝 500자, CLI: `no files selected under <경로>`), 커버리지 4개 전부 0(측정한 0이라 리포트 Coverage 열에 `0/0`), 토큰 None. API 어댑터는 키 확인이 먼저라 키가 없으면 전처럼 `crashed`(`ANTHROPIC_API_KEY not set`)다.

### 4. 바뀐 것과 안 바뀐 것

| 파일 | 변경 |
|---|---|
| `adapters/base.py` | `AGENT_STATUS` 에 `no_files`. `Agent.failed(..., **meta)` 로 추가 Meta 필드를 받는다 |
| `adapters/attacktrail.py` | `NO_FILES_MARK`, `NO_FILES_COVERAGE`. `_run` 에서 timeout 확인 뒤, 비정상 종료 확인 앞에 분기 |
| `adapters/attacktrail_cli.py` | `if not files` 가 `no_files` |
| `runner/run.py` | `_unsupported`, `_with_recall`. `_score_and_finish` 가 `unsupported` 를 scores.json 과 manifest 에 쓴다 |
| `runner/manifest.py` | `DENOMINATORS_NOTE` 에 no_files 문장 |
| `report/render.py` | 지원 밖 절, Status 문구와 색, `_excluded` 와 `run_summary` 에서 `no_files` 분리 |
| `report/compare.py` | `no_files` 집합 차이 경고 |

안 바뀐 것: scan.py(해시 `550694799c08…`), `prompt_sha256`, `call_shape`(API `api-v6`, CLI `cli-v1`), `scoring/`, 채점 입력(`ok` 프로젝트만 `scoring_data` 에 쓴다), `crashed`·`timeout` 의 분모 정책, `_aggregate`.

`unsupported` 블록(scores.json 과 manifest 에 같은 내용):

```
"unsupported": {
  "projects": [{"project_id": "...", "truths": 8}],
  "n_projects": 1, "truths": 8,
  "truths_in_dataset_incl_unsupported": 합,
  "recall_vs_dataset_incl_unsupported": 엄격, "recall_with_partial_vs_dataset_incl_unsupported": 관대,
  "line": "지원 밖 프로젝트 1개, 정답 8건. 이 정답을 전부 미탐으로 넣으면 Recall 8.3% (strict) / 8.3% (lenient). 분모는 채점된 프로젝트 정답과 지원 밖 정답의 합이고 체크아웃 실패·pinned 누락·crashed·timeout 정답은 제외한다. 헤드라인(지원 프로젝트만)은 25.0% (strict) / 25.0% (lenient)"
}
```

`line` 의 숫자는 시험용 가짜 에이전트와 mock 판정자의 값이라 예시일 뿐이다. 분모 구성 문장은 D-28 에서 더했다. 채점이 예외로 끝나면 manifest 에는 앞의 세 키(`projects`, `n_projects`, `truths`)만 있다. 채점된 프로젝트가 없으면 `overall` 은 `None`, 전체 포함 Recall 은 0 이고 `line` 의 헤드라인 자리는 `해당 없음` 이다. `eval rescore` 도 같은 경로(`_score_and_finish`)라서 findings.json 의 `no_files` 가 그대로 이어진다.

### 5. 리포트, 콘솔 요약, compare

- 리포트: Headline 바로 아래에 "Unsupported projects (지원 밖 프로젝트)" 절(설명, `line`, 프로젝트·정답 수·사유 표). 있을 때만 생긴다. 프로젝트별 표 Status 는 `no_files (지원 밖)`, HTML 은 주황 이탤릭(`td.nofiles`)이고 `crashed`·`timeout`·`build_failed` 는 빨강 굵게(`td.failed`)다. "Excluded / failed projects" 에는 `no_files` 를 올리지 않는다(그 절은 실패 목록이다). 프로젝트·사유는 manifest `projects[]` 에서, 정답 수와 `line` 은 `unsupported` 에서 읽고 블록이 없는 manifest 면 정답 수는 `미측정`, `line` 은 없다.
- 콘솔 요약(D-23): `crashed` 줄 목록에서 `no_files` 를 뺐다. 있으면 한 줄을 더한다: `지원 밖 프로젝트: <프로젝트> (정답 8)`(여럿이면 `, ` 로 잇는다). 없으면 줄 자체가 안 나온다(기존 요약과 시험 기대값이 그대로다).
- compare: 두 run 의 `no_files` 프로젝트 집합이 다르면 경고 한 줄(`no_files project sets differ (only A: ...; only B: ...)`). repeat run 은 `rep-k/manifest.json` 의 합집합이다(합본 manifest 의 `projects` 는 비어 있다). D-26 전 run 과 후 run 처럼 cabal 이 한쪽에서 `ok`, 다른 쪽에서 `no_files` 인 비교가 이 경고에 걸린다.

### 6. 시험 (실제 API 호출 없음)

- `tests/test_adapters.py`(63건, +5): 0개는 `no_files`(커버리지 0, 토큰 None, 사유에 문구), exit 1 이 아니거나 문구가 없으면 `crashed`(4건: 다른 사유의 exit 1, 문구와 exit 2, 빈 stderr, 문구와 exit 3).
- `tests/test_scan_select.py`(14건, +1): CLI 어댑터 0개가 `crashed` 에서 `no_files` 로(이름과 기대값 변경). 실제 scan.py 를 서브프로세스로 돌려 어댑터가 `no_files` 를 내는지 보는 시험(§3).
- `tests/test_runner.py`(30건, +3): 실제 `_single` 에 가짜 에이전트와 mock 판정자. ok 프로젝트(정답 4)와 no_files 프로젝트(정답 8)를 같이 돌려 `overall` 이 ok 프로젝트 단독 run 과 같고, `unsupported` 가 scores.json 과 manifest 에 같게 있고(`projects`, 정답 8, 분모 12, 두 Recall = matched/12 와 (matched+partial)/12, `line`), 끝 요약이 `지원 밖 프로젝트: p_nf (정답 8)` 이고 `crashed 프로젝트: 없음` 인 것. 모두 no_files 인 run 의 `overall None` 과 Recall 0. 실제 데이터셋으로 `rescore` 해서 cabal 의 정답이 8로 나오는 것.
- `tests/test_report.py`(29건, +5): 지원 밖 절과 Status 문구·HTML 클래스가 `crashed` 와 다르고 "Excluded / failed" 에는 `crashed` 만 있는 것, `no_files` 가 없으면 절이 없고 `unsupported` 블록이 없는 manifest 도 렌더되는 것, `run_summary` 의 줄 구성, compare 경고(다를 때만, 같으면 없음, repeat 의 rep manifest 합집합).
- mutation 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치패드 `mutate27.py`, 복구 뒤 해시가 변이 전과 같은 것을 스크립트가 단언): 24개를 모두 시험 실패로 잡았다. API 어댑터 `no_files` 를 `crashed` 로 되돌리기, 문구 바꾸기, exit 코드 검사 빼기, 문구 검사 빼기, 커버리지 0 빼기, CLI 어댑터를 `crashed` 로 되돌리기, CLI 커버리지 빼기, `AGENT_STATUS` 에서 `no_files` 빼기, 러너 `_unsupported` 가 다른 상태를 보기, 분모에 지원 밖 정답 안 더하기, `no_files` 를 채점 대상에 넣기, manifest·scores.json 에서 `unsupported` 빼기, 분모 설명 되돌리기, 요약 `crashed` 줄에 `no_files` 넣기, 지원 밖 요약 줄 빼기, "Excluded" 에 `no_files` 올리기, Status 문구 되돌리기, 지원 밖 절 제목 수준 바꾸기와 절 계산 끄기, 두 상태의 HTML 색 빼기, compare 경고 빼기, compare 가 rep 를 안 보기. 처음 23개 중 1개(절 제목 `##` 를 `###` 로)가 살아남았다. 시험의 `_section` 이 `###` 도 `##` 부분문자열로 읽었기 때문이다. 줄 시작 `\n## ` 를 확인하도록 고친 뒤 잡혔고, 24번째(절 계산 끄기)도 잡혔다.

검증 기록(2026-10-22. 파일별로 나눠 돌렸고 각 8분 안이다):

```
scan.py --selftest           selftest OK (132 checks)
python -O scan.py --selftest selftest OK (132 checks)
pytest                       331건 통과, 실패 0건
                             adapters 63, attacktrail_cli 18, cli 25, report 29, runner 30,
                             scan_fake_api 73, scan_parse 73, scan_select 14, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
call_shape                   api-v6 (API), cli-v1 (CLI)
scan.py                      SHA256 550694799C084F114B7E1F4F13AD4CEF9E8531174621BDEADD518BCEDEA672BF (D-26 과 같음, 변경 없음)
legacy prompt_hash (API)     df7d8013fbd78658… (pilot-secondswap-fable-d26 manifest 의 값과 같음)
select_files 가 0개인 프로젝트  cabal 8건, coded-estate 33건 (체크아웃이 있는 29개 중)
```

### 7. 알려진 한계

- `--repeat` 합본 폴더(`_aggregate`)의 manifest 는 `projects` 가 비어 있고 `unsupported` 를 옮기지 않아서 합본 리포트에는 지원 밖 절이 없다. 각 `rep-k/` 에는 있다.
- High Recall 의 전체 포함 값은 만들지 않았다. Recall(엄격·관대)만이다. 지원 밖 프로젝트의 High 정답 수는 기록하지 않는다.
- 이 정의는 `no_files` 에만 적용된다. 다른 이유로 분모에서 빠진 프로젝트(`crashed`, `timeout`, 체크아웃 실패)의 정답은 여전히 안 센다.
- D-26 전에 만든 run 에서 cabal 이 `crashed` 로 기록됐다면 그 manifest 는 안 바꿨다. 지금 `eval/runs/` 의 세 run 은 secondswap 하나뿐이라 해당이 없다. 그런 옛 run 과의 compare 는 `no_files` 집합 경고가 아니라 "scored project sets differ" 경고로 걸린다.
- `--web` 에서 파일이 전부 `MAX_BYTES_WEB` 를 넘어 선택이 비어도 `no_files` 가 된다(scan.py 의 0개 판정과 같다). D-28: scan.py 메시지에 `크기 상한 초과로 뺀 파일 N개` 를 더해 사유가 `크기 상한 초과` 로 구분된다.
- 어댑터가 scan.py 의 한국어 문구에 기댄다(§3). scan.py 의 문구나 종료 코드를 바꾸면 이 어댑터와 시험을 같이 고쳐야 한다. D-28 이 같은 메시지에 크기 상한 사유를 더했다(`tests/test_scan_select.py` 가 실제 scan.py 로 고정).
- 실제 `eval run` 으로 29개 전체를 돌려 지원 밖 줄을 확인하지는 않았다(API 호출 금지). 리포트와 요약은 가짜 에이전트와 실제 데이터셋 `rescore` 시험으로만 봤다.

## D-28. effort max 와 --max-tokens, 시간 상수의 비례 계산, D-27 리뷰 잔여 결함, 모델 비교 결과 (2026-10-22)

사용자 결정(2026-10-22): "가장 강한 모델로 가장 높은 effort". 다음 실행은 Fable 5.1 effort `max` 3회, Opus 5.5 `max` 1회, 계약 16파일(secondswap, D-26 규칙), `max_tokens` 128000, nethermind 채점이다. 비용 상한은 $100 에서 $200 으로 올렸고 Console 지출 한도도 $200 으로 권고한다. 이 작업은 실제 API 를 호출하지 않았다. 시험은 127.0.0.1 가짜 서버, 가짜 실행기, mock 판정자, 임시 폴더로만 했다. ROLE, 기준 문서, 사용자 프롬프트 틀, `SCHEMA`, `select_files` 의 제외 규칙, 후보 검증, 폴백, 캐시 TTL 은 그대로다. 수정 전 파일은 저장소 밖 임시 폴더 `C:\Users\정동욱\AppData\Local\Temp\at_backup_20261022b\` 에 있다. `eval/runs/` 아래는 읽기만 했고, §8 의 수치는 그 파일들(manifest, scores.json, `raw/scan.jsonl`)로 다시 계산했다. 코디네이터가 보낸 값과 어긋난 곳은 없었다(시각 하나만 다르다. §8).

### 1. 결정과 반영 위치 (A)

| 결정 | 반영 |
|---|---|
| effort `max` 허용 | scan.py `EFFORTS = ("high", "xhigh", "max")`. `eval run --effort`, `eval rescore --effort` 의 선택지(`cli.py EFFORTS`)도 같다. 기본값은 `xhigh` 그대로 |
| `--max-tokens` 옵션 | scan.py `--max-tokens`(정수, 기본 `MAX_TOKENS` 64000, 허용 1~`MAX_TOKENS_LIMIT` 128000. 범위 밖과 정수가 아닌 값은 argparse 오류 exit 2, 요청 전에 막힌다). `eval run --max-tokens`, `eval rescore --max-tokens`(manifest 기록용). `AttackTrailAgent(max_tokens=)` 가 scan.py `--max-tokens` 로 넘긴다 |
| 호출별 시간 상수 | scan.py `call_cap(max_tokens)`(무응답 상한), 어댑터 `max_call_s(max_tokens)`. §5 |
| manifest 와 리포트 | manifest `agent.max_tokens`(API 어댑터는 정수, CLI 어댑터는 `n/a`, scan.py 에 상수가 없으면 `unknown`). 리포트 Manifest 표에 `max_tokens (요청 출력 상한)` 한 행(manifest 에 있을 때만) |
| CLI 어댑터 | `--max-tokens` 거부. `eval run --agent attacktrail-cli --max-tokens` 는 SystemExit, `AttackTrailCLIAgent(max_tokens=)` 는 `ValueError`(effort 와 같은 규칙. `claude -p` 에는 `--model` 만 간다) |

문서로 확인한 사실(번들 claude-api 스킬 본문, 2026-10-22 호출. 경로는 `C:\Users\정동욱\AppData\Local\Temp\claude\bundled-skills\2.1.307\0a24d6ef153d96ca65aed6f63c41126d\claude-api` 이고 세션 임시 디렉터리라 사라질 수 있다):
- effort `max`: Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5 모두 `low`~`max` 를 지원한다. 기본값은 Opus 5.5 와 Haiku 5.5 가 `medium`, Sonnet 5.5 가 `high` 다. 우리는 effort 를 항상 명시해서 보낸다.
- 출력 상한: Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5 모두 `max_tokens` 128K 까지 지원하고, SDK 는 그런 큰 값에 스트리밍을 요구한다. 우리 호출은 D-24 부터 스트리밍이라 SDK 의 비스트리밍 가드는 걸리지 않는다. `/v1/models` 로 직접 본 것은 Fable 5.1 의 `max_tokens` 128000 뿐이다(D-25 §1, 코디네이터 실측). Opus 5.5 의 128K 는 문서 서술이고 `/v1/models` 로는 안 봤다.
- Sonnet 5.5 의 `thinking: {type: "between_tools"}` 는 xhigh·max 에서 400 이다. 이 스캐너는 `thinking` 을 보내지 않아 해당 없다. Opus 5.5 와 Fable 5.1 은 thinking 을 끌 수 없고 effort 가 유일한 조절 수단이다.
- 같은 문서가 xhigh·max 에서 긴 산출물의 출력 토큰이 약 2배가 될 수 있다고 쓴다(D-24 §2 가 인용한 1771행과 같은 내용). §6 비용 추정의 근거다.

### 2. 코드 변경

| 파일 | 변경 |
|---|---|
| `internal/tools/scan.py` | `EFFORTS` 에 `max`. `--max-tokens`, `MAX_TOKENS_LIMIT`, `BASE_MAX_TOKENS`, `call_cap()`. `call_limits(rem, cap=None)`. `_run` 행의 `max_tokens`·`call_timeout_s` 가 옵션 값을 반영. 스트림 호출의 `max_tokens=a.max_tokens`. `--web` 에서 전부 크기 상한에 걸려 0개일 때 종료 메시지에 `크기 상한 초과로 뺀 파일 N개` 추가(§7 B4). selftest 검사 6건 추가(132 에서 138) |
| `eval/adapters/attacktrail.py` | `max_tokens=` 인자와 검증(1~128000 정수, bool 거부), `max_tokens()`, `fingerprints()["max_tokens"]`, argv `--max-tokens`(값을 줬을 때만), `_scan_constants` 가 `MAX_TOKENS` 를 읽음, `max_call_s()`, 시간 계산이 `max_tokens` 를 따름, `TOO_BIG_RE`·`too_big_reason()` |
| `eval/adapters/attacktrail_cli.py` | `max_tokens` 거부(`ValueError`), `max_tokens()` 가 `n/a`, `--web` 크기 상한 사유 |
| `eval/cli.py` | `EFFORTS` 에 `max`, `MAX_TOKENS_LIMIT`, `_max_tokens`(argparse 타입), `run`·`rescore` 의 `--max-tokens`, `_agent_opts` 의 규칙 |
| `eval/runner/run.py` | `unsupported.line` 문구(B1), `rescore()` 끝의 `_print_summary`(B3), `judge_incomplete`(§9) |
| `eval/report/render.py` | Manifest 행, 지원 밖 절 문구(B1), `_td_class` 독스트링(B5), light 모드 `--amber`(B7), 채점 불완전 줄(§9) |

`_run` 행과 `_usage` 행의 값 확인. `_run` 행의 `max_tokens` 는 `a.max_tokens`, `call_timeout_s` 는 `call_cap(a.max_tokens)` 다(가짜 서버 시험이 128000 에서 `max_tokens` 128000, `call_timeout_s` 3600 을, 기본 실행에서 64000 과 1800 을 고정한다). `_usage` 행에는 처음부터 `max_tokens` 키가 없다. 그 호출에 준 무응답 상한은 `_usage.call.timeout_s` 이고 이 값이 `call_cap` 을 반영한다(128000 에서 3600.0, 시험이 SDK 가 실제로 보낸 `x-stainless-read-timeout` 헤더와 같은 값임을 본다).

### 3. 지문 (D-14)

- `prompt_sha256`: 불변. `2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f`(앞 16자 `2d0bb457ca2c605a`). ROLE 과 기준 문서를 안 건드렸다.
- `scan_py_sha256`: 바뀐다. 전 `550694799C084F114B7E1F4F13AD4CEF9E8531174621BDEADD518BCEDEA672BF`(D-26, D-27), 후 `39C5A86388279B0367BAC62747213B0DF04D56601BEA5DF419513D0F7BA45376`.
- 레거시 `prompt_hash`(scan.py 바이트 포함): API 어댑터와 CLI 어댑터 모두 바뀐다. 새 값의 앞 16자는 API `3b9f9f3d13872259`, CLI `67513d5745174f99` 다. CLI 의 값은 `attacktrail_cli.py` 바이트도 포함한다. 비교는 `prompt_sha256` 로 한다.
- `call_shape`: API `api-v6`, CLI `cli-v1` 그대로다. 근거: 요청 본문의 형태가 같다. 엔드포인트(`POST /v1/messages?beta=true`), `anthropic-beta` 헤더, `betas`, `fallbacks`, system 블록 둘과 캐시 TTL, `output_config.format`, `stream` 이 그대로이고, 바뀌는 것은 값 둘(`output_config.effort` 의 `max`, `max_tokens`)과 호출별 read timeout 헤더 값(1800 에서 3600)뿐이다. 규칙은 이렇다. 요청 본문의 구조(엔드포인트, 블록 구성, 파라미터 유무)가 바뀌면 `call_shape` 를 올리고, 실행 옵션으로 넘기는 값(model, effort, max_tokens)은 manifest 필드(`agent.model`, `agent.effort`, `agent.max_tokens`)에 기록한다. D-25 가 `CALL_TIMEOUT_S` 를 600 에서 1800 으로 올릴 때도 올리지 않았다. 초판은 이 자리에 "effort 는 D-23 부터 값이 바뀌는 인자였고 그때도 올리지 않았다" 고 썼는데 틀렸다. D-23 은 베타 엔드포인트, 서버 측 폴백, 캐시 TTL 을 바꿔서 `api-v5` 로 올렸다(D-23 §5). effort 값 때문에 안 올린 사례가 아니다. `max_tokens` 도 상수였을 때는 바뀔 때마다 올렸다(D-17 은 8000 에서 16000 과 응답 읽기 변경으로 `api-v2`, D-24 는 16000 에서 64000 과 스트리밍 전환으로 `api-v6`). 지금은 `max_tokens` 가 실행마다 다른 옵션이라 상수 하나로 구분할 수 없어서 규칙에서 뺐다. 코디네이터가 `api-v6` 유지를 정했다(2026-10-22). 한계는 max 128000 run 과 xhigh 64000 run 이 같은 `call_shape` 를 기록한다는 점이고, 구분은 manifest `agent.max_tokens` 로 한다. attacktrail.py 309~311행 주석 수정과 `compare` 경고 추가는 같은 날 뒤따른 작업에서 했다(아래 '코드 수정 후속' 2).
- 이 구현 시점의 `compare` 는 `effort`, `max_tokens`, `model` 이 달라도 경고하지 않았다(D-26 의 "아직 안 된 것" 행 그대로). xhigh 64000 run 과 max 128000 run 을 비교할 때 두 manifest 의 `agent` 를 사람이 대조해야 했다. 경고는 같은 날 뒤따른 작업에서 더했다(아래 '코드 수정 후속' 2).

### 4. 시간 계산에 쓴 실측과 가정

실측: 출력 속도 74~77 tokens/s(D-25 선행 5파일과 본 실행. 60초 넘은 11호출의 중앙값 73.5), 최대 출력 24,912 토큰(§8 의 Fable rep-1), 호출 최장 329.9초, `max_gap_s` 최대 278.6초(D-25 본 실행). 가정: D-24 의 36 tokens/s(64000 토큰에 약 1,778초).

### 5. 시간 설계 재검토 (A4)

128000 토큰을 전부 쓰는 최악 호출의 길이:

| 속도 | 계산 | 길이 |
|---|---|---|
| 74 tokens/s(실측) | 128000 ÷ 74 | 1,730초(1,729.7) |
| 36 tokens/s(D-24 가정) | 128000 ÷ 36 | 3,556초(3,555.6) |

현재 `MAX_CALL_S` 와 `CALL_TIMEOUT_S` 1800 은 실측 속도에서는 1,730초라 아슬아슬하게 닿고, 가정 속도에서는 3,556초라 넘는다. 세 가지를 비교했다.

| 방법 | 128000 에서 | 64000 이하의 동작 | 바뀌는 것 |
|---|---|---|---|
| (a) 비례: `ceil(1800 × max(max_tokens, 64000) / 64000)` | 3600 | 전과 같다(`call_cap(64000)` 이 1800) | 함수 하나씩(scan.py, 어댑터) |
| (b) 상수 둘을 3600 으로 | 3600 | 소프트 여유가 1815 에서 3615 로 늘어 소프트 한도가 1800초 앞당겨지고, 짧은 타임아웃 경고 기준이 3630 에서 7230 으로 바뀐다 | 상수 둘, 그리고 D-24·D-25 의 수치와 시험 기대값 다수(10800 이면 8985 가 7185 로 등) |
| (c) `max(1800, ceil(max_tokens / 36) + 여유)` | 3556 + 여유 | 64000 에서 1778 + 여유라 여유가 22초를 넘으면 1800 이 깨진다 | 위 (a) 와 같은 크기의 함수 하나씩, 64000 의 값은 여유에 따라 흔들림 |

(a)를 골랐다. 1800 은 이미 64000 토큰을 약 35.6 tokens/s 로 쓰는 경우(1,778초)에 22초 여유를 얹은 값이라, 비례 계산은 같은 속도 가정을 128000 까지 늘린 것이다(3600 ≥ 3,556). 64000 이하는 한 바이트도 안 바뀌어서 D-24~D-27 의 모든 수치(10800 에서 소프트 8985, 하드 10785 등)와 시험이 그대로 맞는다. (b)와 128000 의 값은 같다. 결과(128000): 무응답 상한 3600, `MAX_CALL_S` 3600, 소프트 여유 15 + 3600 = 3615, 짧은 타임아웃 경고 기준 2 × 3615 = 7230초. `eval` 어댑터와 scan.py 의 두 식은 손으로 맞추고 시험(`test_adapter_max_call_s_is_scan_pys_call_cap`)이 일치를 고정한다.

한 가지 짚어 둔다. `call_limits` 는 남은 시간이 `3 × cap + 16` 이상일 때만 시도당 timeout 을 cap 으로 준다. cap 3600 이면 10,816초다. 기본 하드 데드라인 10785초에서는 시작 직후에도 (10785 - 16) ÷ 3 = 3,589.7초가 되어 3600 보다 조금 작고, 남은 시간이 줄수록 같이 줄어든다(남은 5000초에서 1,661초). D-25 와 같은 구조다. 이 값은 총시간이 아니라 이벤트 사이 무응답의 상한이고, 실측에서 가장 긴 무이벤트 구간은 호출 길이의 66~82% 였다. 74 tokens/s 의 128000 토큰 호출은 1,730초이므로 무이벤트 구간이 그 82% 여도 약 1,420초다. 남은 시간이 5,000초 아래로 내려가는 것은 시작 후 약 5,785초부터이고, 실측 속도의 이 run 은 그 전에 끝난다.

API 어댑터의 소프트 여유 `MAX_CALL_S + 15` 와 기본 timeout 10800 은 이렇게 맞다.
- 소프트 여유는 `DEADLINE_MARGIN_S + max_call_s(max_tokens)` 로 계산한다. 128000 이면 3615 이고, 소프트 한도 직전에 시작한 호출이 최악 길이(3600)여도 하드 데드라인 안에 끝난다는 성질이 그대로 선다(하네스 타임아웃 7230초 이상일 때. 아래면 경고 한 줄, 실행은 안 막는다).
- 기본 timeout 10800 에서 소프트 한도는 10800 - 3615 = 7185초, 하드 한도는 10785초다.
- 16파일, 워커 4, 4라운드에서 모든 호출이 128000 토큰을 다 쓰는 최악은 속도에 따라 갈린다. 36 tokens/s 면 호출당 3,556초라 2라운드가 7,111초에 끝나 소프트 한도(7,185초)보다 이르다. 3라운드까지 돌고(10,667초에 끝난다) 건너뛰는 것은 4라운드 파일 4개다. 호출당 3,600초(약 35.6 tokens/s 이하)면 2라운드가 7,200초에 끝나 15초 늦어서 3라운드가 시작하지 않고 파일 8개가 건너뛰어진다. 어느 쪽이든 `skipped: time budget reached` 로 남고 커버리지(`12/16` 또는 `8/16`)와 콘솔 요약의 부분 실패에 보인다. 조용히 사라지지는 않는다. 초판은 36 tokens/s 에서 파일 8개라고 적었는데 이 경우는 4개다.
- 실측 속도(74 tokens/s)로는 4라운드가 4 × 1,730 = 6,920초가 필요하고 3라운드 시작이 5,190초라 기본 소프트 한도 안이다. 지금까지 최대 출력은 24,912 토큰이고(§8) max 에서 2배로 잡아도 약 50,000 토큰이라 128000 의 39% 다. 에이전트 단계는 xhigh 3회의 프로젝트 wall(299~362초)의 2배인 12분 안쪽으로 본다(추정). 채점은 따로 약 11~12분이 걸렸다(D-26 재채점 run 이 10:58~11:10).

결정: 기본 timeout 10800 은 올리지 않는다. 올리면 모든 API run 의 기본이 바뀌고(`--timeout` 을 안 주는 run 은 전부), 위 최악은 실측 속도의 절반 이하 속도와 모든 파일의 출력 상한 소진이 동시에 일어나야 하는 경우다(그때 Fable 1회 비용은 약 $103, §6). 한계는 여기 적는다. 그 최악까지 덮으려면 `eval run --timeout 14500` 을 준다(소프트 14500 - 3615 = 10885 라 4라운드가 호출당 3,600초면 10,800초에 시작해 14,400초에 끝나고(36 tokens/s 면 10,667초에 시작해 14,222초에 끝난다), 하드 14,485초 안이다). 시험(`test_default_project_timeout_stays_10800_whatever_max_tokens_is`)이 이 산수를 고정한다.

### 6. 비용 추정 (A5)

모든 값은 손계산(토큰 × 공식 단가)이고 청구서 값이 아니다. 단가는 Fable 5.1 입력 $10, 출력 $50, 캐시 읽기 $0.25, 5분 쓰기 $12.50(per MTok), Opus 5.5 는 $4, $20, $0.20, $5 다.

| 항목 | 추정 | 근거 |
|---|---|---|
| Fable max 1회(16파일), 보통 | $10~15 | 사용자 추정. xhigh 3회 평균 $5.42(§8)의 약 2배에서 3배. 출력이 비용의 대부분이다(d26 run 은 $5.40 중 출력 $4.74) |
| Opus max 1회, 보통 | $3~5 | 사용자 추정. xhigh 1회 $1.32 의 약 2배에서 4배 |
| 4회 합, 보통 | $33~50 | Fable 1회 $10~15 × 3 + Opus $3~5(3 × 10 + 3 = 33, 3 × 15 + 5 = 50). 초판의 약 $35 는 xhigh 평균의 2배 가정(3 × ($5.42 × 2) + $1.32 × 2 = $35.2)이라 위 두 행의 범위와 맞지 않았다 |
| 누적, 보통 | $71~88 | §8 의 추정 누적 $37.74 + $33~50(처음 $37.75. D-29 리뷰 후 수정) |
| Fable max 1회, 최악 | $103 | 16파일이 전부 128000 토큰을 쓰는 경우. 출력 16 × 128,000 × $50/M = $102.4 에 입력과 캐시 $0.66 을 더한 $103.1. 입력과 캐시는 실측 xhigh 16파일(입력 38,721, 캐시 쓰기 20,616, 캐시 읽기 61,848 토큰)에 Fable 단가를 곱한 값이다. 초판의 "1회에 $0.5 안쪽" 은 틀렸다 |
| Opus max 1회, 최악 | $41 | 16 × 128,000 × $20/M = $40.96 |
| 4회 전부 최악 | 약 $350 | 3 × $103.06 + $41.26(입력·캐시 포함). 입력·캐시를 빼면 3 × $102.4 + $41.0 = 약 $348 이고 처음 이 줄은 그 값이었다(D-29 리뷰 후 수정) |

비용 상한은 사용자 결정으로 $200 이다(2026-10-22, 이전 $100). Console 워크스페이스 지출 한도도 $200 으로 권고한다. 한 번의 최악($103)은 상한 안이지만 네 번이 전부 최악이면 $350 으로 상한을 넘는다. 도구에는 비용 차단기가 없고 시간 한도도 비용을 막지 못한다. 끊긴 스트림의 `output_tokens` 는 하한이라 `tokens_out` 으로 센 지출은 실제보다 작을 수 있다(D-24 §3, §6). 진짜 상한은 Console 의 지출 한도다. 실행 사이마다 추정 누적을 손으로 다시 계산한다. 현재 추정 누적은 $37.74 라 상한까지 남은 몫은 $162.26 다(§8 의 Sonnet 정정을 반영한 값이고 처음에는 $37.75 와 $162.25 였다).

### 7. D-27 리뷰 잔여 결함 (B1~B7)

| # | 결함 | 처리 |
|---|---|---|
| B1 | `unsupported.line` 에 분모 구성이 없다 | `line` 에 "분모는 채점된 프로젝트 정답과 지원 밖 정답의 합이고 체크아웃 실패·pinned 누락·crashed·timeout 정답은 제외한다" 를 더했다. 리포트 지원 밖 절의 설명 문장에도 같은 내용을 더했고, "하나도 찾지 못한"을 "하나도 보내지 못한"으로 바꿔 `--web` 크기 상한 사례를 포함했다. README 지표 정의에도 한 문장. 시험(`test_runner.py` 의 D-27 시험, `test_report.py`)의 문자열 단언을 갱신 |
| B2 | 이 문서의 "compare 행은 파일 집합 차이를 덧붙였다" 는 틀렸다(파일 집합 차이 경고는 없다) | "compare 행은 no_files 집합 경고를 덧붙였다(파일 집합 차이 경고는 없다)" 로 고쳤다 |
| B3 | `rescore()` 가 `eval run` 과 달리 요약을 안 낸다 | `rescore()` 끝에서 `_print_summary([run_dir])`. stderr 에만 내고 stdout 은 cli.py 가 찍는 경로 한 줄 그대로(시험 `test_rescore_prints_the_run_summary_on_stderr_and_keeps_stdout_empty`) |
| B4 | `--web` 에서 모든 파일이 `MAX_BYTES_WEB` 를 넘어 0개가 되면 "지원하는 확장자가 없음"과 구분이 안 된다 | scan.py 의 0개 종료 메시지에 `크기 상한 초과로 뺀 파일 N개: 파일당 MAX_BYTES_WEB 100000바이트 초과` 를 더하고(N 이 0 이면 전과 같은 문구), 어댑터가 그 문구를 읽어 no_files 사유를 `no files selected (크기 상한 초과: --web 후보 파일 N개가 모두 MAX_BYTES_WEB 를 넘어 보내지 않음): ...` 로 쓴다. CLI 어댑터는 `select_files` 의 `too_big` 에서 같은 문구를 만든다 |
| B5 | `_td_class` 독스트링이 "Status 셀만"이라 하나 실제로는 모든 표의 모든 셀에 적용된다 | 독스트링을 실제 동작에 맞췄다(코드는 그대로. 더 작은 쪽) |
| B6 | 비용 표기가 손계산인지 추정 누적인지 불명확 | 이 문서의 해당 줄(D-25 본 실행 누적 $14.30, D-26 의 $5.40 줄, 비교 표, 누적 $19.70 줄, "아직 안 된 것" 표의 비용 차단기 행)에 "손계산(토큰 × 공식 단가)"과 "추정 누적"을 적었다 |
| B7 | light 모드 `--amber` 가 갈색에 가깝다(선택 사항) | `#8a5a00` 에서 `#a85d00` 으로 바꿨다. 더 주황에 가깝고 WCAG 대비는 흰 배경 4.96:1, 표 짝수 행 배경(#f6f6f6)에서 4.59:1 로 본문 기준 4.5:1 이상이다(이전 5.93:1, 5.48:1). dark 모드는 그대로 |

B4 의 방법 선택. 요청이 준 두 안 중 scan.py 메시지에 수를 넣는 쪽을 골랐다. `load_scan_module` 로 `select_files` 를 한 번 더 부르는 안은 어댑터가 대상 저장소를 다시 훑어야 하고 코드가 더 길다. D-27 §3 은 scan.py 의 해시를 지키려고 메시지를 안 바꿨는데, 이번에는 (A)로 scan.py 가 어차피 바뀌어서 그 이유가 없다. 문구에 기대는 약점은 그대로이고 시험(`test_scan_select.py` 가 실제 scan.py 를 서브프로세스로 돌린다. API 호출 없음)이 고정한다. CLI 어댑터 경로는 `--web` 시스템 프롬프트가 Windows 명령줄 한도를 넘어(D-15) 실제로는 거기 닿지 않는다. 코드는 대칭을 위해 두었고 시험은 argv 크기 검사를 대체해서 거기까지 보낸다. D-27 §7 의 해당 한계 줄에 "사유가 구분된다"를 덧붙였다.

### 8. 결과 기록 (C)

배치 중단 사건(코디네이터 보고. 이 기록에서 다시 확인하지 못했다): 1차 배치는 PowerShell `$args` 자동 변수 충돌로 프로세스 시작 전에 실패했다(API 호출 없음). 2차 배치는 Claude Code 가 시스템 메모리 부족(여유 1.2GB / 16GB)으로 셸을 중단해서 Sonnet 채점이 끊겼고, `eval rescore` 로 마쳤다. 3차 배치는 정상 종료했다.

모든 run 은 secondswap 한 프로젝트, D-26 규칙의 16파일(스캔 16, 실패 0, 건너뜀 0, 폴백 0, partial 0), scan.py `550694799c084f11…`, 스냅샷 지문(`fingerprint_source` snapshot), 채점은 nethermind(o4-mini, batch 10)다. 시각은 manifest 의 UTC 에 9시간을 더한 KST 다. 정답은 30건(high 3). 수치는 `scores.json` 과 `raw/scan.jsonl` 에서 다시 계산했고 비용은 토큰 × 공식 단가의 손계산이다(Haiku 5.5 단가는 공식 가격표 https://platform.claude.com/docs/en/about-claude/pricing 의 100K 토큰 이하 구간 값이다. 입력 $0.10, 5분 캐시 쓰기 $0.125, 1시간 쓰기 $0.20, 캐시 읽기 $0.01, 출력 $0.50 per MTok 이고 이 값으로 $0.045 가 나온다(2026-10-23 확인, 10-24 에 표를 다시 열어 같은 값을 봤다). 처음에는 코디네이터가 준 값이라고만 적었다).

모델 비교(전부 1회 표본. Fable xhigh 만 3회 평균):

| 모델, effort | 후보 | matched | partial | FP | FN | recall 엄격 / 관대 | precision | high | 비용 | 출력 토큰 | 호출 최대 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Fable 5.1 xhigh, D-26 재채점 run (`pilot-secondswap-fable-d26-nm`, 10:58~11:10) | 41 | 7 | 3 | 11 | 23 | 0.233 / 0.333 | 0.389 | 1/3 | $5.40 | 94,809 | 284.3초 |
| Haiku 5.5 high (`cmp-haiku-5-5-d26`, 11:10~11:21) | 34 | 5 | 1 | 21 | 25 | 0.167 / 0.200 | 0.192 | 1/3 | $0.045 | 75,055 | 110.9초 |
| Sonnet 5.5 high (`cmp-sonnet-5-5-d26`, 스캔 11:21~11:22, rescore 16:32~16:41) | 27 | 6 | 2 | 9 | 24 | 0.200 / 0.267 | 0.400 | 1/3 | $0.41 | 27,781 | 55.0초 |
| Opus 5.5 xhigh (`cmp-opus-5-5-d26`, 16:41~16:52) | 26 | 8 | 0 | 6 | 22 | 0.267 / 0.267 | 0.571 | 2/3 | $1.32 | 52,623 | 142.7초 |
| Fable 5.1 xhigh, 3회 평균(아래 표) | 40.3 | 9.67 | 3.00 | 11.3 | 20.3 | 0.322 / 0.422 | 0.456 | 0.444 | $5.42 | 95,228 | 302.8초 |

D-26 재채점 run 의 `scan_findings` 18 은 matched 7 + partial 3 + 관대 기준 FP 8 이다(채점기 계산식, D-26 소절). Haiku 는 `fallbacks="default"` 파라미터로 400 이 나지 않았고 폴백 0 이다. Sonnet 5.5 의 채점이 메모리 부족 중단 뒤 rescore 로 끝난 사건은 위 사건 기록과 같다. Sonnet 의 manifest 는 rescore 라 `timeout_s` 가 없다. 호출 최대는 각 run 의 `_usage.call.elapsed_s` 최댓값이고 출력 최대 호출은 Opus 14,399 토큰, Fable d26 run 22,712 토큰이다. Sonnet 5.5 의 비용은 처음 $0.42 였다. 캐시 읽기를 $0.20 으로 계산해서이고 공식 단가는 $0.10 이다. 38,721 × $2 + 20,616 × $2.5 + 61,848 × $0.10 + 27,781 × $10 (per MTok) = $0.413 이라 $0.41 로 고쳤다(`raw/scan.jsonl` 의 `_usage` 16행을 다시 합산해 토큰 수를 확인했다. D-29 리뷰 후 수정).

Fable 5.1 xhigh ×3(`fable-5-1-d26-rep3`, 16:52~17:41. rep-1 16:52:21~17:09:42, rep-2 17:09:43~17:26:45, rep-3 17:26:46~17:41:00. 같은 manifest 의 에이전트 시간(프로젝트 wall)은 351.7, 362.5, 298.6초). 코디네이터의 별도 집계는 받지 않고 `rep-k/scores.json` 과 `raw/scan.jsonl` 로 직접 계산했다. rep-3 의 채점 값은 재채점(`runs/fable-5-1-d26-rep3-r3nm`, manifest 기준 17:45:01~17:56:39, exit 0, `truths_unevaluated` 0)으로 바꿨다. 코디네이터는 이 재채점을 "17:5x~18:0x"로 보고했고 manifest 의 시작은 17:45:01 이다. manifest 값을 쓴다.

| rep | 후보 | matched | partial | FP | FN | recall 엄격 / 관대 | precision | high | 비용 | 출력 토큰 | 호출 최대 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rep-1 | 42 | 12 | 1 | 10 | 18 | 0.400 / 0.433 | 0.545 | 2/3 | $5.91 | 104,937 | 329.9초 |
| rep-2 | 41 | 10 | 3 | 13 | 20 | 0.333 / 0.433 | 0.435 | 1/3 | $5.55 | 97,794 | 322.0초 |
| rep-3 (재채점 값) | 38 | 7 | 5 | 11 | 23 | 0.233 / 0.400 | 0.389 | 1/3 | $4.81 | 82,953 | 256.5초 |
| 평균 | 40.3 | 9.67 | 3.00 | 11.3 | 20.3 | 0.322 / 0.422 | 0.456 | 0.444 | $5.42 | 95,228 | 302.8초 |
| 표본 표준편차 | 2.08 | 2.52 | 2.00 | 1.53 | 2.52 | 0.084 / 0.019 | 0.080 | 0.192 | $0.56 | 11,214 | 40.3초 |

후보, 비용, 출력 토큰, 호출 최대는 에이전트 단계의 값이라 채점 문제와 무관하다. 3회가 모두 유효하다. 채점 지표(matched 이하, recall, precision, high)만 재채점한 rep-3 를 쓴다. high 는 rep 별 0.667, 0.333, 0.333 이다. 3회 비용 합은 $16.27(5.907 + 5.550 + 4.808)이다. 참고로 D-26 재채점 run(위 표 첫 줄)도 같은 설정의 Fable xhigh 표본이라 넷으로 보면 엄격 recall 의 평균은 0.300, 표본 표준편차는 0.082 다(0.233, 0.400, 0.333, 0.233).

rep-3 오염 기록. 원래 rep-3 의 nethermind 채점은 OpenAI 호출 오류로 불완전했다. `scoring_output/scorer_stdout.log` 에 `Connection error` 가 206줄, `Request timed out` 이 1줄 있다(줄 수 기준). 결과는 `truths_unevaluated` 17/30, `actual_findings` 13 이었다. 그때의 헤드라인 recall 0.167(matched 5, partial 1, FN 8)은 채점 실패의 산물이고, 채점기 자신의 recall 0.385 는 13건 기준이다. rep-1(0.400)과 rep-2(0.333)는 `truths_unevaluated` 0 으로 정상이었다. 그런데 이 run 은 오류 없이 끝났다(원래 rep-3 manifest 에 `scoring_error` 가 없다). 합본 `fable-5-1-d26-rep3/scores.json` 의 `aggregate` 는 이 rep-3 를 그대로 품고 있어 무효다(엄격 recall 평균 0.300 ± 0.120, precision 0.431 ± 0.117, high 0.556, `truths_unevaluated` 평균 5.67 이 그 값이고, 위 표가 바르다). 합본 파일은 고치지 않았다. 반복 통계는 rep-1, rep-2, 재채점 rep-3 로 다시 낸 위 값을 쓴다. 이 사건 때문에 아래 "채점 불완전" 표시를 넣었다.

해석. 전부 1회 표본이고(Fable 만 3회) 2~3건 차이는 변동 범위다. 엄격 recall 의 표본 표준편차 0.08 은 모델 사이 차이(Fable 0.32, Opus 0.27, Sonnet 0.20)와 같은 크기라, 1회 비교로 모델 순위를 정할 수 없다. Opus 와 Sonnet 도 반복이 필요하다. 그 한도 안에서 눈에 띄는 것은 둘이다. Opus 5.5 xhigh 는 비용이 Fable 3회 평균의 약 1/4 이고($1.32 대 $5.42) matched 8, precision 0.571, high 2/3 였다. Haiku 5.5 의 recall 은 0.167 로 가장 낮고 precision 도 0.192 다.

누적 Anthropic 지출(손계산, 추정 누적): $4.01(선행 5파일) + $10.29(본 실행 36파일) + $5.40(D-26 재실행) + $0.045(Haiku) + $0.41(Sonnet) + $1.32(Opus) + $16.27(Fable xhigh ×3) = 약 $37.74(항목을 소수 셋째 자리까지 합하면 37.743). 상한 $200 의 19% 다. 처음에는 Sonnet 을 $0.42, 합계를 $37.75 로 적었다(위 Sonnet 정정). 채점기의 OpenAI 비용은 측정하지 못했다(대시보드로만 잰다).

### 9. 채점 불완전 표시 (코디네이터 추가 요청)

채점 결과에서 어떤 프로젝트의 `truths_unevaluated` 가 0 보다 크면 `judge_incomplete` 로 표시한다. 위 rep-3 처럼 judge 호출이 실패해도 run 은 오류 없이 끝나고 헤드라인 recall 만 조용히 낮아졌기 때문이다.

- `scores.json` 과 manifest 에 `judge_incomplete: true` 와 `judge_incomplete_projects: [{"project_id", "truths_unevaluated"}]`. 없으면 두 키 모두 없다(`unsupported` 와 같은 방식). `--repeat` 합본(`_aggregate`)은 rep 마다의 항목을 `run: "rep-3"` 과 함께 모아서 합본 `scores.json` 과 manifest 에 싣는다. 합본의 평균과 표준편차를 믿지 말라는 표시다.
- 콘솔 요약(`_print_summary`, `eval rescore` 끝에도): `채점 불완전: <프로젝트> 미평가 정답 N건(채점 로그 확인)` 한 줄씩. 합본에는 `(채점 로그 확인, rep-3)` 처럼 rep 이 붙는다. 처음 문구는 `(rescore 필요)` 였고 같은 날 뒤따른 작업에서 바꿨다('코드 수정 후속' 4).
- 리포트: Headline 표 바로 아래에 같은 줄과 설명 한 문단. HTML 에도 같다.
- README 에 `eval rescore` 절과 "채점 불완전 시 복구 절차"(run 또는 `rep-k` 사본을 만들고 manifest, scores, report, `scoring_output` 을 지운 뒤 rescore)를 적었다. `rep-3` 의 재채점이 이 절차였다.
- 실제 데이터 확인(읽기 전용): 새 감지 함수를 `fable-5-1-d26-rep3` 의 `rep-1`, `rep-2`, `rep-3` `scores.json` 과 재채점본(`fable-5-1-d26-rep3-r3nm`)에 적용하면 원래 rep-3 만 `truths_unevaluated` 17 로 잡히고 나머지 셋은 비어 있다. 그 run 들의 파일은 고치지 않았다(표시는 새 run 부터 붙는다).
- 한계: `truths_unevaluated` 는 judge 호출 실패 말고도 에이전트가 지적을 0건 냈거나 매칭으로 지적이 다 소진돼도 0 보다 커진다(채점기 정의, `scoring/nethermind.py` DEFINITIONS). 구분하지 못한다. 그래서 줄은 "로그를 보라"는 신호이고 judge 호출 실패의 증거가 아니다. 설명 문단과 README 가 이 점을 적는다. 로그의 오류 줄은 `scoring_output/scorer_stdout.log` 에서 센다. 처음 줄 문구는 `(rescore 필요)` 로 단정했고 지적 0건 프로젝트에도 켜졌다. 같은 날 뒤따른 작업에서 조건에 `n_findings > 0` 을 더하고 문구를 '채점 로그 확인' 으로 바꿨다('코드 수정 후속' 4). 지적 0건은 이제 빠지지만 매칭 소진과 judge 호출 실패는 여전히 구분하지 못한다.

### 10. 시험 (실제 API 호출 없음)

- `scan.py --selftest`: 138건(D-27 의 132건에 +6). `call_cap` 의 값(64000 이하 1800, 128000 에서 3600, 올림), `call_limits` 의 `cap` 인자, `max` 가 `EFFORTS` 에 있는 것. `python -O scan.py --selftest` 도 138건이다.
- `tests/test_scan_fake_api.py`(85건, +12): effort `max` 와 `--max-tokens 128000` 이 요청 본문(`output_config.effort`, `max_tokens`)과 `_run` 행(`effort`, `max_tokens`, `call_timeout_s` 3600)과 SDK 가 보낸 read timeout 헤더(3600.0)에 반영됨(Fable 5.1·Opus 5.5 둘, 스트리밍·beta·폴백·TTL 5m·thinking 없음은 그대로). 기본 실행은 64000 과 1800. `--max-tokens 1`. `--deadline-s 20000` 에서 호출별 timeout 3600, 재시도 2. 범위 밖 7종(`0`, `-5`, `128001`, `1000000`, `abc`, `64000.5`, 빈 값)은 exit 2 이고 요청이 안 나간다. 기존 `--effort` 거부 시험에서 `max` 를 허용값으로 옮겼다.
- `tests/test_scan_parse.py`(74건, +1): 상수(`MAX_TOKENS` 64000, 상한 128000, `EFFORTS`)와 `call_cap` 값, 어댑터 `max_call_s` 와의 일치(7개 값). 기존 `EFFORTS` 단언은 `max` 를 포함하게 고쳤다.
- `tests/test_adapters.py`(89건, +26): `--effort max`·`--max-tokens` 의 argv 전달(안 줬으면 없음)과 지문, scan.py 소스에서 기본값을 읽음(변수·읽을 수 없는 소스는 `unknown`, 예외 없음), 생성자의 범위 검증 7종, `max_call_s` 8개 값이 `call_cap` 과 일치, 128000 이 36 tokens/s 최악을 덮음, 소프트 한도와 데드라인 6종(128000 에서 10800, 7230, 7229, 100000, 64000, 16000)과 경고 문구, 기본 timeout 10800 유지와 `--timeout 14500` 산수.
- `tests/test_attacktrail_cli.py`(19건, +1): CLI 어댑터의 `max_tokens` 거부(생성자, `get_agent`)와 manifest `n/a`.
- `tests/test_cli.py`(41건, +16): `EFFORTS` 가 scan.py 와 같고 `max` 포함, 상한 128000 이 scan.py·어댑터와 같음, `--effort max` 와 `--max-tokens` 가 `agent_opts` 로 감(정수), CLI 어댑터·baseline 에서는 거부, 범위 밖 7종 exit 2, 경계 1·64000·128000 허용, `rescore` 도 전달·거부.
- `tests/test_report.py`(32건, +3): Manifest 의 `max_tokens` 행(있을 때만, `n/a` 도), 지원 밖 절의 분모 문장, 채점 불완전 줄(Headline 아래, 합본의 rep 표시, 콘솔 요약, 표시 없으면 없음).
- `tests/test_runner.py`(34건, +4): `rescore` 가 요약을 stderr 에 내고 stdout 은 비움(B3), 채점 불완전 표시가 scores.json·manifest·콘솔·리포트에 있고 온전한 run 에는 없음, 반복 합본이 불완전한 rep 를 싣고 전부 온전하면 없음, `eval run` 의 agent_opts 가 manifest 와 리포트 Manifest 표에 `effort max`, `max_tokens 128000` 으로 남음(없으면 64000). 기존 D-27 시험은 `unsupported.line` 의 분모 문장을 단언하게 고쳤고, 시험 도우미의 가짜 에이전트 지적 수를 1건에서 8건으로 늘렸다(1건이면 정답 4건 중 3건이 채점되지 않아 새 표시가 켜진다).
- `tests/test_scan_select.py`(16건, +2): `--web` 에서 전부 크기 상한 초과면 실제 scan.py 가 exit 1 과 `크기 상한 초과로 뺀 파일 N개` 를 내고(API 호출 없음) 어댑터가 사유를 구분함, 같은 저장소에서 큰 파일이 없으면 D-27 의 옛 사유 그대로. CLI 어댑터 경로는 argv 크기 검사를 대체해서 `too_big` 에서 같은 사유가 나오는 것을 본다.
- `tests/test_scoring.py`(6건)는 변동 없다.
- 변이 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치 스크립트): 22개를 모두 시험 실패로 잡았다. scan.py 7개(effort `max` 제거, 요청 본문 `max_tokens` 고정, `_run` 행 `max_tokens` 고정, 범위 검사 제거, `call_cap` 상수화, `call_limits` 에 cap 미전달, 크기 상한 사유 문구 제거), 어댑터 5개(argv 에서 `--max-tokens` 제거, `max_call_s` 상수화, 시간 계산이 `max_tokens` 무시, 지문에서 `max_tokens` 제거, CLI 어댑터가 `max_tokens` 수용), `cli.py` 3개(effort `max` 제거, CLI 에이전트에 `--max-tokens` 허용, 범위 검사 제거), 러너 4개(`line` 분모 문장 제거, `rescore` 요약 제거, `judge_incomplete` 항상 끔, 합본이 표시를 안 옮김), 리포트 3개(Manifest 행 제거, 콘솔 요약의 채점 불완전 줄 제거, 지원 밖 절 분모 문장 제거). 첫 실행이 10번째 변이에서 Windows 의 일시적 쓰기 오류(`OSError: [Errno 22] Invalid argument`, `attacktrail.py` 열기)로 중단됐고, 복구 루프도 `cli.py` 에서 같은 오류를 냈다. 중단 직후 변이 대상 6개 파일 모두에서 변이 전 문구가 정확히 1회씩 있는 것(변이 흔적 없음)을 확인하고, 쓰기에 재시도를 넣은 스크립트로 10번째부터 다시 돌렸다. 최종 복구는 6개 파일 모두 바이트 동일이다. scan.py 의 SHA256 은 변이 전과 후가 같다(`39C5A863…`). 나머지 파일은 변이 전 해시를 남기지 않아 위 문구 확인과 이후 전체 시험 통과로 확인했다.

검증 기록(2026-10-22. 파일별로 나눠 돌렸고 각 8분 안이다. 가장 긴 것은 scan_fake_api 5분 38초이고 같은 파일의 앞선 실행은 5분 31초였다):

```
scan.py --selftest           selftest OK (138 checks)
python -O scan.py --selftest selftest OK (138 checks)
pytest                       396건 통과, 실패 0건
                             adapters 89, attacktrail_cli 19, cli 41, report 32, runner 34,
                             scan_fake_api 85, scan_parse 74, scan_select 16, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f (앞 16자 2d0bb457ca2c605a, 전과 같음)
call_shape                   api-v6 (API), cli-v1 (CLI)
model / effort / max_tokens  claude-fable-5-1 / xhigh / 64000 (API 기본, 옵션을 안 줄 때), n/a / n/a (CLI)
scan.py                      SHA256 39C5A86388279B0367BAC62747213B0DF04D56601BEA5DF419513D0F7BA45376
                             (전은 550694799C084F114B7E1F4F13AD4CEF9E8531174621BDEADD518BCEDEA672BF)
legacy prompt_hash           API 3b9f9f3d13872259…  CLI 67513d5745174f99…
CRLF                         DECISIONS.md, runner/run.py, report/render.py 의 LF 단독 줄 0 (줄바꿈 보존)
```

### 11. 남은 위험

- 실제 서버가 effort `max` 와 `max_tokens` 128000 요청을 받는지는 키를 쓰지 않아 확인하지 못했다. 모델 접근, 폴백(`claude-opus-4-8`, `claude-opus-5`)과의 조합, `max_tokens` 128000 이 폴백 모델에서도 받아들여지는지도 같다. 첫 호출이 400 이면 8건 연속에서 차단기가 멈춘다(비용 없음). 갱신(2026-10-23): D-29 실측으로 Fable 5.1 의 effort `max` 와 128000 요청은 정상 처리됐다. 폴백 조합은 폴백이 0 이라 못 봤고 Opus 5.5 는 진행 중이다(D-29 §5).
- Opus 5.5 의 128K 출력 한도는 문서만 근거다(`/v1/models` 로는 Fable 5.1 만 봤다).
- 128000 토큰 출력은 한 번도 실측이 없다. 지금까지 최대 출력은 24,912 토큰(§8)이고 max effort 에서 출력이 2배를 넘으면 비용과 시간이 추정(§5, §6)을 넘는다. 비용 상한 $200 은 도구가 막지 않는다. 4회 전부 최악이면 약 $350 이다(입력·캐시 포함. 처음에는 입력을 빼서 $348, §6). 갱신: D-29 의 Fable max 3회에서 최대 출력은 56,058 토큰이었다(D-29 §3).
- 기본 timeout 10800 은 모든 파일이 128000 토큰을 쓰는 최악에서 건너뛰는 파일이 생긴다. 36 tokens/s 면 4라운드 파일 4개, 호출당 3,600초(약 35.6 tokens/s 이하)면 3, 4라운드 파일 8개다(§5). 커버리지로 보이고 `--timeout 14500` 이 둘 다 덮는다.
- (해결) `compare` 는 처음에 effort, max_tokens, model 차이를 경고하지 않았다. 같은 날 뒤따른 작업에서 model, effort, max_tokens, call_shape 차이 경고를 더했다('코드 수정 후속' 2).
- `rescore` 에서 `--effort`, `--max-tokens` 를 안 주면 manifest 에는 사본 scan.py 의 기본값(xhigh, 64000)이 적힌다. 원래 run 이 max, 128000 이었다면 같은 값을 줘야 한다. 재채점한 rep-3 사본은 D-28 전 코드로 돌아서 manifest 에 `max_tokens` 키가 없다(이미 쓴 manifest 라 고치지 않았다). 같은 날 뒤따른 작업에서 rescore 가 원래 run 의 `_run` 행과 대조하고 안 준 값을 채우게 했다('코드 수정 후속' 1).
- `judge_incomplete` 는 처음에 지적 0건이나 소진에도 켜졌다(§9). 지적 0건은 같은 날 뒤따른 작업에서 뺐고 소진은 여전히 켜진다('코드 수정 후속' 4).
- 가짜 서버 시험은 요청 본문과 시간 값까지만 본다. 서버가 `fallbacks="default"` 와 함께 `max` 를 받아들이는지, 폴백이 일어났을 때 128000 이 유지되는지는 실제 호출로만 알 수 있다.

### 재검증 후 수정 (2026-10-23)

D-28 리뷰(2026-10-22)가 남긴 8건 중 문서 쪽을 고쳤다. 새 D 번호는 만들지 않았다. 번호는 리뷰의 항목 번호다. 이 소절의 작업은 `DECISIONS.md` 와 `README.md` 둘만 바꿨다. 코드(`scan.py`, `eval/*.py`, 시험)는 건드리지 않았고 pytest 도 돌리지 않았다(분리 실행 중인 배치가 eval 모듈을 새로 import 할 예정이고 시스템 메모리가 적다). 실제 API 는 호출하지 않았다. 수정 전 두 파일은 저장소 밖 `C:\Users\정동욱\AppData\Local\Temp\at_backup_20261023a\` 에 있다.

| # | 지적 | 처리 |
|---|---|---|
| 1 | README 복구 예시가 한 프로젝트만 채점하게 하고 `--max-tokens` 가 없어, 128000 run 을 복구하면 manifest 라벨이 틀린다 | (코드 후속에서 변경됨: 최종 README 복구 예시는 `--model`·`--effort`·`--max-tokens` 세 옵션을 뺐다. rescore 가 `_run` 값을 채우게 되어서다. 아래 `--max-tokens` 예시와 '값을 안 주면 기본값이 적힌다' 경고는 최종 README 에 없다. '코드 수정 후속' 1 참고) README rescore 절. 예시에 `--max-tokens <원래 값>` 을 더하고 `--projects` 는 원래 run 과 같은 범위로 주라고 적었다. 값을 안 주면 manifest 에 기본값(xhigh, 64000)이 적힌다는 경고도 더했다. `--projects` 를 아예 빼는 안은 쓰지 않았다. 빼면 `select_projects` 가 데이터셋 전체를 고르고, `projects/` 에 결과가 없는 프로젝트가 `crashed` 로 기록된다. rescore 가 `_run` 행과 대조하는 기능은 코드 수정 대기 |
| 2 | `call_shape` 근거 문장이 틀렸다(D-23 은 effort 값 때문에 안 올린 것이 아니라 `api-v5` 로 올렸다) | §3 의 `call_shape` 항목을 규칙 문장과 정정으로 고쳤다. 코디네이터 결정(2026-10-22)으로 `api-v6` 유지. attacktrail.py 309~311행 주석 수정과 `compare` 경고 추가는 코드 수정 대기 |
| 3 | 비용 서술이 자기 표와 안 맞는다 | §6 표의 세 행(4회 합 보통 $33~50, 누적 보통 $71~88, Fable 최악 $103.1)과 같은 수치를 쓰는 §5 결정 문단, §6 상한 문단, "아직 안 된 것" 비용 행을 고쳤다. 리뷰가 든 2146행은 §8 의 누적 $37.75 줄이고 값이 맞아서 그대로 뒀다(이후 Sonnet 단가 정정으로 $37.74 가 됐다, D-29 리뷰 후 수정) |
| 4 | `judge_incomplete` 문구가 원인을 단정한다 | README 콘솔 요약 항목과 §9 한계에 한 문장씩 더했다. 조건에 `n_findings > 0` 을 더하는 것과 줄 문구 변경은 코드 수정 대기 |
| 5 | 36 tok/s 산수가 틀렸다(4라운드 파일 4개가 맞고, 8개는 호출당 3,600초일 때) | §5, §11, "아직 안 된 것" 시간 상수 행, README 를 두 경우로 나눠 고쳤다. `--timeout 14500` 권고는 그대로. attacktrail.py 74~77행 주석은 코드 수정 대기 |
| 6 | README 에 서버가 max 와 128000 을 받는지 미확인 표기가 없다 | README 에 한 문장 썼다. D-29 실측으로 확인돼서 확인됨 문장으로 바로 갱신했다 |
| 7 | `SOFT_MARGIN_S` 가 64000 기준 상수로 남았다 | 코드 수정 대기 |
| 8 | 굵은 글씨가 늘었다 | D-28 문단에는 굵은 글씨가 없다. 리뷰의 +11 은 HEAD 와 비교한 별표 두 개 표기의 수다. 늘어난 것은 D-22 의 굵은 글씨 5곳(10개)과 D-27 표의 `Agent.failed` 인자 표기 1개였다. D-22 의 5곳을 평문으로 바꿨다. 별표 두 개 표기는 HEAD 48, 이 수정 후 49 이고 차이 1은 위 인자 표기다. em대시는 13 그대로 |

코드 수정 대기였던 5건(이 소절의 문서 작업에서는 하지 않았다. 행 번호는 리뷰 시점 것이고 2026-10-23 에 열어 같은 위치임을 확인했다. 5건 모두 같은 날 뒤따른 작업에서 했고 아래 '코드 수정 후속' 에 적었다. 위 표의 '코드 수정 대기' 표기도 같은 건이다):

- 1: `rescore`(runner/run.py)가 `projects/<id>/raw/scan.jsonl` 첫 줄 `_run` 의 effort, max_tokens 를 읽어 `--effort`, `--max-tokens` 와 대조하고, 다르면 거부하거나 경고한다. 기본값을 manifest 에 적는 곳은 attacktrail.py 382~385행이다.
- 2: attacktrail.py 309~311행 `call_shape` 주석을 위 규칙으로 고친다. `report/compare.py` 에 model, effort, max_tokens 차이 경고를 더한다(시험 포함). 그때 README compare 절과 "아직 안 된 것" 의 해당 행도 고친다.
- 4: `_judge_incomplete`(runner/run.py 172행)에 `n_findings > 0` 조건을 더하고, report/render.py 206행의 `(rescore 필요)` 를 `(채점 로그 확인)` 으로 바꾼다(시험의 문자열 단언 포함). 선택 사항으로 `scorer_stdout.log` 의 `Connection error`, `Request timed out` 줄 수로 게이트한다.
- 5: attacktrail.py 74~77행 주석의 36 tok/s 산수를 두 경우(4개, 8개)로 고친다.
- 7: attacktrail.py 97행 `SOFT_MARGIN_S` 의 이름이나 주석(42~45행)에 64000 기준임을 밝히고 tests/test_adapters.py 831행도 맞춘다. 실행 로직은 `_run` 의 계산을 쓰므로 동작에는 영향이 없고, 주석이 128000 에서 틀린다.

### 코드 수정 후속 (2026-10-23)

위 코드 수정 대기 5건(리뷰 번호 1, 2, 4, 5, 7)을 구현했다. 새 D 번호는 만들지 않았다. 실제 API 는 호출하지 않았고 `eval/runs/` 는 읽기만 했다. 시험은 임시 폴더, mock 판정자, 데이터셋이 제외한 프로젝트(`code4rena_initia-move_2025_04`, 빈 커밋)로만 돌렸다. 수정 전 파일은 저장소 밖 `C:\Users\정동욱\AppData\Local\Temp\at_backup_20261023b\` 에 있다. `internal/tools/scan.py` 는 건드리지 않았다(SHA256 이 백업과 같다).

| # | 변경 | 위치 |
|---|---|---|
| 1 | `eval rescore` 가 원래 run 의 `_run` 행과 인자를 대조한다. attacktrail API 어댑터에서 선택된 프로젝트의 `projects/<id>/raw/scan.jsonl` 첫 줄 `_run` 의 model, effort, max_tokens 를 읽는다. `--model`, `--effort`, `--max-tokens` 를 줬는데 다르면 `ValueError` 로 거부한다(cli.py 가 `재채점 중단: --effort xhigh 는 원래 run 의 effort 값(max)과 다르다 (projects/<id>/raw/scan.jsonl 첫 줄 _run). 원래 값을 주거나 --effort 를 빼라(빼면 _run 값을 쓴다)` 로 찍고 exit 1. 이 시점에는 manifest, scores.json 이 아직 안 써졌다). 안 준 항목은 `_run` 값을 manifest 에 적는다(scan.py 기본값이 아니다). 행이나 파일이 없거나 키의 타입이 틀리면 그 키는 의견 없음이라 전과 같다. CLI 어댑터(`scan-cli.jsonl` 을 쓴다)와 baseline 은 검사하지 않는다. | `runner/run.py` `RUN_OPTS`, `_opt_ok`, `_logged_opts`, `_check_logged_opts`, `rescore()` |
| 2 | `call_shape` 주석을 새 규칙으로 고쳤다: 요청 본문의 구조(엔드포인트, 블록 구성, 파라미터 유무)가 바뀌면 올리고, model, effort, max_tokens 는 manifest 필드(`agent.model`, `agent.effort`, `agent.max_tokens`)에 기록한다. D-28 줄도 이 규칙을 가리킨다. `compare` 는 두 manifest 의 `agent.model`, `agent.effort`, `agent.max_tokens`, `agent.call_shape` 가 다르면 항목마다 경고 한 줄(`agent.effort differs: A=xhigh B=max`)을 낸다. 기존 경고와 같은 `WARNING: ... Deltas may not be comparable.` 줄이다. repeat run 은 합본 manifest 의 `agent`(`runner._aggregate` 가 rep-1 의 것을 복사해 둔다)를 그대로 읽는다. 한쪽이 키를 기록하지 않았으면(D-28 전 manifest 의 `max_tokens`) 그쪽이 `미기록` 으로 나오고 다른 값으로 센다. | `adapters/attacktrail.py` 316~320행과 340~343행 주석, `report/compare.py` `AGENT_KEYS`, `_agent_diff`, README compare 절 |
| 4 | `_judge_incomplete(scores, records)` 가 `truths_unevaluated > 0` 이고 manifest 레코드의 `n_findings > 0` 인 프로젝트만 센다. `n_findings` 는 scores.json 에 없어서 인자를 늘렸다(None 은 켜지 않는다). 줄 문구를 `(rescore 필요)` 에서 `(채점 로그 확인)` 으로 바꿨다(합본은 `(채점 로그 확인, rep-3)`). 콘솔 요약이 같은 함수라 같은 문구다. Headline 아래 설명 문단도 '지적을 매칭으로 다 쓴 프로젝트도 이렇게 보인다(에이전트 지적이 0건인 프로젝트는 표시하지 않는다)' 로 고쳤다. 선택 사항이던 `scorer_stdout.log` 줄 수 게이트는 하지 않았다. | `runner/run.py` `_judge_incomplete`, `report/render.py` `judge_incomplete_lines`와 Headline 문단, README 콘솔 요약 항목 |
| 5 | 36 tok/s 산수를 두 경우로 고쳤다. 36 tok/s 는 호출당 3,556초라 2라운드가 7,111초에 끝나 소프트 한도(7,185초) 이전이다. 3라운드까지 돌고(10,667초에 끝남) 4라운드 파일 4개가 `skipped: time budget reached` 다(커버리지 12/16). 호출당 3,600초(약 35.6 tok/s 이하)는 2라운드가 7,200초에 끝나 15초 늦어서 3, 4라운드 파일 8개가 건너뛰어진다(8/16). `--timeout 14500` 은 둘 다 덮는다. | `adapters/attacktrail.py` 77~82행 주석 |
| 7 | `SOFT_MARGIN_S` 는 이름을 바꾸지 않았다. 코드가 읽지 않는 상수다(참조는 주석과 `tests/test_adapters.py` 한 곳뿐이고 `_run()` 은 `DEADLINE_MARGIN_S + max_call_s(max_tokens)` 로 계산한다). 42~48행 주석과 102~103행(상수 앞) 주석에 '64000 기준값 1815초, 128000 에서는 3615초, 실제 값은 `_run()` 이 max_tokens 에 비례해 계산' 을 적었다. 시험 주석도 맞췄다. 동작과 값은 그대로다. | `adapters/attacktrail.py`, `tests/test_adapters.py` 201~205행과 826~833행 주석 |

README 는 rescore 절(대조와 채워 넣기, 복구 예시에서 `--model`·`--effort`·`--max-tokens` 를 뺌), 콘솔 요약 항목, compare 절 두 항목, 파일 선택 절의 compare 문장을 고쳤다.

판단 둘. 프로젝트마다 `_run` 이 같은 키에 서로 다른 값을 기록했으면 인자가 없어도 거부한다. 한 run 은 에이전트 하나로 도므로 정상 run 에서는 안 생기고, 생기면 라벨을 하나로 정할 수 없어서다. 비교는 정확 일치다. `_run.model` 은 scan.py 가 보낸 요청 모델 이름이라 별칭 정규화는 하지 않는다.

시험(실제 API 호출 없음). 6건을 더했고 문구 단언 5곳을 고쳤다.
- `tests/test_runner.py`(38건, +4): `test_rescore_refuses_arguments_that_differ_from_the_logged_run_row_and_accepts_equal_ones`(세 옵션 각각과 effort 만 다르고 나머지는 같은 경우를 거부하고 아무것도 안 쓴다, cli.py 가 exit 1 과 사유를 낸다, 같은 값은 통과), `test_rescore_takes_the_logged_values_for_arguments_not_given_and_scan_pys_defaults_without_a_row`(안 준 값을 `_run` 에서 채움, 일부만 준 경우, `scan.jsonl` 이 없으면 scan.py 기본값 xhigh·64000, CLI 어댑터는 검사 안 함), `test_logged_run_rows_that_disagree_are_refused_and_unreadable_ones_are_ignored`(서로 다른 값 거부, JSON 아님·`_run` 없음·타입 틀림·파일 없음은 무시), `test_a_project_without_agent_findings_is_not_flagged_as_incomplete_judging`(n_findings 0 이면 플래그 없음, `_judge_incomplete` 단위 3경우). 기존 시험 세 곳의 문구 단언을 `채점 로그 확인` 으로 고쳤다.
- `tests/test_report.py`(34건, +2): `test_compare_warns_per_agent_setting_that_differs_and_only_for_those`(같으면 경고 없음, effort·max_tokens 만 다르면 그 둘만, model·call_shape, 미기록 쪽, 양쪽 미기록은 경고 없음. 이 마지막은 D-29 리뷰 후 수정 R2 에서 경고 한 줄로 바뀌었다), `test_compare_reads_the_agent_of_a_repeat_run_from_its_aggregate_manifest`. 기존 시험의 문구 단언 두 곳과 주석 두 곳을 고쳤다.
- `tests/test_adapters.py`(89건): 건수 변동 없음. 시험 주석을 `SOFT_MARGIN_S` 가 64000 기준값이라는 말에 맞췄다.
- 변이 확인(레포 파일을 잠깐 바꾸고 바이트 단위로 복구, 스크래치 스크립트): 8개를 모두 시험 실패로 잡았다. rescore 4개(`_run` 검사 전체 건너뜀, 다른 인자를 거부하지 않음, 안 준 값을 `_run` 에서 안 채움, 프로젝트 간 불일치를 거부하지 않음), compare 2개(에이전트 경고 제거, model 을 비교 대상에서 뺌), 러너 1개(`n_findings > 0` 조건 제거), 리포트 1개(줄 문구를 옛 `(rescore 필요)` 로). 스크립트가 변이마다 복구 뒤 SHA256 이 변이 전과 같은 것을 확인했다(CRLF 파일은 치환도 CRLF 로 했다).

검증 기록(2026-10-23. 파일별로 나눠 돌렸고 각 8분 안이다. 가장 긴 것은 scan_fake_api 5분 32초):

```
scan.py --selftest           selftest OK (138 checks)
python -O scan.py --selftest selftest OK (138 checks)
pytest                       402건 통과, 실패 0건 (396 + 6)
                             adapters 89, attacktrail_cli 19, cli 41, report 34, runner 38,
                             scan_fake_api 85, scan_parse 74, scan_select 16, scoring 6
prompt_sha256                2d0bb457ca2c605a07cbd6d12469ba61236859556a7fc79056d20c069a00929f 의 앞 16자 2d0bb457ca2c605a (전과 같음, 두 어댑터 모두)
call_shape                   api-v6 (API), cli-v1 (CLI)
model / effort / max_tokens  claude-fable-5-1 / xhigh / 64000 (API 기본, 옵션을 안 줄 때)
scan.py                      SHA256 39C5A86388279B0367BAC62747213B0DF04D56601BEA5DF419513D0F7BA45376 (백업과 같고 D-28 §3 의 값과 같음)
CRLF                         DECISIONS.md, runner/run.py, report/render.py, report/compare.py 의 LF 단독 줄 0 (README.md, attacktrail.py, 시험 파일은 원래 LF)
em대시, 별표 두 개 표기      DECISIONS.md 는 13개, 49개 그대로(새 문단에 추가 없음)
```

남은 위험.
- `_run` 대조는 `projects/<id>/raw/scan.jsonl` 첫 줄의 `_run` 형태에 기댄다. scan.py 가 키 이름이나 위치를 바꾸면 조용히 의견 없음이 되어 전처럼 동작한다(거부도 채움도 없다). 시험이 현재 형태를 고정한다.
- 사본에서 `raw/` 를 지우고 rescore 하면 대조가 없다. README 의 복구 절차는 `projects/` 를 그대로 두라고 한다.
- `compare` 의 `미기록` 경고는 D-28 전 manifest 와 비교할 때 실제 값이 같아도(64000) 뜬다. 의도한 동작이다.
- `judge_incomplete` 는 매칭 소진과 judge 호출 실패를 여전히 구분하지 못한다. 이미 만든 run 의 manifest, scores.json, 리포트는 새 조건과 문구로 갱신하지 않았다. D-29 의 run 들은 표시가 없어서 영향이 없다.
- 리뷰 번호 3, 6, 8 은 문서 쪽이라 위 재검증 후 수정에서 끝났다. 이 작업의 범위가 아니다.

## D-29. effort max, max_tokens 128000 실측: Fable 5.1 max 3회와 Opus 5.5 max (실행 2026-10-22~23, 기록 2026-10-23)

D-28 에서 정한 실행(Fable 5.1 max 3회, Opus 5.5 max 1회, secondswap 16파일, `max_tokens` 128000, nethermind 채점)의 결과를 기록한다. 처음 기록은 부분 기록이었다(rep-1, rep-2 는 채점까지, rep-3 는 스캔 수치까지. rep-3 채점과 Opus max 는 진행 중이라 자리만 남겼다). 같은 날 뒤따른 작업에서 rep-3 채점, 합본 통계, Opus max 결과와 종합표를 채웠다(§3, §4, §6 이후). reviewer 검증 전이다. 수치는 `scores.json`, manifest, `raw/scan.jsonl` 로 다시 계산했고 비용은 토큰 × 공식 단가의 손계산이다(Fable 5.1 입력 $10, 출력 $50, 캐시 읽기 $0.25, 5분 쓰기 $12.50 per MTok). 코디네이터가 준 수치는 재계산 값과 모두 맞았고 호출 최대의 배율 하나만 달랐다(§4). 두 번째 작업의 수치(rep-3 채점, 합본 통계, Opus, 누적 지출)도 같은 방식으로 다시 계산했고 호출 최대의 '약 1.7배' 하나만 어긋났다(§4, §10). 이 기록 작업은 실제 API 를 호출하지 않았고 `eval/runs/` 는 읽기만 했다. 시각은 KST 다.

### 1. 실행

- 분리 PowerShell 프로세스(`%TEMP%\at_logs\max_batch.ps1`)로 2026-10-22 19:22:44 에 시작했다(`max_batch.status.txt`). Claude Code 백그라운드 셸이 시스템 메모리 부족으로 두 번 중단된 뒤 사용자가 분리 실행으로 정했다(코디네이터 보고, 이 기록에서 확인하지 못했다).
- 1단계(Fable. rep-1 manifest 의 `cli_argv` 로 확인): `eval run --agent attacktrail --dataset scabench --projects code4rena_secondswap_2025_02 --judge nethermind --model claude-fable-5-1 --effort max --max-tokens 128000 --timeout 14500 --repeat 3 --out runs\fable-5-1-d26-max-rep3`
- 2단계(Opus. 배치 스크립트로 확인): 같은 인자에서 `--model claude-opus-5-5`, `--repeat` 없이 `--out runs\cmp-opus-5-5-d26-max`.
- manifest `agent`(rep-1, rep-2): model `claude-fable-5-1`, effort `max`, `max_tokens` 128000, `call_shape` `api-v6`, `scan_py_sha256` 앞 8자 `39c5a863`, `prompt_sha256` 앞 16자 `2d0bb457ca2c605a`(둘 다 D-28 §3 과 같다), `fingerprint_source` `snapshot`, `timeout_s` 14500. `_run` 행은 `time_budget` 10885.0, `deadline_s` 14485.0, `call_timeout_s` 3600.

### 2. 중단 사건

코디네이터 보고이고 이 기록에서 다시 확인하지 못한 부분이 있다.

- 1차 시도(2026-10-22 18:32:53): Claude Code 가 시작 2분 만에 시스템 메모리 부족으로 중단했다. 완료한 파일은 0개, 진행 중이던 호출 4개가 끊겼다. 끊긴 호출의 과금은 기록이 없고 최대 $1~2 로 추정한다. 이 금액은 §6 의 누적에 넣지 않았다. 폴더를 지우고 다시 시작했다.
- 분리 실행 중 PC 가 절전에 들어갔다(Kernel-Power 506/507. 2026-10-22 20:27 rep-3 스캔 완료 이후 절전이었고 20:34:55 와 20:45:43 에 짧게 깼다가 다시 잠들었다. 10-23 11:47:53 에 재개했다, Kernel-Power 로그). rep-3 채점이 밤새 멈췄다. 이 기록에서 확인한 것은 rep-3 스캔의 마지막 호출이 20:27 에 끝난 것(`scan.jsonl`)뿐이다. 재개 시각 11:48 의 근거는 코디네이터가 11:48:40 에 본 `truth_trace.json`(41바이트)이다. 채점 도중의 중간 쓰기로 보이고(코디네이터 보고, 이 기록에서 확인하지 못했다), `scoring_output` 폴더가 11:48 에 갱신됐다는 뜻이 아니다. 지금 남은 파일 시각은 폴더 12:06:07, 최종 `truth_trace.json`(203바이트) 12:06:06 이라 11:48 의 흔적은 없다(2026-10-23 확인).

### 3. 결과: Fable 5.1 effort max

모든 run 은 secondswap 한 프로젝트, D-26 규칙의 16파일(스캔 16, 실패 0, 건너뜀 0)이고 채점은 nethermind(o4-mini, batch 10)다. 정답은 30건(high 3). 세 rep 의 `scan.jsonl` 에서 48호출 모두 응답 모델이 `claude-fable-5-1` 이고 `fallback` 이 false 이며, partial 행과 stderr 의 `ERROR` 줄이 없다(거절, 폴백, 잘림 0). 세 rep 모두 입력 38,721, 캐시 쓰기 20,616(첫 라운드 4호출 × 5,154), 캐시 읽기 61,848 토큰이라 입력과 캐시 비용은 rep 마다 $0.66 이다(D-28 §6 정정과 같은 값).

| rep | 스캔 | 후보 | matched | partial | FP | FN | recall 엄격 / 관대 | precision | high | 비용 | 출력 토큰 | 호출 최대 | 출력 최대 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rep-1 | 19:22~19:35 | 47 | 17 | 0 | 12 | 13 | 0.567 / 0.567 | 0.586 | 3/3 | $11.75 | 221,832 | 541.2초 | 43,794 |
| rep-2 | 19:49~20:00 | 47 | 12 | 0 | 13 | 18 | 0.400 / 0.400 | 0.480 | 1/3 | $10.29 | 192,621 | 494.3초 | 39,533 |
| rep-3 | 20:15~20:27 | 48 | 10 | 2 | 17 | 20 | 0.333 / 0.400 | 0.370 | 2/3 | $11.67 | 220,160 | 672.7초 | 56,058 |

세 rep 모두 `truths_unevaluated` 는 0 이고 `judge_incomplete` 표시도 없다(합본 `scores.json` 과 manifest 에도 없다). 채점은 rep-1 이 19:49(manifest 종료 19:49:21), rep-2 가 20:15(20:15:20)에 끝났다. rep-3 는 20:27 스캔 뒤 PC 절전(§2)으로 멈췄다가 10-23 11:48 에 재개해(§2 가 적은 11:48:40 `truth_trace.json` 중간 쓰기 목격이 근거이고 이 기록에서 다시 확인하지 못했다) 12:06:08 에 끝났다(batch status exit 0. manifest 종료 12:06:07, 결과 파일 12:06:06~07). manifest 의 시작~종료가 20:15:20 에서 다음 날 12:06:07 로 약 15시간 51분 찍히는 것은 절전 때문이다. `scoring_output/scorer_stdout.log` 에 `Connection error` 가 14줄 있었고(5~20행, 로그 앞쪽) `Request timed out` 은 0줄이다. rep-1, rep-2 와 Opus 의 로그는 둘 다 0줄이다. 그 뒤 정답 30건이 모두 평가됐고(`truths_unevaluated` 0, `truth_mapping` traced) 채점기가 재시도로 마친 것으로 본다. 재시도 코드는 이 기록에서 읽지 않았다(추정). xhigh 쪽 원래 rep-3(D-28 §8)가 채점 오류로 오염됐던 것과 달리 이 값은 `truths_unevaluated` 0 으로 온전하다.

- 호출 시간: 가장 긴 호출은 672.7초(rep-3)로 `call_timeout_s` 3600 의 19% 다. 이벤트 사이 최대 간격(`max_gap_s`)의 최댓값은 rep-1 489.3초, rep-2 433.5초, rep-3 613.3초로, xhigh 본 실행의 278.6초(D-25)보다 길다. 최댓값 613.3초는 무응답 상한 3600 의 17% 다.
- 출력 속도: 60초 넘는 29호출의 중앙값이 약 80 tokens/s(범위 74.1~84.5)다. D-25 의 74~77 보다 조금 빠르고, D-24 의 36 tokens/s 가정은 이번에도 맞지 않았다.
- 출력 최대: 세 rep 모두 `contracts/SecondSwap_Marketplace.sol` 이고, rep-3 의 56,058 토큰(128000 의 44%)이 가장 크다(672.7초, 83.3 tokens/s). D-28 §5 는 xhigh 최대 24,912 토큰의 2배인 약 50,000(39%)을 예상했는데 실제는 2.25배(56,058 ÷ 24,912)였다. 한도(128000 토큰, 3600초)에는 여유가 크다.
- 프로젝트 시간: 마지막 호출이 끝난 시점이 시작 후 746.9초(rep-1), 637.8초(rep-2), 706.7초(rep-3)다. xhigh 3회의 프로젝트 wall(351.7, 362.5, 298.6초)의 약 2.1배이고, 기본 소프트 한도(7185초) 안에서 끝났다.

### 4. xhigh 와의 비교 (3회 대 3회)

max 3회의 통계다(표본 표준편차, n-1). 합본 `scores.json` 의 `aggregate` 가 같은 값을 갖고 있다(matched 13 ± 3.6056, 엄격 0.4333 ± 0.1202, 관대 0.4556 ± 0.0962, precision 0.4789 ± 0.1079, high 0.6667 ± 0.3333).

| 항목 | rep-1 | rep-2 | rep-3 | 평균 | 표본 표준편차 |
|---|---|---|---|---|---|
| matched | 17 | 12 | 10 | 13.0 | 3.61 |
| recall 엄격 | 0.567 | 0.400 | 0.333 | 0.433 | 0.120 |
| recall 관대 | 0.567 | 0.400 | 0.400 | 0.456 | 0.096 |
| precision | 0.586 | 0.480 | 0.370 | 0.479 | 0.108 |
| high | 1.000 (3/3) | 0.333 (1/3) | 0.667 (2/3) | 0.667 | 0.333 |
| 비용(1회) | $11.75 | $10.29 | $11.67 | $11.24 | $0.82 |
| 출력 토큰 | 221,832 | 192,621 | 220,160 | 211,538 | 16,404 |
| 호출 최대 | 541.2초 | 494.3초 | 672.7초 | 569.4초 | 92.5초 |

xhigh 3회 평균(D-28 §8, rep-3 는 재채점 값)과 max 3회 평균의 비교다. 괄호는 원래 rep-3(채점 실패본)을 쓴 경우의 값이다.

| 항목 | xhigh 3회 평균 | max 3회 평균 | 차이 또는 배율 |
|---|---|---|---|
| 후보 | 40.3 | 47.3 | +7.0 |
| matched | 9.67 (원본 rep-3 이면 9.00) | 13.0 | +3.3 (원본이면 +4.0) |
| partial | 3.00 | 0.67 | -2.3 |
| FP | 11.3 | 14.0 | +2.7 |
| FN | 20.3 | 17.0 | -3.3 |
| recall 엄격 | 0.322 (원본 rep-3 이면 0.300) | 0.433 | +0.111 (원본이면 +0.133) |
| recall 관대 | 0.422 | 0.456 | +0.033 |
| precision | 0.456 | 0.479 | +0.022 |
| high | 0.444 | 0.667 | +0.222 |
| 비용(1회) | $5.42 | $11.24 | 2.1배 |
| 출력 토큰 | 95,228 | 211,538 | 2.2배 |
| 호출 최대(rep 별 최댓값의 평균) | 302.8초 | 569.4초 | 1.9배 |
| 호출 최대(세 rep 의 최댓값) | 329.9초 | 672.7초 | 2.0배 |

이 비교는 effort 와 `max_tokens` 가 함께 바뀐 비교다. xhigh 실행(Fable 5.1 3회, Opus 5.5)은 `_run` 행의 `max_tokens` 가 64000 이고 max 실행(Fable 5.1 3회, Opus 5.5)은 128000 이다(각 run 의 `raw/scan.jsonl` 첫 줄로 다시 확인했다). 그래서 effort 효과만 분리할 수 없다. xhigh 실행의 호출당 출력 최대는 24,912 토큰(Fable rep-1)이라 64,000 에 닿은 호출이 없다(잘림 0). 그래서 상한이 xhigh 쪽 결과를 제한한 영향은 작다고 보지만 추정이다. 같은 64000 으로 max 를 돌려 보지는 않았다. 반대쪽 한계도 하나 있다. max 실행의 출력 최대는 Fable 56,058, Opus 62,225 토큰이라(Opus 는 64,000 의 97%) 64000 이었다면 일부 호출이 상한에 닿았을 수 있다(추정).

xhigh 평균의 rep-3 는 재채점본(`fable-5-1-d26-rep3-r3nm`: matched 7, partial 5, `truths_unevaluated` 0)이다. 원래 rep-3(`fable-5-1-d26-rep3/rep-3`: matched 5, partial 1, 엄격 recall 0.167, `truths_unevaluated` 17)는 채점 중 OpenAI 호출 오류(`Connection error` 206줄)로 정답 17건이 평가되지 않았고(D-28 §8), 재채점은 그 채점 실패를 복구한 것이다. 에이전트 단계(후보, 비용, 출력 토큰)는 같고 채점만 다시 했다. 그래서 재채점본을 골랐다. 원본을 쓰면 xhigh 평균은 matched 9.00, 엄격 recall 0.300 이고 max 와의 차이는 +4.0, +0.133 으로 벌어진다(위 표 괄호). 원본이면 차이 0.133 이 max 의 표본 표준편차 0.120 보다 약간 크지만 xhigh 의 표본 표준편차도 0.120 으로 커져서(재채점본 0.084) 3회 대 3회로 확정하기 어렵다는 아래 결론은 같다.

호출 최대의 배율은 기준에 따라 1.9배(평균 대 평균, 569.4 ÷ 302.8 = 1.88)나 2.0배(최댓값 대 최댓값, 672.7 ÷ 329.9 = 2.04)다. 이번 요청서의 '약 1.7배' 는 569.4 ÷ 329.9 = 1.73 이라 평균과 최댓값을 섞은 값이다. 앞 기록의 '약 2배(541 대 330)' 는 541.2 ÷ 329.9 = 1.64배라 rep-1 만 보면 맞지 않고, rep-3 까지 넣은 최댓값 대 최댓값 2.04배는 맞다.

읽는 법. 엄격 recall 의 평균 차이 0.111 은 max 의 표본 표준편차 0.120 보다 작고 xhigh 의 0.084 보다 조금 크다. Welch t 는 1.31(자유도 약 3.6), 양측 p 는 약 0.27 이다(손으로 수치적분한 근사값). 두 설정의 범위도 겹친다(max 0.333~0.567, xhigh 0.233~0.400). rep 번호로 짝지으면 세 쌍 모두 max 가 높지만(0.567 대 0.400, 0.400 대 0.333, 0.333 대 0.233) rep 번호는 독립된 짝이 아니라서 근거로 쓰지 않는다. 그래서 max 가 xhigh 보다 recall 이 높다고 확정하지 않는다. 방향은 높은 쪽이다. 관대 기준 차이는 0.033 으로 작다. 엄격과 관대의 간격은 xhigh 0.100 에서 max 0.022 로 줄었는데, max 는 matched 가 늘고 partial 이 평균 3.00 에서 0.67 로 줄었다(관찰이고 이유는 보지 않았다). high 는 정답 3건 중 평균 2.0건(xhigh 1.33건)이고 값이 0.333 단위로만 나온다. 비용은 2.1배다.

### 5. 확인한 것과 아직 모르는 것

- 서버가 Fable 5.1 의 effort `max` 와 `max_tokens` 128000 요청을 받았다. 세 rep 의 `_run` 행에 effort `max`, `max_tokens` 128000, `call_timeout_s` 3600 이 있고 48호출이 모두 응답을 냈다(400 없음). 요청에 든 beta 헤더와 `fallbacks="default"` 도 받아들였다. D-28 §11 의 첫 위험이 Fable 5.1 에서는 해소됐다.
- 폴백이 일어나는 경우는 못 봤다(폴백 0). 그때 128000 이 폴백 모델에서 유지되는지는 여전히 모른다.
- Opus 5.5 도 effort `max` 와 `max_tokens` 128000 요청을 받았다. 16호출 모두 응답을 냈고(400 없음) 응답 모델이 `claude-opus-5-5` 이며 폴백 0 이다(§7). 다만 최대 출력이 62,225 토큰(128000 의 48.6%)이라 Opus 의 128K 출력 한도 자체는 이 실행으로 확인하지 못했다(문서만 근거인 상태 그대로).
- 시간 상수의 비례 설계(`call_cap`, 3600)는 한 번도 한도에 닿지 않았다. 최장 호출이 672.7초(Fable)와 561.8초(Opus)다. D-28 §5 의 최악(36 tokens/s 에 모든 파일이 128000 토큰)은 실측 속도 약 80 tokens/s 에서는 일어나지 않는다.

### 6. 누적 지출

- 누적 Anthropic 지출(손계산, 추정 누적): D-28 §8 의 약 $37.74 + Fable max rep-1~3 $33.71(11.752 + 10.291 + 11.668) = 약 $71.45, 여기에 Opus 5.5 max $8.98(§7)을 더해 $80.43 이다(항목을 소수 셋째 자리까지 합하면 80.436). D-28 §8 의 $37.75 는 Sonnet 을 $0.42 로 계산한 값이라 $0.41 로 고쳤다(§8). 상한 $200 의 40%(40.2%)이고 남은 몫은 약 $119.6 이다. 중단된 1차 시도의 과금(최대 $1~2 추정)은 기록이 없어 넣지 않았다. 채점기의 OpenAI 비용은 측정하지 못했다.
- D-28 §6 의 예상과 비교: Fable 1회 보통 범위 $10~15 에 세 rep 모두 들어갔다($10.29~11.75). Opus max 1회는 보통 범위 $3~5 를 넘었다($8.98. 출력이 xhigh 의 8.3배라서다). 4회 합은 $42.69 로 보통 범위 $33~50 안이다. 최악 추정(Fable $103, Opus $41)에는 한참 못 미쳤다.
- 진행 상태: 모두 끝났다. batch status(`max_batch.status.txt`)는 `fable-max-rep3` end 12:06:08 exit 0, `opus-max` 12:06:08~12:33:41 exit 0, batch done 12:33:41 이다. 실행 중인 프로세스는 없다.

### 7. 결과: Opus 5.5 effort max

`cmp-opus-5-5-d26-max`. 인자는 Fable 과 같고 모델만 `claude-opus-5-5` 다(§1). 시각은 batch status 로 12:06:08~12:33:41(exit 0), manifest 로 12:06:11~12:33:40 이다(3초 안팎 차이는 프로세스 시작과 manifest 기록 시점). manifest `agent`: model `claude-opus-5-5`, effort `max`, `max_tokens` 128000, `call_shape` `api-v6`, `timeout_s` 14500, `prompt_sha256` 앞 16자 `2d0bb457ca2c605a`, `scan_py_sha256` 앞 8자 `39c5a863`, `fingerprint_source` `snapshot`. 같은 D-26 규칙의 16파일을 모두 스캔했고(실패 0, 건너뜀 0) 16호출 모두 응답 모델이 `claude-opus-5-5`, `fallback` false 다. partial 행, 거절, 잘림, 폴백은 0 이다(`files_failed` 0 이라 ERROR 로 끝난 파일이 없다).

| 항목 | Opus 5.5 max | Opus 5.5 xhigh(`cmp-opus-5-5-d26`) | 배율 또는 차이 |
|---|---|---|---|
| 후보 | 30 | 26 | +4 |
| matched | 10 | 8 | +2 |
| partial | 2 | 0 | +2 |
| FP | 11 | 6 | +5 |
| FN | 20 | 22 | -2 |
| recall 엄격 / 관대 | 0.333 / 0.400 | 0.267 / 0.267 | +0.067 / +0.133 |
| precision | 0.476 | 0.571 | -0.095 |
| high | 2/3 | 2/3 | 같다 |
| scan_findings | 21 | 14 | +7 |
| 비용 | $8.98 | $1.32 | 6.8배 |
| 출력 토큰 | 434,350 | 52,623 | 8.3배 |
| 호출 최대 | 561.8초 | 142.7초 | 3.9배 |

`truths_unevaluated` 0, `judge_incomplete` 표시 없음, `scorer_stdout.log` 오류 0줄이다. 토큰: 입력 38,721, 캐시 쓰기 25,770(5,154 × 5회. Fable 은 4회였다), 캐시 읽기 56,694, 출력 434,350. 비용은 단가 4/20/0.20/5(per MTok)로 $8.98 이다(입력 $0.155, 출력 $8.687, 캐시 읽기 $0.011, 캐시 쓰기 $0.129). 파일당 출력 최대는 62,225 토큰(`contracts/SecondSwap_StepVesting.sol`, 128000 의 48.6%)이고 에이전트 단계의 프로젝트 wall 은 1,094.4초다. Fable max 와 비교하면 같은 16파일에서 출력은 2.1배(434,350 대 211,538)인데 비용은 $8.98 대 $11.24 로 낮다. 단가가 낮아서다. matched 10 은 Fable max 3회의 범위(10~17)의 하한이다.

Opus 는 설정마다 1회라 변동을 판단할 수 없다. Fable 의 변동(엄격 recall 표본 표준편차 0.120)에 비추면 Opus 의 +0.067(정답 2건)은 그 안이다(Opus 자신의 변동은 모른다).

### 8. 모델·effort 종합표 (secondswap 16파일, 정답 30건)

| 모델, effort (표본) | max_tokens | matched | recall 엄격 / 관대 | precision | high | 비용/회 | 출력 토큰/회 | 호출 최대 |
|---|---|---|---|---|---|---|---|---|
| Fable 5.1 max (3회 평균) | 128000 | 13.0 | 0.433 / 0.456 | 0.479 | 0.667 (평균 2.0/3) | $11.24 | 211,538 | 569.4초 |
| Fable 5.1 xhigh (3회 평균) | 64000 | 9.67 | 0.322 / 0.422 | 0.456 | 0.444 (평균 1.33/3) | $5.42 | 95,228 | 302.8초 |
| Opus 5.5 max (1회) | 128000 | 10 | 0.333 / 0.400 | 0.476 | 2/3 | $8.98 | 434,350 | 561.8초 |
| Opus 5.5 xhigh (1회) | 64000 | 8 | 0.267 / 0.267 | 0.571 | 2/3 | $1.32 | 52,623 | 142.7초 |
| Sonnet 5.5 high (1회) | 64000 | 6 | 0.200 / 0.267 | 0.400 | 1/3 | $0.41 | 27,781 | 55.0초 |
| Haiku 5.5 high (1회) | 64000 | 5 | 0.167 / 0.200 | 0.192 | 1/3 | $0.045 | 75,055 | 110.9초 |

3회 평균 행의 호출 최대는 rep 별 최댓값의 평균이다. `max_tokens` 는 각 run 의 `raw/scan.jsonl` 첫 줄 `_run` 값이다. 같은 모델에서 max 와 xhigh 는 effort 와 `max_tokens` 가 함께 다르다(§4). Fable 5.1 xhigh 행은 재채점본 rep-3 을 쓴 값이다(원본 rep-3 이면 matched 9.00, 엄격 recall 0.300, §4). 비용은 손계산(토큰 × 공식 단가)이고 Sonnet 의 $0.41 은 D-28 §8 의 정정값이다(처음 $0.42, 캐시 읽기를 $0.20 으로 계산했다. 공식 단가 $0.10 으로 38,721 × $2 + 20,616 × $2.5 + 61,848 × $0.10 + 27,781 × $10 per MTok = $0.413). Sonnet 과 Haiku 는 effort 를 바꿔 보지 않았으니 effort 비교에는 쓰지 않는다.

### 9. 해석

같은 모델에서 effort 를 올리면 recall 이 오르는 방향은 Fable 과 Opus 둘 다 같았지만 비용이 Fable 은 2.1배, Opus 는 6.8배다. 정답 30건 데이터셋에서 1건은 0.033 이라, Fable max 3회의 엄격 recall 표본 표준편차 0.12(정답 3.6건)는 모델과 effort 사이 차이와 같은 크기다(xhigh 에서 max 로 Fable 은 +0.111, Opus 는 +0.067). Fable 의 xhigh 대 max 는 Welch t 1.31, 양측 p 약 0.27 이라 확정이 아니다. 더 확정하려면 반복 횟수나 프로젝트 수를 늘려야 한다. 다음 후보는 다른 scabench 프로젝트 2~3개로 넓히는 것이다.

한계 하나. effort 와 `max_tokens` 가 함께 바뀌어(xhigh 64000, max 128000) effort 효과만 분리할 수 없다. xhigh 출력 최대 24,912 < 64,000 이라 잘림 0 이므로 영향은 작다고 보지만 추정이다(§4).

### 10. 검산 기록

원본(`runs/fable-5-1-d26-max-rep3/rep-1..3`, `runs/cmp-opus-5-5-d26-max`, xhigh 쪽 `fable-5-1-d26-rep3`, `fable-5-1-d26-rep3-r3nm`, `cmp-*`)의 `scores.json`, manifest, `raw/scan.jsonl`, `scorer_stdout.log` 에서 다시 계산했다. 읽기만 했다.

- 요청서 값과 맞은 것: rep-3 의 matched 10, partial 2, FP 17, FN 20, recall 0.333 / 0.400, precision 0.370, high 2/3, `truths_unevaluated` 0, scan_findings 27. 합본의 matched 13.0 ± 3.61, 엄격 0.433 ± 0.120, 관대 0.456 ± 0.096, precision 0.479 ± 0.108, high 0.667 ± 0.333. 비용 평균 $11.24, 출력 평균 211.5K, 호출 최대 평균 569초. 합본 `scores.json` 과 manifest 에 `judge_incomplete` 없음. 스캔은 세 rep 모두 16/16이고 폴백·거절·partial·잘림 0. Opus 의 16/16, 후보 30, $8.98, 출력 434,350, 호출 최대 561.8초, 파일당 출력 최대 62,225, matched 10, partial 2, FP 11, FN 20, recall 0.333 / 0.400, precision 0.476, high 2/3, unevaluated 0, scan_findings 21. xhigh 3회 평균(9.67, 0.322, 0.422, 0.456, 0.444, $5.42, 95,228, 302.8초)과 Opus xhigh 의 값. 누적 $71.45 + $8.98 = $80.43(40.2%. 처음 값은 $71.46 과 $80.44 였고 Sonnet 정정으로 바뀌었다).
- 어긋난 것: 호출 최대의 '약 1.7배'(§4). 1.9배나 2.0배로 고쳤다. 그 밖의 비율은 맞다(비용 2.1배, 출력 2.2배, Opus 출력 8.3배, 비용 6.8배).
- 요청서에 없던 것을 더한 것: Welch t 와 p(§4), Opus 와 Fable 의 캐시 쓰기 횟수 차이(§7), rep-3 의 `Connection error` 14줄과 `Request timed out` 0줄의 구분(§3).
- 확인하지 못한 것: rep-3 채점의 재개 시각 11:48(§2 가 적은 11:48:40 `truth_trace.json` 중간 쓰기 목격 그대로), 중단된 1차 시도의 과금, `scorer_stdout.log` 의 `Connection error` 14줄이 어떻게 재시도됐는지(채점기 코드를 안 읽었다).

### 11. 남은 위험과 다음 일

- 해소: Opus 5.5 의 effort `max` 와 `max_tokens` 128000 수락(§5). D-28 §11 의 첫 위험이 Fable 5.1 과 Opus 5.5 둘에서 해소됐다.
- 안 본 것: 폴백이 일어났을 때 128000 이 폴백 모델에서 유지되는지(폴백 0 이라 못 봄), Opus 의 128K 한도 자체(최대 62,225 토큰까지만 받음), 128000 을 다 쓴 호출(없었다).
- 표본: 프로젝트 1개(secondswap, 정답 30건)에 Opus, Sonnet, Haiku 는 1회씩이다. effort 의 효과도 Fable 3회까지로는 확정하지 못했다(§4). 다른 scabench 프로젝트 2~3개로 넓히거나 반복을 늘리는 것이 다음 후보다. 비용은 프로젝트 크기에 따라 다르다. secondswap 16파일 기준 Fable max 1회가 $11.24 라서 비슷한 규모 3개면 약 $34 라는 것이 추정이다(파일 수와 출력 길이에 비례한다고 본 값이고 근거는 이 한 프로젝트뿐이다). 남은 몫은 약 $119.6 이다.
- 사용자 결정 대기: 확장할 프로젝트와 반복 횟수, 상한 $200 유지 여부.

### 리뷰 후 수정 (2026-10-23)

D-29 기록(위 §1~§11)에 대한 리뷰가 낸 문서 지적 8건과 코드 권장 2건(R1, R2)을 반영했고 선택이던 R3 도 했다. 새 D 번호는 만들지 않았다. 번호는 리뷰의 항목 번호다. 실제 API 는 호출하지 않았고 `eval/runs/` 는 읽기만 했다. 수정 전 파일 7개는 저장소 밖 `C:\Users\정동욱\AppData\Local\Temp\at_backup_20261023c\` 에 있다. `internal/tools/scan.py` 는 건드리지 않았다.

| # | 지적 | 처리 |
|---|---|---|
| 1 | xhigh 와 max 는 `max_tokens` 도 달라서 effort 효과만 분리할 수 없다 | §4 비교 표 아래와 §9 에 그 문단을 더했고 §8 종합표에 `max_tokens` 열을 더했다. 값은 각 run 의 `raw/scan.jsonl` 첫 줄 `_run` 으로 다시 확인했다(Fable xhigh 3회, Opus xhigh, Sonnet, Haiku 는 64000, Fable max 3회와 Opus max 는 128000). 64000 으로 돈 run 의 호출당 출력 최대는 24,912 토큰(Fable xhigh rep-1)이라 상한에 닿은 호출이 없고, 그래서 영향은 작다고 보지만 추정이다. 반대쪽으로 max 의 Opus 출력 최대 62,225 토큰은 64,000 의 97% 라 64000 이었다면 닿았을 수 있다는 문장도 §4 에 더했다(추정). |
| 2 | xhigh 평균의 rep-3 가 재채점본이라는 점 | §4 비교 표의 matched 와 엄격 recall 행에 원본 rep-3 을 쓴 값을 병기했다(matched 9.00, 엄격 0.300, max 와의 차이 +4.0, +0.133). 재채점본(matched 7, partial 5)을 고른 이유는 원래 rep-3 의 채점이 OpenAI 호출 오류로 정답 17건 미평가였고 재채점이 그 복구여서라고 문단으로 적었다. §8 표 아래에도 한 문장. 원본 값은 `fable-5-1-d26-rep3/rep-1..3/scores.json` 으로 다시 계산했다(matched 12, 10, 5. 엄격 recall 0.400, 0.333, 0.167). |
| 3 | Sonnet 5.5 캐시 읽기 단가 | 공식 가격표(`https://platform.claude.com/docs/en/about-claude/pricing`, 2026-10-23 확인)에서 Sonnet 5.5 의 캐시 읽기는 $0.10(입력의 0.05배, 각주 2)이고 $0.20 은 Sonnet 5 의 값이다. 번들 claude-api 스킬의 캐시 표(2026-10-06)는 5.5 도 $0.20 으로 적어서 그쪽이 틀렸다. `cmp-sonnet-5-5-d26` 의 `_usage` 16행을 합산해 토큰(입력 38,721, 캐시 쓰기 20,616, 캐시 읽기 61,848, 출력 27,781)을 확인했고 $0.413 이다. 고친 곳: D-28 §8 의 표, 설명, 누적($37.75 에서 $37.74), D-28 §6 의 남은 몫($162.26), D-29 §6($71.46 에서 $71.45, 누적 $80.43), §8 표와 설명, §10 합계 줄, "아직 안 된 것" 비용 행, D-19 §4 단가 줄과 식의 읽기 배수(0.1 에서 0.05), 같은 오류를 든 D-20 L2 줄(707행). 누적은 항목을 센트 단위로 반올림해 더한 값이고 소수 셋째 자리까지 합하면 37.743 과 80.436 이다. |
| 4 | 4회 전부 최악 약 $348 | 입력과 캐시를 뺀 값이었다. 3 × $103.06 + $41.26 = $350.44 이라 약 $350 으로 고쳤다(Fable 입력·캐시 $0.660, Opus 입력·캐시 $0.295 는 `raw/scan.jsonl` 의 토큰으로 다시 계산. Opus 는 입력 38,721, 캐시 쓰기 25,770(5회), 캐시 읽기 56,694 토큰이고 처음 $0.270 으로 적은 것을 2026-10-23 재검토에서 고쳤다. 이 때문에 Opus 최악은 $41.23 에서 $41.26, 4회 합은 $350.41 에서 $350.44 가 됐고 "약 $350" 결론은 그대로다). D-28 §6 표와 본문, §11, "아직 안 된 것" 비용 행. 표에는 입력을 뺀 $348 의 계산을 남겼다. |
| 5 | rep-3 재개 시각 11:48 의 근거 | §2, §3, §10 의 세 곳. 근거를 'scoring_output 폴더가 11:48 에 갱신됨' 에서 '코디네이터가 11:48:40 에 본 `truth_trace.json`(41바이트)의 중간 쓰기' 로 바꿨다. 이 기록에서 확인하지 못한 코디네이터 보고다. 지금 파일 시각이 폴더 12:06:07, 최종 `truth_trace.json`(203바이트) 12:06:06 인 것은 이번에 확인했다. |
| 6 | README 의 '둘 다 비례' | `MAX_CALL_S`(어댑터)와 `CALL_TIMEOUT_S`(scan.py 상수)는 1800 그대로이고 비례하는 것은 scan.py 의 `call_cap()`(97~104행, 열어서 확인)과 어댑터의 `max_call_s()` 반환값이라고 고쳤다. |
| 7 | attacktrail.py 주석의 4라운드 시작 시각 | 52~53행 주석에 호출 상한 3600, 소프트 여유 3615, 짧은 timeout 경고 기준 7230 의 라벨을 붙였다(두 줄 그대로). 80~83행 주석은 `--timeout 14500` 에서 4라운드가 36 tok/s 면 10,667초에 시작해 14,222초에, 호출당 3,600초면 10,800초에 시작해 14,400초에 끝난다고 병기했다(한 줄 늘었다). 코드는 그대로다. 같은 서술이 D-28 §5 에도 있어 같게 고쳤다(요청 밖이지만 같은 오류). |
| 8 | D-28 정정 소절 1행의 README 예시 서술 | 그 행에 '코드 후속에서 변경됨' 을 더했다. 최종 README 복구 예시는 `--model`·`--effort`·`--max-tokens` 세 옵션을 뺐고 본문의 `--max-tokens` 예시와 기본값 경고는 최종 README 에 없다고 적었다. |
| R1 | `_run` 값을 못 찾을 때 말없이 넘어감 | `_check_logged_opts` 가 선택한 모든 프로젝트의 `_run` 에서 값을 못 찾은 키를 모아(한 프로젝트라도 그 키를 기록했으면 그 값으로 채우고 경고하지 않는다. 모든 선택 프로젝트에서 그 키가 없을 때만 경고한다), 하나라도 있으면 stderr 에 `rescore: <run 폴더 이름> 의 _run 행에서 <키> 를 찾지 못해 인자 또는 기본값으로 기록한다` 한 줄을 낸다. 키는 못 찾은 것만 `/` 로 잇는다(전부면 `model/effort/max_tokens`, D-28 전 run 은 `max_tokens`). attacktrail API 어댑터에서만 불리므로 CLI 어댑터와 baseline 은 안 낸다. 채움과 거부 동작은 그대로다. 시험은 `test_rescore_says_on_stderr_which_keys_the_logged_run_row_did_not_give`(`raw` 없음은 세 키, `max_tokens` 없는 옛 행은 한 키, 전부 있으면 줄 없음, CLI 어댑터는 줄 없음). 기존 `test_rescore_prints_the_run_summary_on_stderr_and_keeps_stdout_empty` 는 stderr 를 줄 단위로 통째 비교하고 그 run 에 `raw/scan.jsonl` 이 없어서, 새 줄이 맨 앞에 붙은 기대값으로 고쳤다. |
| R2 | 양쪽 다 미기록이면 경고가 없어 독스트링('unknown is not equal')과 어긋남 | `_agent_diff` 가 `AGENT_KEYS` 마다 양쪽 다 기록이 없으면 `agent.<키>: 양쪽 미기록` 한 줄을 낸다. 한쪽만 없거나 값이 다르면 전처럼 `differs`. 시험은 `test_compare_warns_for_each_agent_setting_neither_manifest_recorded`(fixtures 의 baseline agent 는 model 만 있어서 effort, max_tokens, call_shape 세 줄이 뜨고 model 은 안 뜬다). `test_compare_warns_per_agent_setting_that_differs_and_only_for_those` 의 마지막 단언('양쪽 모르면 말할 것 없음')은 경고 한 줄로 바꿨고, `test_compare_detects_regression_and_new_catch` 는 '경고 없음' 을 보려고 먼저 네 설정을 맞추게 했다. 부작용으로 baseline 끼리 비교하면 세 줄이 항상 뜬다. README compare 절과 "아직 안 된 것" 의 compare 행에 적었다. |
| R3 | 프로젝트 간 불일치 거부의 rescore 종단 시험 | `test_rescore_refuses_a_run_whose_projects_logged_different_values_before_writing_anything`. 데이터셋 프로젝트 둘의 `_run` 이 effort max 와 xhigh 로 다르면 `rescore()` 가 ValueError(두 값과 프로젝트가 메시지에 있음)를 내고, `cli.main` 이 `재채점 중단: ...` 과 exit 1 을 내며 manifest, scores 는 안 써진다. 이 시험은 수정 전 코드로도 통과한다(이미 있던 동작을 종단으로 고정). |

시험(실제 API 호출 없음. 메모리 때문에 요청대로 두 파일만 한 파일씩 돌렸다). `test_runner.py` 40건 통과(38 + 새 2건), `test_report.py` 35건 통과(34 + 새 1건). 다른 시험 파일은 이번에 돌리지 않았다. 읽어서 영향을 확인한 것: `test_cli.py` 는 `rescore` 를 가짜 함수로 바꿔 부르므로 R1 의 stderr 줄과 무관하고, `test_adapters.py` 는 attacktrail.py 의 주석만 바뀌었다(파싱 확인). 변이 확인(백업의 옛 파일을 잠깐 넣고 시험을 돌린 뒤 복구, 복구 뒤 SHA256 이 같음): 옛 `compare.py` 면 R2 시험 2건이, 옛 `run.py` 면 R1 시험 2건이 실패한다.

```
pytest tests/test_runner.py   40건 통과 (38 + 2)
pytest tests/test_report.py   35건 통과 (34 + 1)
scan.py SHA256                39C5A86388279B0367BAC62747213B0DF04D56601BEA5DF419513D0F7BA45376 (D-28 §3 과 같음)
CRLF                          DECISIONS.md, runner/run.py, report/compare.py 의 LF 단독 줄 0 (README.md, attacktrail.py, 시험 파일은 원래 LF)
em대시, 별표 두 개 표기      DECISIONS.md 는 13개, 49개 그대로(새 문단에 추가 없음. 새 문단의 굵은 글씨 0)
```

남은 위험.
- R2 의 `양쪽 미기록` 은 baseline 끼리 비교할 때도 effort, max_tokens, call_shape 세 줄을 낸다. baseline 에 해당 없는 설정이라 잡음이다. 문서에 적었고 조건을 좁히지는 않았다(에이전트 종류별 예외를 두면 규칙이 복잡해진다).
- R1 의 줄은 현재 `eval\runs` 의 run 에는 뜨지 않는다. 그 run 들의 `raw\scan.jsonl` `_run` 행에는 model, effort, max_tokens 가 모두 있고(2026-10-21 pilot 도 64000 을 기록했다), D-28 전 run 에서 None 인 것은 manifest 의 `agent.max_tokens` 뿐이다. 뜨는 경우는 `raw\scan.jsonl` 이 없을 때와 `_run` 에 키가 없는 산출물이다. 그때 manifest 값은 인자나 scan.py 기본값이라 실제와 다를 수 있다는 경고이고 의도한 동작이다. 처음에는 "D-28 전 run 을 rescore 할 때마다 `max_tokens` 로 뜬다" 고 적었는데 그 전제가 틀렸다(2026-10-23 재검토에서 정정).
- README 44행 끝의 'Opus 5.5 는 아직 모른다' 는 D-29 §5(Opus 5.5 도 effort max 와 128000 요청을 받음)와 어긋난다. 이 수정의 범위 밖이라 고치지 않았다.
- 누적 지출 $80.43 은 항목을 센트로 반올림해 더한 값이다. 정밀 합은 $80.436 이다.

## D-30. 스캐너가 디렉터리를 파일로 집던 버그와 빌드 산출물 폴더 제외, 확장 배치 채점 실패 (2026-10-25)

범위는 `internal/tools/scan.py`(`select_files`, `SKIP_DIRS`, `SKIP_DIRS_WEB`, selftest), `eval/tests/test_scan_select.py`, 이 문서, README 다. 실제 API 는 호출하지 않았다. 시스템 프롬프트 본문, `SCHEMA`, 후보 검증, 호출 형태, 시간 설계, 모델, effort, `max_tokens` 는 그대로다. 수정 전 파일은 `%TEMP%\at_backup_20261025a\` 에 있다.

### 1. 배경 (실측)

2026-10-23~24 확장 배치(`runs/exp3-fable-max`, `runs/exp3-opus-max`)에서 `code4rena_kinetiq_2025_07` 이 파일 25개 중 17개 스캔, 8개 ERROR 였다. 두 run 의 manifest 가 같다(`files_total` 25, `files_scanned` 17, `files_failed` 8). 8개는 Foundry 빌드 산출물 `out/KHYPE.sol/`, `out/IOracleManager.sol/` 등 디렉터리다(`Get-Item` 의 `PSIsContainer` 가 True). 이름이 `.sol` 로 끝나서 `select_files` 가 파일로 집었고, scan.py 가 열다가 `[Errno 13] Permission denied` 가 났다. 실제 소스 17개는 정상이다.

`select_files` 는 `glob` 결과를 확장자 문자열로만 걸렀고 파일인지 확인하지 않았다. 같은 증상이 kinetiq 밖에도 있다. 29개 체크아웃 전체에서 디렉터리를 집는 곳은 비웹 21개, `--web` 13개였다(아래 표).

### 2. 수정

- `select_files` 가 후보를 `os.path.isfile` 로 거른다(비웹·웹 공통). 확장자와 제외 폴더 검사 뒤에 둬서 stat 호출은 후보에만 간다. 디렉터리 안의 진짜 파일(`src/Nested.sol/Inner.sol`)은 그대로 선택된다.
- 비웹 `SKIP_DIRS` 에 `out artifacts cache build dist typechain typechain-types` 를 더했다. 비교 방식은 D-26 과 같다(root 기준 상대경로의 디렉터리 이름을 하나씩, 깊이 무관, 소문자 정확 일치).
- `SKIP_DIRS_WEB` 에는 `dist build out` 이 이미 있어서 `artifacts cache typechain typechain-types` 만 더했다.
- 시험: `test_scan_select.py` 에 "디렉터리 이름이 `.sol` 로 끝나도 제외"(웹·비웹) 1건, "빌드 산출물 폴더 제외"(7개 이름 × 웹·비웹, 파라미터화) 1건을 더했다. 기존 `test_non_web_rules_unchanged` 는 비웹이 `dist/app.js` 를 남긴다고 단언했는데 이 결정으로 달라져서 `app.js` 로 바꿨고, 고정 목록 시험(`test_non_web_skip_list_is_pinned`)은 새 7개를 포함하도록 고쳤다. selftest 에 같은 검사 2건을 더했다(140건).

### 3. 29개 체크아웃 검증 (D-26 방식)

29개 `datasets/sources/` 체크아웃에 수정 전·후 `select_files` 를 비웹·`--web` 각각 돌려 파일 목록을 비교했다. 정답 쪽은 D-26 §2 와 같은 방법이다(정답 555건의 title·description 에서 파일 경로 또는 파일명, `<폴더>/` 경로 토큰 검색).

폴더 이름 추가로 새로 빠지는 실제 파일(디렉터리 항목 제외):

| 폴더 | 비웹 빠지는 파일 | 프로젝트 | `--web` 빠지는 파일 | 정답 글에서 파일 이름·경로로 든 것 | `<폴더>/` 토큰 |
|---|---|---|---|---|---|
| `out` | 0 | | 0 | 0 | 0 |
| `artifacts` | 0 | | 0 | 0 | 0 |
| `cache` | 0 | | 0 | 0 | 0 |
| `build` | 1 (`build/lint.go`) | sherlock_morph-l-2_2024_09 | 0 | 0 | 0 |
| `dist` | 0 | | 0 | 0 | 0 |
| `typechain` | 0 | | 0 | 0 | 0 |
| `typechain-types` | 0 | | 0 | 0 | 0 |

`build/lint.go` 는 morph-l2 의 Go 린트 도구이고 정답이 언급하지 않는다. 취약 위치가 이 폴더에 있는 정답은 0건이라 7개를 전부 넣었다. 빼야 할 이름은 없다.

`os.path.isfile` 로 빠지는 디렉터리 항목:

| 프로젝트 | 비웹 | `--web` | 디렉터리 이름 |
|---|---|---|---|
| code4rena_kinetiq_2025_07 | 8 | 0 | `out/*.sol/` (`--web` 은 `out` 이 원래 제외) |
| code4rena_lambowin_2025_02 | 8 | 8 | `broadcast/*.s.sol/` |
| sherlock_crestal-network_2025_03 | 5 | 5 | `broadcast/*.s.sol/` (그중 `UpgradeV2.s.sol/Upgrade.s.sol` 도 디렉터리) |
| 합계 | 21 | 13 | |

파일 수 전후:

| 구분 | 수정 전 | 수정 후 |
|---|---|---|
| 비웹, 29개 합 | 2,141 | 2,119 (디렉터리 21 + 파일 1) |
| `--web`, 29개 합 | 3,383 | 3,370 (디렉터리 13) |

바뀌는 프로젝트는 비웹이 kinetiq 25 -> 17, lambowin 41 -> 33, crestal-network 19 -> 14, morph-l2 388 -> 387 이고, `--web` 은 lambowin 53 -> 45, crestal-network 34 -> 29 다. 나머지 25개의 파일 집합은 비웹·웹 모두 같다. 웹 파일 집합이 `artifacts cache typechain typechain-types` 추가 때문에 바뀐 곳은 0개고, 위 `--web` 변화는 전부 `isfile` 때문이다. `broadcast/` 는 Foundry 배포 로그 폴더이고 `SKIP_DIRS` 에 넣지 않았다. 그 안의 `.sol` 이름 항목이 전부 디렉터리라 `isfile` 만으로 닿는 파일이 없다.

secondswap 은 비웹 16개 그대로다(D-26 §4 와 같은 목록).

### 4. 지문

| | 수정 전 (exp3 manifest) | 수정 후 (live scan.py) |
|---|---|---|
| `prompt_sha256` 앞 16자 | `2d0bb457ca2c605a` | `2d0bb457ca2c605a` |
| `scan_py_sha256` | `39c5a86388279b0367bac62747213b0df04d56601bea5df419513d0f7ba45376` | `ad2ae93e1d34a9f0cf9481855a1e2458b770439da91ddfb7807bbbdfc5bd1915` |
| `call_shape` | `api-v6` | `api-v6` |

이전 run 과 파일 집합이 달라지는 프로젝트는 `code4rena_kinetiq_2025_07` 하나다(exp3-fable-max, exp3-opus-max 의 25개가 17개가 된다). secondswap 계열 run(pilot, cmp, fable-5-1-d26 등), axion, tally, superposition, cork, next-generation 은 같다. lambowin, crestal-network, morph-l2 는 지금까지 어떤 run 에도 안 쓰였다. `compare` 는 파일 집합 차이를 경고하지 않으므로(D-26 "아직 안 된 것" 표) 이후 run 과 exp3 를 비교할 때 `files_total` 을 사람이 대조한다.

### 5. 영향

exp3 두 run 의 kinetiq `files_total` 25 와 `files_failed` 8 은 수정 전 값이다. 실제 소스는 17개이고 커버리지는 17/17 로 읽는다. 8건의 ERROR 는 모델이 못 읽은 파일이 아니라 디렉터리 항목이었다. ERROR 줄은 `Permission denied: '<디렉터리 경로>'` 이고 파일을 여는 단계의 오류라서 그 8건에 모델 호출은 없었다(`projects/code4rena_kinetiq_2025_07/raw/scan.stdout.txt` 의 `[1/25]` 줄). 수정 후 재실행은 하지 않았다. 두 manifest 는 그대로 두고 이 문서가 설명한다.

### 6. 확장 배치 채점 실패

2026-10-24 OpenAI 가 `o4-mini` 를 폐기해(404 deprecated) exp3 두 run 의 채점 호출이 전부 실패했다. 두 manifest 의 `truths_unevaluated` 가 kinetiq 25, cork 18, next-generation 14 로 합 57 이고 정답 57건이 전부 미평가다. `judge_incomplete` 가 true 로 켜졌다. D-28 §9 에서 만든 경고가 실제로 동작한 첫 사례다. 지표(recall)는 이 run 에서 의미가 없다.

채점기 `SUPPORTED_MODELS` 중 가용은 `gpt-5-nano-2025-08-07`, `gpt-5-2025-08-07`, `o3-2025-04-16` 이고 `o4-mini`, `gpt-4.1-nano-2025-04-14` 는 폐기됐다(2026-10-25 확인). 재채점은 사본 `runs/exp3-fable-max-nm2`, `runs/exp3-opus-max-nm2` 에서 진행한다. judge 모델은 사용자 결정을 기다린다. `--judge-model` 기본값 `o4-mini` 는 README 에 "폐기됨, 변경 예정"으로 적었고 `cli.py` 기본값 변경은 다음 작업이다.

### 7. 검증

- `scan.py --selftest` 와 `python -O scan.py --selftest`: 둘 다 `selftest OK (140 checks)`.
- `pytest tests/test_scan_select.py`: 24건 통과. `tests/test_scan_parse.py`: 74건 통과.
- secondswap 비웹 16 -> 16, kinetiq 비웹 25 -> 17.
- DECISIONS.md CRLF 보존, scan.py·README·시험 파일은 LF 그대로.

### 8. 한계

- 이름 규칙이라 `build`, `cache`, `out` 이 실제 소스 폴더인 저장소(예: Go 의 `pkg/cache/`, 웹 앱의 `src/cache/`)는 소스까지 빠진다. 29개에서는 morph-l2 의 `build/lint.go` 1개뿐이었고 `--web` 은 0개였지만, 29개 밖은 검증하지 않았다. 웹 정답셋이 없어 `--web` 의 `artifacts cache typechain typechain-types` 추가는 효과를 측정하지 못했다.
- 안 보는 것은 D-26 과 같다(대문자 폴더, 접두 일치, 파일 이름 패턴).
- `broadcast/` 폴더 자체는 제외하지 않았다. 안에 `.sol` 파일이 들어 있는 저장소가 있으면 선택된다. 29개에서는 없었다.

## 아직 안 된 것

D-30 에서 해결한 것: 디렉터리가 `.sol` 로 끝나는 이름으로 파일 후보에 들던 것(kinetiq 의 `out/*.sol/` 8건 ERROR, lambowin·crestal-network 의 `broadcast/*.s.sol/` 도 같은 증상), 빌드 산출물 폴더(`out artifacts cache build dist typechain typechain-types`)를 비웹 스캔이 보내던 것. 확장 배치의 채점 실패(`o4-mini` 폐기)는 아직 안 풀렸다(아래 표 마지막 행).

D-28 재검증 후 수정(2026-10-23)에서 해결한 것: 문서 쪽 지적(복구 절차 예시, `call_shape` 근거 문장, 비용 문구, `judge_incomplete` 문구, 36 tok/s 산수, 서버 수락 표기, 굵은 글씨). 코드 쪽 5건은 같은 날 뒤따른 작업('코드 수정 후속')에서 해결해서 맨 끝 네 행을 이 문단으로 옮겼다. `eval rescore` 가 원래 run 의 `_run` 행과 인자를 대조하고 안 준 값을 채운다(1). `call_shape` 주석을 새 규칙으로 고치고 `compare` 가 model, effort, max_tokens, call_shape 차이를 경고한다(2). `judge_incomplete` 에 `n_findings > 0` 조건을 더하고 줄 문구를 '채점 로그 확인' 으로 바꿨다(4). attacktrail.py 의 36 tok/s 주석을 두 경우(4개, 8개)로 고쳤다(5). `SOFT_MARGIN_S` 와 주석에 64000 기준값임을 밝혔다(7). 남은 것은 아래 행에 따로 적었다(`compare` 의 `prompt_sha256`·파일 집합, 매칭 소진 구분, 모델·effort 비교의 표본 크기). effort max, 128000 실행 행은 D-29 로 갱신했다. D-29 에서 해결한 것: effort `max` 와 `max_tokens` 128000 의 서버 수락(Fable 5.1, Opus 5.5 둘 다), Fable max 3회와 Opus max 1회의 결과 기록.

D-28 에서 해결한 것: effort `max` 와 `--max-tokens`(1~128000)를 열고 호출별 시간 상수를 `max_tokens` 에 비례시켰다(§1, §5). D-27 리뷰 잔여 결함 7건(§7), 모델 비교와 Fable xhigh 3회 결과의 기록과 rep-3 오염 정정(§8), 채점 불완전 표시(§9). 표의 해당 행을 고쳤고 맨 끝 두 행은 D-28 에서 더한 것이다.

D-27 에서 해결한 것: 선택 파일 0개 프로젝트가 `crashed` 로 기록되어 정답이 분모에서 말없이 빠지던 것(`no_files` 상태로 분리, 지원 밖 프로젝트의 정답 수와 전체 포함 Recall 을 scores.json·manifest·리포트·콘솔 요약에 남김, compare 경고). 표의 선택 파일 0개 행을 고쳤고 그 아래 행(재채점·재실행 이후)은 D-27 에서 더한 것이다. `compare` 행은 `no_files` 집합 경고를 덧붙였다.

D-26 에서 해결한 것: 비웹 스캔이 테스트·배포·목 폴더(`test tests ignition script scripts deploy mocks`)까지 보내던 것(본 실행 출력의 49.2%), manifest 지문이 run 시작의 스냅샷이 아니라 manifest 를 쓰는 시점의 라이브 scan.py 를 가리키던 것(`eval rescore` 경로도 같은 날 후속으로 `adopt_snapshot` 이 닫았다, §5). 맨 끝 두 행은 D-26 에서 더한 것이다. `compare` 행은 `no_files` 집합 경고를 덧붙였다(파일 집합 차이 경고는 없다).

D-25 에서 해결한 것: 기본 모델을 `claude-fable-5-1` 로(Mythos 5.1 은 이 조직에서 404), 무응답 상한 1800초와 `--deadline-s` 없을 때의 호출별 timeout 명시, 호출 시간 로그의 첫 이벤트·최대 간격, partial usage 의 문구 정정과 폴백 표시, SSE error 이벤트의 치명 타입, 완료 직전 close 경합, 짧은 하네스 타임아웃 경고, selftest 건수 카운터, 문서의 Glasswing·404 표현. 표의 해당 행을 고쳤고 맨 끝 행은 D-25 에서 더한 것이다. 선행 5파일 실행(2026-10-21) 뒤에 호출 timeout 설계 행을 "결정됨"으로 바꿨다.

D-24 에서 해결한 것: 기본 effort 를 xhigh 로, 기본 모델을 Mythos 5.1 로(사용자 결정. 모델은 D-25 에서 Fable 5.1 로 바뀌었다), `max_tokens` 64000, 스트리밍 호출, 스트림 도중 오류의 분류와 부분 usage, 데드라인에 스트림을 닫기, 호출 시간 수치의 재설정(추정, 아래 표), 키 변수 별칭, D-23 리뷰 지적 7건. 표의 해당 행을 고쳤고 맨 끝 여섯 행은 D-24 에서 더한 것이다.

D-23 에서 해결한 것: 모델 선택(`claude-sonnet-5-5`), effort 명시, 캐시 TTL(`5m`), 서버 측 폴백 켜기(실동작은 미확인, 표의 해당 행), 부분 실패·crashed 프로젝트·폴백 파일의 콘솔 요약. 표의 해당 행을 고쳤고 맨 끝 행은 D-23 에서 더한 것이다.

D-21 에서 해결한 것: API 경로 Ctrl-C 후 프로세스가 호출을 기다리며 사는 문제(H-1), CLI 어댑터의 Ctrl-C(H-2, 러너 쪽은 아래 표), 메모만 있는 프로젝트가 부분 실패로 보이던 것(M-3), 감시 경합과 버린 호출 수 계산(관찰 2, L-1), 버린 호출의 토큰 미집계 표시(M-1), 호출 시간 로그(H-3 b), 시험 시간 여유(M-5), 문서 정정(M-6). 표 끝의 행은 D-21 에서 더한 것이다.

D-20 에서 해결한 것: 호출별 timeout 과 하드 데드라인(D-19 의 한계), 402 치명 처리, 연속 API 오류 차단기, 부분 실패의 커버리지 기록·표시, 헤더 `prompt_sha256` 식, CLI 어댑터 파일 읽기 오류, `_usage` 의 service_tier 등과 CLI 원값 키, 후보 안 내부 키, `verify()` 읽기 오류, git 커밋 실패 경고, 에이전트 스냅샷(M9 완화). 남은 것:

| 항목 | 이유 |
|---|---|
| 진짜 nethermind 판정자 실행 | OPENAI_API_KEY 없음 |
| AttackTrail 실제 스캔 | ANTHROPIC_API_KEY 없음 (anthropic 1.15.0 은 2026-10-19 eval/.venv 에 설치, D-17) |
| effort 수준과 max_tokens 64000 한도 | 기본은 Fable 5.1 + xhigh + 64000 이다(D-24 사용자 결정, 모델은 D-25). 어느 effort 가 충분한지, xhigh 에서 사고가 64000 을 쓰는지, 실제 평균 출력 토큰은 실측 안 됨. 문서는 xhigh 에서 긴 산출물의 출력이 약 2배가 될 수 있다고 한다. 모자란 파일은 `stop_reason=max_tokens` ERROR 로 남는다. 문서가 권하는 사용자 메시지 끝 안내문은 프롬프트 틀을 바꾸므로 넣지 않았고 파일럿 뒤에 검토한다. D-28: effort `max` 와 `--max-tokens`(1~128000)를 열었다. D-29: max + 128000 으로 돌렸고 파일당 최대 출력은 Fable max 3회에서 56,058 토큰, Opus max 에서 62,225 토큰(128000 의 44%, 49%)이었다. 128000 을 다 쓴 호출은 없다. 어느 effort 가 충분한지는 반복이 모자라 못 정했다 (D-17, D-19, D-23, D-24 §2, D-28, D-29 §3, §7, §9) |
| 서버 측 폴백의 실동작 (D-23 에서 켬) | `fallbacks="default"` 로 거절된 요청을 `claude-opus-4-8` 또는 `claude-opus-5` 가 다시 받는다(범주별 라우팅은 비공개, 일부 범주는 폴백 없음). 키가 없어 서버가 요청을 받아들이는지, 폴백이 실제로 도는지, 거절·폴백 빈도, sticky 라우팅이 파일 사이에 걸리는지는 확인하지 못했다. 폴백 모델은 Fable 5.1 의 thinking 블록 없이 돌아서 폴백 파일의 후보 품질이 다를 수 있다 (D-23 §7, D-24 §2) |
| `scoped_env` 가 프록시·CA 변수를 안 넘김 | `HTTPS_PROXY`, `HTTP_PROXY`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` 등을 scan.py 에 넘기지 않는다. 사내 프록시나 사설 CA 환경에서는 모든 호출이 연결 오류로 실패할 수 있다(전부 실패면 `crashed`). 그런 환경에서 돌릴 때 화이트리스트에 추가해야 한다 |
| 부분 실패 프로젝트의 분모 정책 | 일부 파일이 실패해도 `status ok` 라 모든 분모에 들어간다. 리포트에 "부분 실패"와 프로젝트별 커버리지(`40/53`)로 보이게만 했다(D-19, D-20 §2). 분모에 넣을지는 사용자 결정 |
| `cost_usd` 미계산 | 단가 확정 전. 토큰은 기록·합산된다(D-19 §4). 계산식도 거기 있다 |
| repeat 의 프로젝트별 평균·표준편차 | 헤드라인만 집계됨 |
| `--from-manifest` 재실행 | 미구현. 지금은 manifest 의 `cli_argv` 로 손으로 재실행 |
| 커밋이 `main`·짧은 sha 인 프로젝트의 체크아웃 경로 | 29개 측정에서 동작은 했으나 전용 테스트 없음 |
| Docker 실행기 | 거부 스텁. PoC 실행 전 필수 |
| 모델 선택 | 결정됨(D-25): `claude-fable-5-1`(공개 모델). D-24 의 `claude-mythos-5-1` 은 이 조직에서 404 라 쓸 수 없다. 이 모델에 30일 데이터 보존 요건이 걸리는지는 확인하지 못했다(걸리면 400 "...must have data retention enabled", 8건 연속이면 차단기). Fable 5.1 은 사이버보안 안전 분류기가 있어 정당한 보안 작업도 거절될 수 있다(폴백이 받는다). 첫 기준선은 `files_fallback` 비율이다(D-25 §1). `claude -p --model claude-fable-5-1` 을 CLI 가 받는지도 미확인이다 |
| 병렬 시작 중복 쓰기 | 캐시 TTL 은 D-23 에서 `5m` 로 결정했다(Fable 5.1 쓰기 $12.50, 읽기 $0.25). 병렬 시작에서 처음 workers 개 요청이 각자 기준 문서를 쓰는 중복은 그대로다. 첫 요청 뒤 나머지를 병렬로 내는 fan-out 이 문서가 권하는 대안이고, `max_tokens: 0` 예열은 구조화 출력과 함께 쓰면 400 이라 못 쓴다 선행 5파일(D-25, 2026-10-21)에서 실측했다. workers 4 가 동시에 시작해 첫 라운드 4건이 모두 캐시 쓰기(5,154 토큰 × 4)를 했고 5번째 파일만 읽기였다. 캐시 쓰기 합이 $0.26 이라 영향은 작았다. 36 파일 본 실행도 첫 라운드 4회 쓰기 뒤 읽기로 바뀔 것이다. 첫 호출이 끝난 뒤 나머지 워커를 시작하는 개선은 안 했다. (D-20 §10) |
| 시간 상수의 실측 | 선행 5파일(D-25, 2026-10-21)의 실측을 보고 `MAX_CALL_S` 1800, 소프트 여유 1815초, API 기본 프로젝트 timeout 10800, `CALL_TIMEOUT_S` 1800 은 유지하기로 결정됐다(아래 호출 timeout 설계 행). 그대로 둔 `MIN_RETRY_ATTEMPT_S` 120, 연속 오류 8건, 어댑터 여유 15초(`DEADLINE_MARGIN_S`)는 실제 API 응답 시간 없이 정한 값이고 이번 실행으로도 못 쟀다. `--jobs` 기본 2(동시 8호출)의 속도 한도도 못 봤다. 가정했던 출력 속도 약 36 tokens/s 는 실측으로 약 74~77 tokens/s 였다. 부하 속 종료 지연과 실제 Ctrl-C 키 입력도 측정하지 못했다. D-28: `--max-tokens` 가 64000 보다 크면 `MAX_CALL_S`·`CALL_TIMEOUT_S` 가 비례해 커진다(128000 에서 3600, 소프트 여유 3615). 기본 timeout 10800 은 유지했다. 모든 파일이 128000 토큰을 쓰는 최악이면 16파일 run 에서 36 tokens/s 는 4라운드 파일 4개, 호출당 3,600초(약 35.6 tokens/s 이하)는 3, 4라운드 파일 8개가 건너뛰어진다(`--timeout 14500` 이 둘 다 덮는다. D-28 재검증 후 수정). 128000 토큰을 다 쓴 호출은 아직 없다. `max_tokens` 128000 으로 낸 48호출(D-29)의 최대 출력은 56,058 토큰(44%), 최장 672.7초, 60초 넘는 호출의 속도 중앙값은 약 80 tokens/s 였다. Opus 5.5 의 16호출은 최대 출력 62,225 토큰(49%), 최장 561.8초였다 (D-20 §1, D-24 §5, D-25 선행 5파일 소절, D-28 §5, D-29 §3, §7) |
| 에이전트 스냅샷은 `eval/runs/` 안에서만 커밋을 막는다 | `--out` 을 그 밖으로 주면 내부 기준 문서 사본이 든 폴더를 사람이 관리해야 한다. (D-20 §8) |
| 웹 정답셋 | 웹 대상 데이터셋이 없어 `--web` 확장의 효과와 `--web` 의 캐시·시간 영향은 측정하지 못했다 (D-16, D-20 §10) |
| 호출 timeout 설계 실측 (H-3) | 결정됨(D-25, 실측 근거). 선행 5파일(2026-10-21)에서 최장 호출 293.3초, 최장 이벤트 간격 234.2초, 출력 최대 22,307 토큰이었다. 사용자가 `CALL_TIMEOUT_S` 1800, `MAX_CALL_S` 1800, API 기본 프로젝트 timeout 10800, `max_tokens` 64000 을 유지하기로 했다(실측의 6배 이상 여유). 스트리밍에서 `CALL_TIMEOUT_S` 는 총시간이 아니라 무응답의 상한이다(D-24). 남은 것: 생각하는 동안 ping 이나 바이트가 오는지는 여전히 모른다(아래 생각 중 ping·바이트 행). 응답이 없는 호출은 `_usage` 행이 없어 `ERROR` 줄의 사유로만 본다. 실제 재시도 횟수는 기록하지 않는다 (D-21 §7, D-24 §5, D-25 선행 5파일 소절) |
| 짧은 하네스 타임아웃의 시간 정책 | 타임아웃이 263초 미만이면 첫 호출의 남은 시간이 248초 아래라 재시도가 0 이 된다. 30초 이하면 소프트 예산이 데드라인과 같아져(둘 다 timeout/2) 마지막 호출이 과금 후 잘릴 수 있다(블랙박스 관찰 1). D-24 부터는 소프트 여유가 1815초라, API 어댑터에 `--timeout` 을 3,630초 미만으로 주면(하한 timeout/2 가 먼저 걸림) 소프트 한도 직전에 시작한 최악 호출(1800초)이 하드 데드라인 안에 끝난다는 보장이 없다. 그런 값이면 어댑터가 stderr 에 경고 한 줄을 내고 실행은 막지 않는다(D-25 항목 6). API 어댑터 기본 10800 에서는 해당 없다. CLI 어댑터와 baseline 의 기본 1800 은 이 소프트·하드 계산을 쓰지 않는다 (D-21 §9) |
| 어댑터 여유 15초의 근거 범위 (L-2) | 15초의 근거(측정한 종료 지연 최대 2.7초의 약 5.5배)는 하네스 타임아웃이 30초 이상일 때만 성립한다. 30초 미만이면 데드라인이 timeout/2 라 여유가 15초보다 작다 (D-20 §1) |
| 순서 쓰기와 외부 강제 종료 (M-2) | 결과를 파일 순서로 쓴다. 데드라인 경로는 끝난 결과를 먼저 쓰지만, 외부 강제 종료나 크래시 때는 앞 파일이 매달린 동안 뒤 파일의 완료 결과가 메모리에만 있다가 사라질 수 있다. 완료 순서로 임시 파일에 쓰고 병합하는 방식은 구현하지 않았다 |
| `rescore` 시험과 동작 (M-4) | `rescore` 시험은 스냅샷 지문 한 건(D-26 후속), 요약 한 건(D-28), `_run` 대조 세 건(D-28 후속), `_run` 을 못 찾았을 때의 stderr 한 줄 한 건과 프로젝트 간 불일치를 rescore 와 cli 로 거부하는 종단 한 건(D-29 리뷰 후 수정)이다. repeat 인덱스·`started_at`·`--projects` 를 생략했을 때의 동작이 확인되지 않았다. D-21 범위 밖 |
| `--repeat` 의 `rep-k/manifest.json` 커밋 (블랙박스 관찰 3) | `.gitignore` 의 `eval/runs/*/*` 가 `rep-k/` 를 통째로 무시하고 `!eval/runs/*/manifest.json` 예외는 최상위 manifest 에만 닿는다. `git check-ignore -v eval/runs/x/rep-1/manifest.json` 으로 확인했다. 범위 밖 |
| CLI 메모의 실패 사유 80자 절단 (관찰 4) | `coverage_meta` 가 사유를 80자(`REASON_CHARS`)에서 자른다. CLI 사유는 `ExceptionName: 메시지` 라 긴 메시지의 뒷부분이 메모에서 빠진다. 전체는 `raw/scan.stdout.txt` 에 있다 |
| 러너의 Ctrl-C (D-21 §2 한계) | `runner/run.py` `_single` 이 에이전트를 스레드 풀에서 돌려서 KeyboardInterrupt 가 어댑터에 닿지 않는다. `pool.map` 대기와 `with ThreadPoolExecutor` 종료가 인터럽트를 미루고 진행 중 프로젝트(`claude` 호출 포함)를 기다린다. 스크래치 실험에서 1.0초에 넣은 인터럽트가 8.0초에 나왔다. `eval run` 에서 Ctrl-C 로 호출이 멈추려면 러너에 같은 처리가 필요하다 |
| 응답을 못 읽은 호출의 토큰 | 데드라인 + 2초에 버린 호출(진행 줄·요약·메모에 표시, D-21 §6)과 SDK timeout 이 끊은 호출은 응답이 없어 `_usage` 가 없다. 서버가 그 호출의 일부 토큰을 청구했을 수 있고 `tokens_in`·`tokens_out` 은 그만큼 모자랄 수 있다. 얼마인지는 알 수 없다 |
| 실제 `claude` 의 자손 프로세스 (D-22) | 출력을 임시 파일로 돌려 파이프 대기 문제는 없앴다. 다만 `Popen.kill` 은 여전히 자손을 죽이지 않는다. 자손이 있으면 kill 뒤에도 살아 있고 임시 cwd 를 쥐어 지우지 못할 수 있다(이제 stderr 에 한 줄 경고). 실제 `claude -p` 가 자손을 만드는지는 API 를 쓰지 않아 확인하지 못했다 |
| 실제 Ctrl-C 키 입력 | 시험은 `CTRL_BREAK_EVENT`, `_thread.interrupt_main()` 으로 대신했다. 실제 키 입력과 이 머신 밖(다른 Windows, POSIX)은 확인하지 못했다 |
| `compare` 가 `prompt_sha256` 과 파일 집합 차이를 경고하지 않는다 | `report/compare.py` 의 비교 가능성 경고는 데이터셋 sha, 채점기 모델·커밋, batch size, 채점된 프로젝트 집합, `no_files` 프로젝트 집합(D-27), 에이전트 `model`·`effort`·`max_tokens`·`call_shape`(D-28 후속)다. `prompt_sha256` 이 달라도, 스캔한 파일 집합(`projects[].files_total`)이 달라도 경고가 없다. D-26 전후의 run 은 같은 저장소인데 파일 집합이 다르고(secondswap 36개 대 16개) `prompt_sha256` 은 같아서 같은 조건처럼 보인다. D-28 전 manifest 에는 `max_tokens` 키가 없어서 그런 run 과 비교하면 실제 값이 같아도 `agent.max_tokens differs: A=미기록 ...` 이 뜬다. 양쪽 모두 키를 기록하지 않은 경우도 `agent.max_tokens: 양쪽 미기록` 한 줄이 뜬다(D-29 리뷰 후 수정 R2). baseline 은 `effort`, `max_tokens`, `call_shape` 를 적지 않아서 baseline 끼리 비교해도 세 줄이 항상 뜬다. 파일 집합이 다른 run 끼리 비교할 때는 두 manifest 의 `projects[].files_total` 을 사람이 대조한다 (D-14, D-23, D-26, D-28 후속) |
| 스트림 도중 끊김은 재시도하지 않는다 (D-24) | SDK 재시도는 연결 단계(429·5xx·연결 오류)뿐이다. 도중에 끊긴 파일은 `ERROR stream interrupted` 이고 다시 요청하지 않는다. 15분 넘게 생성한 스트림이 끊기면 부분 usage 만 남는다. 끊김 빈도를 보고 재시도를 넣을지 정한다 (§3) |
| 출력 도중 거절의 text 이어 붙이기 (D-24) | 경계 앞 text 와 폴백 모델의 이어쓰기를 이어 붙인다(문서의 "continuation context" 에서 추론). 폴백 모델이 처음부터 다시 쓰면 `ERROR JSON 파싱 실패` 로 보이고, 그때는 경계 뒤 text 만 읽도록 바꾼다. 파일럿 확인 항목 (§4) |
| 스트림을 닫는 타이머의 OS 범위 (D-24) | 다른 스레드의 `close()` 가 막힌 읽기를 깨우는 것은 Windows 11 에서만 확인했다. 못 깨우는 OS 에서는 main() 의 감시가 데드라인 + 2초에 호출을 버리고 그 호출의 usage 는 못 읽는다 (§3) |
| 끊긴 스트림의 출력 토큰 (D-24) | partial 행의 `output_tokens` 는 `message_start` 시점 값이라 하한이다. 그 호출의 실제 출력은 알 수 없다. `tokens_out` 으로 센 지출은 과소일 수 있고 진짜 상한은 Console 지출 한도다 (§3, §6) |
| 비용 차단기 없음, 상한은 Console 지출 한도로 ($100 은 D-24, $200 으로 상향은 D-28) | 손계산(토큰 × 공식 단가) 기준. 64000 토큰, 36 파일 최악 1회는 약 $115 였다(D-24). D-28 의 max 128000 실행은 Fable 16파일 최악 1회 약 $103, Opus 약 $41, 4회 전부 최악이면 약 $350(3 × $103.06 + $41.26, 입력·캐시 포함)으로 상한 $200 을 넘는다. 사용자가 상한 $200 을 정했고(2026-10-22) Console 지출 한도도 $200 으로 권고한다. 도구는 막지 않는다. 추정 누적은 약 $37.74 이고 4회 보통 추정 $33~50 을 더하면 $71~88 이다. Fable max 3회의 손계산 합은 $33.71 이라 누적은 약 $71.5 였고, Opus max $8.98 을 더하면 약 $80.4(상한 $200 의 40%)다 (D-24 §6, D-28 §6, §8, D-29 §6) |
| 키 별칭은 러너 경유만 (D-24) | `ATTACKTRAIL_ANTHROPIC_API_KEY` 는 `eval run` 이 읽는다. `AttackTrailAgent` 를 직접 부르면 `os.environ` 의 `ANTHROPIC_API_KEY` 만 본다 (§7) |
| partial 행의 폴백 표시와 입력 토큰 (D-25) | partial 행의 `fallback` 은 message_start 의 `model` 과 요청 모델의 이름 비교다(별칭이면 오표시 가능). 끊기기 전에 폴백이 있었으면 partial 행의 입력 토큰에서 다른 시도의 입력이 빠질 수 있다. 응답 `model` 이 요청 이름과 같은지는 파일럿에서 본다 (§2 항목 4) |
| 생각 중 ping·바이트 (D-25) | 모델이 생각하는 동안 ping 이나 바이트가 오는지는 여전히 미확인이다. 36파일 본 실행(2026-10-21)에서도 최대 `max_gap_s` 는 278.6초(`test/StepVesting.test.ts`)로 시도당 timeout 1800초보다 짧았다. 선행 5파일에서 가장 긴 무이벤트 구간(`max_gap_s`, 단일 최장 간격)은 호출 길이의 66~82%, 최대 234.2초였다. 그 구간이 생각 구간이라는 해석은 추정이다. 그 구간이 그 실행의 시도당 timeout(1193.4초)보다 짧아서 바이트가 왔는지는 가려지지 않았다. `CALL_TIMEOUT_S` 1800 은 이 실측 뒤 유지로 결정됐다. `--deadline-s` 없이 직접 돌리면 총시간은 못 묶는다 (§2 항목 1, D-25 선행 5파일 소절) |
| 닫는 경합의 범위 (D-25) | 데드라인 close 와 마지막 읽기가 겹쳐도 `message_stop` 까지 받은 `end_turn` 응답은 살린다. `refusal`·`max_tokens` 응답이나 `message_stop` 전에 닫히면 예외 그대로이고 usage 는 partial 값이다. message_stop 을 받은 뒤의 예외(refusal·max_tokens 포함)는 차단기에 세지 않는다. 다만 인증·권한·결제·모델 없음 error 이벤트는 그 뒤에 와도(`end_turn` 응답 뒤 포함) 치명이라 첫 발생에 멈추고(재검증 2차 B, `end_turn` 뒤는 3차), 이 ERROR 끝에는 `stop_reason=<값> category=<값>` 이 붙는다(2차 A). 데드라인 타이머가 message_stop 뒤에 닫은 호출은 `ERROR timeout: hard deadline reached` 라 이 값이 안 붙는다. message_stop 전의 끊김은 classify 대로 센다(재검증 후 수정 항목 4). 경합 자체는 가짜 서버로 재현하지 못했다 (§2 항목 8) |
| 후보 제목·설명의 언어 (D-25 선행 5파일) | 후보 제목이 Marketplace 와 Whitelist 는 영어, StepVesting 과 VestingManager 는 한국어다. 설명(`why`)은 후보가 있는 네 파일 모두 한국어 문장에 코드 식별자가 섞여 있다. scan.py 에 출력 언어를 정하는 문구가 없다(ROLE 포함). 고정하려면 프롬프트를 바꿔야 하고 그러면 `prompt_sha256` 이 달라지므로 이번엔 하지 않았다 (D-25 선행 5파일 소절 관찰 5) |
| 제외 규칙이 닿지 않는 곳 (D-26) | `--web` 은 같은 폴더를 빼지 않는다(웹 정답셋이 없어 검증 못 함). 폴더가 아니라 파일 이름이 테스트·스크립트 모양인 파일(`*.t.sol`, `*.test.ts`, `*_test.go` 등)은 폴더 밖에 있으면 선택된다(29개 저장소에 65개). 대문자 폴더(`Test/`)와 `testing/` 같은 이름은 안 뺀다. 검증은 29개 저장소와 정답 글의 이름 언급에 기댄다. holdout 이 생기면 다시 한다 (D-26 §2, §3, §7) |
| 지원 밖 프로젝트의 보고 범위 (D-27) | 선택 파일 0개 프로젝트는 `no_files` 로 분리했고(결정됨, D-27) 정답 수와 전체 포함 Recall 을 `unsupported` 에 남긴다. 해당은 `code4rena_cabal-liquid-staking-token_2025_05`(정답 8, in-scope 가 전부 `.move`)와 `code4rena_coded-estate-invitational_2024_12`(정답 33, `.rs`)로 합 41건이다. 남은 것: `--repeat` 합본 리포트에 지원 밖 절이 없고 각 `rep-k/` 에만 있다. High Recall 의 전체 포함 값이 없다. 어댑터가 scan.py 의 0개 문구(`스캔할 파일이 0개입니다`)와 exit 1 에 기댄다(scan.py 를 안 바꾼 대가이고 시험이 실제 scan.py 로 고정한다). 지원 언어 자체를 넓히는 일(Move, Rust)은 하지 않았다 (D-27 §1, §7) |
| 재채점·재실행 결과 이후 (D-26 소절) | 해결(D-28 §8). `pilot-secondswap-fable-d26` 의 nethermind 재채점(`pilot-secondswap-fable-d26-nm`: recall 0.233 / 0.333)과 모델 비교를 마쳤다. 남은 것: 전부 1회 표본이라 모델 순위를 정할 수 없다. Fable xhigh 3회의 엄격 recall 표본 표준편차 0.084 가 모델 간 차이(Fable 0.32, Opus 0.27, Sonnet 0.20)와 같은 크기라 Opus 5.5·Sonnet 5.5·Haiku 5.5 도 반복이 필요하다. 36파일 run(0.30)과 16파일 run 은 파일 집합이 달라 같은 조건이 아니다. D-29: Fable max 3회의 표준편차 0.120 도 모델·effort 간 차이와 같은 크기이고 Opus max 는 1회라 같은 결론이다 (D-26, D-28 §8, D-29 §9) |
| effort max, 128000 실행 (D-28, D-29) | 완료했다(D-29). Fable 5.1 max 3회와 Opus 5.5 max 1회가 채점까지 끝났다. 서버가 effort `max` 와 128000 요청을 받는지는 두 모델 모두 확인됐다(D-29 §5). 결과: Fable 은 xhigh 대비 엄격 recall 0.322 에서 0.433, 비용 2.1배이고 표준편차 때문에 확정은 아니다(D-29 §4). 남은 것: 폴백이 일어났을 때 128000 이 유지되는지(폴백 0 이라 못 봄), Opus 의 128K 한도 직접 확인(최대 62,225 토큰까지만 받음), Opus 반복, 프로젝트 확장 (D-28 §11, D-29 §11) |
| 채점 불완전을 원인별로 못 가른다 (D-28) | `judge_incomplete`(`truths_unevaluated` > 0 이고 지적 1건 이상)는 judge 호출 실패와 지적 소진을 구별하지 못한다(지적 0건 프로젝트는 D-28 후속에서 제외). 재채점이 필요한지는 `scoring_output/scorer_stdout.log` 의 오류 줄로 본다. 선택 사항이던 로그 줄 수 게이트는 하지 않았다. `compare` 도 이 표시를 경고하지 않는다 (D-28 §9, 후속 4) |
| 모델·effort 비교의 표본 크기 (D-29) | 프로젝트 1개(secondswap, 정답 30건)에서 Fable 은 effort 당 3회, Opus·Sonnet·Haiku 는 1회다. Fable 의 xhigh 대 max 엄격 recall 차이 0.111 은 표본 표준편차(0.120, 0.084)와 같은 크기이고 Welch p 약 0.27 이라 확정이 아니다. 다음 후보는 다른 scabench 프로젝트 2~3개로 확장하거나 반복을 늘리는 것이고 사용자 결정 대기다. 폴백 시 128000 유지와 Opus 의 128K 한도 직접 확인도 못 했다. xhigh(64000)와 max(128000)는 `max_tokens` 도 달라서 effort 효과만 분리하지도 못했다(영향은 작다고 보지만 추정이다) (D-29 §4, §9, §11) |
| exp3 재채점과 judge 모델 (D-30) | `o4-mini` 폐기로 exp3 두 run 의 채점이 전부 실패했다(`truths_unevaluated` 57/57, `judge_incomplete`). 사본 `runs/exp3-fable-max-nm2`, `runs/exp3-opus-max-nm2` 에서 재채점할 예정이고 judge 모델(`gpt-5-nano-2025-08-07`, `gpt-5-2025-08-07`, `o3-2025-04-16` 중)은 사용자 결정 대기다. `cli.py` 의 `--judge-model` 기본값 `o4-mini` 변경은 다음 작업이다. 사본 재채점은 수정 전 kinetiq 25개(17개 + 디렉터리 8개) 스캔 결과를 그대로 쓴다 |
| 빌드 산출물 폴더 이름 규칙의 범위 (D-30) | 이름만 보므로 `build`, `cache`, `out` 이 실제 소스 폴더인 저장소는 소스까지 빠진다. 29개에서는 morph-l2 `build/lint.go` 1개였고 `--web` 은 0개다. 29개 밖과 웹 대상은 검증하지 못했다. `broadcast/` 는 제외하지 않았다 |
