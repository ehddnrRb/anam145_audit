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

## 검증 결과

```
pytest                       54/54 통과
목 판정자 end-to-end           3개 프로젝트 실제 clone, run/render/compare/repeat 동작
판정자 nethermind + 키 없음     clone 전에 즉시 중단, 디렉토리 생성 안 됨
holdout 가드                  플래그 없으면 exit 2 + 경고
scan.py                      SHA256 F724874070E5F7FF8D8C4A7F0C9665BC867C23E36573CB553C0C56F7D7383871 전후 동일
```

목 실행 숫자는 전부 의미 없다. 판정이 내용과 무관한 카운터로 정해진다.

## 리뷰에서 잡아 고친 것 (13건 중 9건 확인, 4건 반박)

- blocker: 판정 안 된 truth 가 분모에서 빠지던 문제 (D-9)
- blocker: compare 가 finding 0건 run 의 회귀를 놓치던 문제
- major: compare 가 두 run 의 채점 대상 프로젝트 집합이 같은지 확인하지 않던 문제
- major: POSIX 에서 대상 저장소의 심볼릭 링크가 체크아웃되던 문제 → `core.symlinks=false` + 사후 검사
- minor: git 하위 프로세스가 API 키를 상속하던 문제 → 허용목록 env
- 테스트 중 발견: `pytest` 가 클론된 대상 저장소의 테스트 파일을 수집해 import 했다. 대상 코드를 실행하는 경로다. `conftest.py` 에 `collect_ignore = ["datasets", "runs"]`

## 아직 안 된 것

| 항목 | 이유 |
|---|---|
| 진짜 nethermind 판정자 실행 | OPENAI_API_KEY 없음 |
| AttackTrail 실제 스캔 | ANTHROPIC_API_KEY 없음, eval/.venv 에 anthropic SDK 없음 |
| repeat 의 프로젝트별 평균·표준편차 | 헤드라인만 집계됨 |
| `--from-manifest` 재실행 | 미구현. 지금은 manifest 의 `cli_argv` 로 손으로 재실행 |
| 커밋이 `main`·짧은 sha 인 프로젝트의 체크아웃 경로 | 29개 측정에서 동작은 했으나 전용 테스트 없음 |
| Docker 실행기 | 거부 스텁. PoC 실행 전 필수 |
