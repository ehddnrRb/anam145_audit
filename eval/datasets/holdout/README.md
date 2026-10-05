# holdout 데이터셋

마일스톤 평가 전용이다. 프롬프트나 체크리스트를 고치는 동안에는 쓰지 않는다.
한 번이라도 튜닝 루프에 들어가면 그 셋은 오염된 것으로 보고 새로 만든다.

기본 실행에는 절대 포함되지 않는다. `--dataset holdout` 은 `--i-understand-holdout` 없이는 exit 2 로 멈추고,
붙이면 경고를 stderr 에 찍은 뒤 진행하며 manifest 에 `holdout_used: true` 가 남는다.

## 프로젝트 추가

1. `TEMPLATE.json` 을 `projects/<project_id>.json` 으로 복사한다. 파일명과 `project_id` 가 같아야 한다.
2. 형태는 SCABench `curated-2025-08-18.json` 배열의 항목 하나와 똑같다.
   `project_id, name, platform, codebases[codebase_id, repo_url, commit, tree_url, tarball_url], vulnerabilities[finding_id, severity, title, description]`.
   다른 키는 받지 않는다.
3. `commit` 은 40자 전체 sha. 빈 문자열이나 브랜치 이름은 거부한다. 체크아웃이 `rev-parse HEAD == commit` 으로 검증하기 때문이다.
4. `severity` 는 `critical, high, medium, low, informational` 중 하나.
5. `description` 은 채점 LLM 이 보는 유일한 본문이다. 원인, 관련 함수·컨트랙트 이름, 영향을 빠짐없이 쓴다.
   SCABench 와 마찬가지로 파일·라인 위치는 채점에 쓰이지 않는다.
6. 검증한다. eval/ 에서:

```powershell
.venv\Scripts\python.exe datasets\holdout\validate.py
```

`REPLACE` 자리표시자가 남아 있거나 커밋이 비어 있으면 실패한다.

## 고르는 기준

- 공개 시점이 에이전트 모델의 학습 컷오프 이후이거나 비공개 감사 결과일 것. 공개 리포트가 학습 데이터에 들어갔다면 recall 이 부풀려진다.
- SCABench 31개와 겹치지 않을 것.
- 사람이 확인한 버그만 넣는다. 미확인 지적은 truth 에 넣지 않는다.
