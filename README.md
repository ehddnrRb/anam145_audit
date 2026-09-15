# AttackTrail — 랜딩페이지

ANAM145 주식회사의 보안 서비스 **AttackTrail** 정적 랜딩페이지입니다.
빌드 도구와 의존성이 없는 순수 HTML/CSS라 어떤 정적 호스트에도 그대로 올라갑니다.

## 구성 파일

| 파일 | 설명 |
|------|------|
| `index.html` | 페이지 본문 (완전한 HTML 문서) |
| `styles.css` | 전체 스타일 (다크 단일 테마) |
| `favicon.svg` | 브라우저 탭 아이콘 |
| `DEVELOPING.md` | 개발·배포 절차, 편집 시 주의사항 |
| `README.md` | 이 문서 |

## 로컬에서 보기

`index.html`을 브라우저로 열면 됩니다. 외부 의존성이 없어 파일 프로토콜로도 동작합니다.

문의 폼까지 시험하려면 로컬 서버가 필요합니다. `file://`로 열면 CORS 때문에 제출이 막힙니다.

```bash
python -m http.server 8000
# http://localhost:8000
```

## 배포

`main` 브랜치에 push하면 GitHub Actions가 GitHub Pages로 자동 배포합니다. 보통 1~3분 걸립니다.
진행 상황은 저장소 **Actions** 탭의 `Deploy to GitHub Pages`에서 확인합니다.

상세 절차와 주의사항은 `DEVELOPING.md`를 보십시오.

---

## 저장소 구조 주의

이 저장소는 **루트 전체가 GitHub Pages로 공개 배포**됩니다 (`.github/workflows`의 `path: "."`).

사업 문서는 `internal/`에 두고 `.gitignore`로 제외했습니다. **절대 커밋하지 마십시오.**
`git status`에 `internal/`이 보이면 `.gitignore`가 깨진 것입니다.

`internal/`은 git으로 동기화되지 않으므로 다른 PC에서 필요하면 별도로 옮겨야 합니다. `DEVELOPING.md` §4 참조.

---

## 배포 전 확인 사항

- **가격 미게시**: 사이트에 금액을 싣지 않았습니다. 게시 결정이 나면 Tier 표에 금액 열을 추가합니다.
- **문의 폼**: Formspree로 연결되어 있습니다 (`index.html`의 `<form action>`).
  제출은 AJAX로 처리되어 페이지 이동 없이 결과가 인라인으로 표시됩니다.
  수신 이메일 인증이 안 되어 있으면 제출이 도착하지 않으니 Formspree 대시보드에서 확인하십시오.
- **통계 인용**: Threat 섹션의 수치는 공개 보도에 근거해야 합니다. 출처 없는 수치는 싣지 않습니다.
- **스팸 대응**: Formspree 대시보드에서 reCAPTCHA를 켜십시오. 현재 방어는 허니팟뿐이고 무료 한도가 월 50건입니다.
- **OG/메타**: 소셜 공유 이미지가 필요하면 `<meta property="og:image">`를 추가합니다.

## 참고

- 다크 단일 테마입니다.
- 외부 폰트·스크립트·CDN 의존성이 없어 오프라인과 폐쇄망에서도 동작합니다.
- 폼 백엔드를 Tally·Web3Forms·자체 서버로 바꾸려면 `<form>`의 `action`과 하단 제출 스크립트만 교체하면 됩니다.
