# 개발·배포 절차

> 이 저장소는 **빌드 도구가 없는 순수 정적 사이트**입니다. Node·npm 불필요.
> 필요한 것: **Git**, **텍스트 에디터**, **Chrome**(PDF 생성용, 선택)

---

## 0. 저장소 구조

```
anam145_audit/
├─ index.html          랜딩페이지 본문 (전부 여기 들어있음)
├─ styles.css          전체 스타일
├─ favicon.svg
├─ README.md
├─ DEVELOPING.md       ← 이 문서
├─ .github/workflows/  GitHub Pages 자동 배포
├─ .gitignore          internal/ 제외
└─ internal/           .gitignore 처리 — clone해도 따라오지 않음
```

**⚠️ `internal/` 은 git에 올라가지 않습니다.** 사업 계획·가격·원가·영업 자료가 들어 있어 의도적으로 제외했습니다. 저장소 루트 전체가 GitHub Pages로 공개 배포되기 때문입니다(`path: "."`).

→ 다른 PC에서 `internal/` 이 필요하면 **별도로 옮겨야 합니다.** §4 참조.

---

## 1. 회사 PC 최초 설정

```bash
# 1) Git 설치 확인
git --version

# 2) clone
git clone https://github.com/ehddnrRb/anam145_audit.git
cd anam145_audit

# 3) 최신 상태 확인
git log --oneline -3
```

`git log --oneline -3` 의 최신 커밋이 clone 시점과 맞는지 확인합니다. 줄 수로 판별하지 마십시오.

Chrome 경로 확인 (PDF 생성용):

```powershell
Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

---

## 2. 미리보기

**가장 간단** — `index.html` 을 브라우저로 그냥 엽니다. 외부 의존성이 없어 파일 프로토콜로도 정상 동작합니다.

로컬 서버로 보려면:

```bash
python -m http.server 8000
# http://localhost:8000
```

---

## 3. 편집 시 주의 — 실제로 사고가 났던 부분

### 🔴 PowerShell로 HTML·MD 파일을 읽고 쓰지 마십시오

```powershell
# ❌ 절대 이렇게 하지 말 것
$s = Get-Content index.html -Raw
$s -replace 'a','b' | Set-Content index.html
```

**이유**: 이 저장소의 파일은 **UTF-8(BOM 없음)** 입니다. Windows PowerShell 5.1의 `Get-Content` 는 BOM이 없으면 시스템 코드페이지(cp949)로 잘못 해석해 **한글과 특수문자(`·` `—` `–` `▪` `→`)를 전부 깨뜨립니다.**

실제로 이 사고가 났고, 더 나빴던 건 **연쇄 피해**였습니다. CSS의 `content: "▪"` 가 깨져 문자열이 닫히지 않으면서 **그 뒤 CSS 전체가 죽었습니다** — 표 배경·테두리·배지 색상이 모두 사라진 채로 PDF가 "정상 1페이지"로 보였습니다.

**대신 이렇게**:

```bash
# Python (권장)
python -c "
import io
p='index.html'
s=io.open(p,encoding='utf-8').read()
s=s.replace('before','after')
io.open(p,'w',encoding='utf-8').write(s)
"
```

또는 에디터(VS Code 등)에서 직접 편집. 에디터는 인코딩을 올바르게 처리합니다.

### 편집 후 인코딩 검사

```bash
python -c "
import io
s=io.open('index.html',encoding='utf-8').read()
bad=[c for c in ['烈','?','\ufffd'] if c in s]
print('특수문자:', all(c in s for c in ['·','—']))
print('BOM:', open('index.html','rb').read(3)==b'\xef\xbb\xbf')
"
```

`특수문자: True` / `BOM: False` 여야 정상입니다.

---

## 4. `internal/` 을 다른 PC로 옮기기

`internal/` 은 git으로 동기화되지 않습니다. 세 가지 방법이 있습니다.

| 방법 | 장점 | 단점 |
|---|---|---|
| **A. 별도 private 저장소** (권장) | 두 PC 동기화 + **백업** 확보 | 저장소 하나 더 관리 |
| B. USB · 개인 클라우드 드라이브 | 즉시 가능 | 수동 동기화, 버전 충돌 위험 |
| C. 옮기지 않음 | 실수 여지 없음 | 회사 PC에서 사업 자료 참조 불가 |

### A안 설정 방법

```bash
# 1) GitHub에서 private 저장소 생성 (예: anam145_audit_internal)

