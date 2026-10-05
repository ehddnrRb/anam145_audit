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
```

## 사용법

eval/ 에서 실행한다. Windows 는 `.\eval.cmd`, POSIX 셸(Git Bash 포함)은 `./eval`. 둘 다 `.venv` 파이썬으로 `cli.py` 를 부른다.

### run

```
./eval run --agent {attacktrail,baseline} --dataset {scabench,holdout} --out DIR
           [--projects a,b] [--suite NAME] [--repeat N] [--jobs N] [--timeout S]
           [--judge {nethermind,mock}] [--judge-model M]
           [--baseline-mode {precomputed,live}] [--model M] [--i-understand-holdout]
```

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--projects` | 전체 | 쉼표로 구분한 project_id. `--suite` 보다 우선 |
| `--suite` | 없음 | `suites/<NAME>.txt`. 한 줄에 project_id 하나, `#` 줄은 무시 |
| `--repeat` | 1 | N>1 이면 `DIR/rep-<k>/` 로 N번 돌리고 평균과 표본 표준편차를 낸다 |
| `--jobs` | 2 | 프로젝트 병렬 수 |
| `--timeout` | 1800 | 프로젝트당 에이전트 제한 시간(초) |
| `--judge` | nethermind | `mock` 은 배관 테스트 전용. 아래 참고 |
| `--judge-model` | o4-mini | 채점기 `MODEL` |
| `--baseline-mode` | precomputed | `precomputed` 는 upstream 이 커밋해 둔 GPT-5 결과 파일을 읽는다. 네트워크·키 불필요 |
| `--model` | 에이전트 기본값 | 에이전트에 넘길 모델 |

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
DIR/scoring_data/source_of_truth/<id>.json
DIR/scoring_data/<agent>/<id>_results.json  채점기 입력
DIR/scoring_output/                       채점기 원출력
```

`runs/` 는 `manifest.json` 만 커밋 대상이다.

### compare

```
./eval compare RUN_A RUN_B
```

마크다운을 출력하고 `RUN_B/compare-vs-<RUN_A 이름>.md` 에도 쓴다.

- 한쪽만 mock judge 면 비교를 거부한다(exit 1).
- 데이터셋 sha, 채점 모델, 채점기 커밋, batch size 가 다르면 크게 경고한다.
- 양쪽에서 채점된 프로젝트 집합이 다르면(예: B 에서 에이전트가 crash 해 분모에서 빠짐) 차이를 나열해 경고하고, 양쪽 모두 채점된 프로젝트만으로 계산한 Recall/High Recall 표를 따로 보인다.
- REGRESSIONS: A 에서 엄격 매칭됐는데 B 에서 안 된 truth. `(project_id, finding_id)` 로 짝짓는다. 채점된 프로젝트에서 채점기가 판정하지 않은 truth(지적 0건 등)는 매칭 안 됨으로 센다.
- 어느 쪽이든 repeat 표준편차가 있으면, 큰 쪽 표준편차보다 작은 변화는 노이즈 범위로 표시한다.

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
- 에이전트 서브프로세스 환경변수: executor 는 환경변수를 상속하지 않는다. AttackTrail 에는 `ANTHROPIC_API_KEY` 와 최소 OS 변수(`PATH`, `SYSTEMROOT`, `TEMP`/`TMP`, `PYTHONIOENCODING`)만, baseline live 에는 `OPENAI_API_KEY` 만 넘긴다. manifest 에는 키와 환경변수 덤프를 쓰지 않는다.

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
- 서브모듈을 초기화하지 않으므로 의존 라이브러리 코드가 없다. truth 버그가 그 안에 있으면 에이전트는 원천적으로 못 찾는다. scan.py 는 `/lib/` 경로를 아예 제외한다.
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
