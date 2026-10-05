# 회귀 스위트 (2026-10-01)

프롬프트나 체크리스트를 바꿨을 때 회귀를 잡으려고 자주 돌리는 스위트다. 주 지표는 known-bug recall.

선정 규칙:

- 필수 조건: `checkout_status` ok, EVM/Solidity 세그먼트, high truth 1건 이상.
- 커밋이 브랜치명이라 재현이 안 되는 5개(fenix, lambowin, loopfi, bakerfi, blackhole)는 뺐다.
- 남은 후보 중 `scanpy_files`가 작은 것을 우선했고, high 카테고리 합집합이 가장 넓어지게 골랐다.
- 지정 후보 3개 중 `code4rena_superposition_2025_01`은 제외했다. primary_language가 rust(Stylus)이고 `in_segment_evm=false`라 EVM 세그먼트 조건에 걸린다. .sol 파일은 13개뿐이고 high truth 3건(LOGIC 2, ORD 1)이 Rust 코드에 있다.

## 프로젝트

| project | scanpy_files | truths | high | 커버 카테고리 (전체, high는 굵게) |
|---|---:|---:|---:|---|
| sherlock_axion_2025_01 | 25 | 10 | 4 | **ARI**, TOK, **DOS**, UPG, **LOGIC** |
| sherlock_tally_2024_12 | 29 | 11 | 2 | ORC, **STA**, SIG, **LOGIC**, OTHER |
| sherlock_oku_2024_12 | 57 | 19 | 8 | **ACC**, **EXT**, **ARI**, ORC, **TOK**, **STA**, INP, DOS, LOGIC |
| code4rena_forte-float128-solidity-library_2025_04 | 9 | 12 | 5 | **ARI**, **INP**, DOS, **LOGIC**, OTHER |
| code4rena_next-generation_2025_05 | 11 | 14 | 1 | ACC, TOK, INP, **SIG**, ORD, DOS, LOGIC, OTHER |
| sherlock_cork-protocol_2025_01 | 56 | 17 | 11 | **ARI**, TOK, **STA**, **ORD**, **DOS**, UPG, **LOGIC** |
| 합계 | 187 | 83 | 31 | |

Claude 호출 187회(파일당 1회). EVM 세그먼트 전체 3082회의 약 6%다.

## 카테고리별 합집합 커버리지

| 카테고리 | 전체 truth | high truth |
|---|---:|---:|
| ACC | 4 | 1 |
| EXT | 1 | 1 |
| ARI | 10 | 6 |
| ORC | 2 | 0 |
| TOK | 6 | 3 |
| STA | 7 | 6 |
| INP | 5 | 2 |
| SIG | 3 | 1 |
| ORD | 4 | 1 |
| DOS | 12 | 2 |
| UPG | 4 | 0 |
| LOGIC | 20 | 8 |
| OTHER | 5 | 0 |

## 커버되지 않는 것

- 전체 심각도 기준: 없음. 13개 카테고리 모두 1건 이상 들어 있다.
- high 기준: ORC, UPG, OTHER. 데이터셋의 EVM 프로젝트 어디에도 이 세 카테고리의 high truth가 없어서 스위트를 바꿔도 채울 수 없다.
- 얇은 곳: EXT, ACC, SIG, ORD는 high가 1건씩이다. 한 건을 놓치면 해당 카테고리 high recall이 0으로 떨어지니 해석할 때 주의한다. ACC high를 늘리려면 sherlock_crestal-network_2025_03(29파일, ACC high 1)을 7번째로 넣는 게 가장 싸다.