# 2) 이 PC에서
cd internal
git init
git add .
git commit -m "Internal business documents"
git branch -M main
git remote add origin https://github.com/<계정>/anam145_audit_internal.git
git push -u origin main

# 3) 회사 PC에서
cd anam145_audit
git clone https://github.com/<계정>/anam145_audit_internal.git internal
```

**반드시 private으로 만드십시오.** 가격·원가·손익·미승인 가격표가 들어 있습니다.

> 현재 `internal/` 28개 파일은 **이 PC 한 대에만 있고 백업이 없습니다.** A안은 동기화보다 백업 목적이 더 큽니다.

---

## 5. PDF 재생성 (영업 자료)

`internal/sales/` 의 HTML을 PDF로 뽑을 때 씁니다.

```powershell
$chrome  = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$profile = "$env:TEMP\chrome-pdf-profile"
$dir     = "<저장소경로>\internal\sales"
$name    = "AttackTrail-EN-onepager"   # internal/sales/ 의 HTML 파일명

& $chrome --headless=new --disable-gpu --no-sandbox `
  --user-data-dir=$profile `
  --run-all-compositor-stages-before-draw `
  --virtual-time-budget=10000 `
  --no-pdf-header-footer `
  "--print-to-pdf=$dir\$name.pdf" `
  ("file:///" + "$dir\$name.html".Replace('\','/'))
```

**`--user-data-dir` 을 빼면 Chrome이 조용히 실패합니다** (파일이 생성되지 않고 에러도 안 냅니다).

### 페이지 수 확인

```bash
python -c "
import re
b=open('internal/sales/AttackTrail-EN-onepager.pdf','rb').read()
print('페이지:', len(re.findall(rb'/Type\s*/Page[^s]',b)))
"
```

1페이지 자료는 반드시 `1` 이어야 합니다. 내용을 추가하면 쉽게 2페이지로 넘어갑니다.

### PDF가 열려 있으면 덮어쓰기 실패

PDF 뷰어가 파일을 잠그면 Chrome이 쓰지 못합니다. **뷰어를 닫고** 다시 실행하거나, 다른 이름으로 뽑은 뒤 `Move-Item ... -Force` 로 교체하십시오.

---

## 6. 배포

`main` 브랜치에 push하면 **GitHub Actions가 자동으로 GitHub Pages에 배포**합니다.

```bash
git add index.html styles.css
git commit -m "Update landing page"
git push
```

- 진행 상황: 저장소 **Actions** 탭 → `Deploy to GitHub Pages`
- 배포 URL: 워크플로 실행 결과의 `page_url` 출력값 (통상 `https://<계정>.github.io/anam145_audit/`)
- 보통 1~3분 소요

### 🔴 push 전 확인

| 확인 | 이유 |
|---|---|
| `git status` 에 `internal/` 이 없는가 | 있으면 `.gitignore` 가 깨진 것. **사업 자료가 공개 배포됩니다** |
| 미확정 가격·금액이 들어 있지 않은가 | 현재 사이트에 금액 없음 |
| 문의 폼이 연결되었는가 | 연결 완료(Formspree). 수신 이메일 인증 여부는 대시보드에서 확인 |
| 연락처가 맞는가 | `dev@anam145.com` 으로 통일되어 있습니다 |

**push는 곧 공개 배포입니다.** 되돌리려면 다시 push해야 하고, 그 사이 내용은 이미 노출됩니다.

---

## 7. 알려진 미해결 항목

| 항목 | 상태 |
|---|---|
| 가격 게시 | 금액 미게시 (승인 대기). 금액과 복원 방법은 `index.html` 주석에 보존 |
| 국문 사이트 | 현재 전부 영문. 국내 고객용 국문판 미제작 |
| `AUD-SCP-01` 법률 검토 | 변호사 질문 18개 정리 완료, 검토 미착수 |
| `AUD-SCP-01` AI 조항 | 고객 코드를 외부 API로 보내는 건에 대한 수탁자 고지 조항 없음 |
| `AUD-MEA-01` 측정 도구 | 1순위 도구 `solidity-code-metrics` 유지보수 중단. 대안 확정 필요 |
| 감사 도구 설치 | 작업 PC에 Foundry·Slither·Aderyn 미설치. `AUD-CHK-01` §1 참조 |
| `AUD-UPG-01` | 프록시·재감사 절차서 미작성 |
| `AUD-MISS-01` · `AUD-BMK-01` | 미스 로그와 벤치마크 20건 미착수 |
