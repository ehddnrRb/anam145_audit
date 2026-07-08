# ANAM145 Audit — 랜딩페이지

AI-augmented 스마트컨트랙트 감사 회사 ANAM145의 정적 랜딩페이지입니다.
빌드 도구·의존성 없이 순수 HTML/CSS로만 구성되어 있어 어떤 정적 호스트에도 그대로 올릴 수 있습니다.

## 구성 파일

| 파일 | 설명 |
|------|------|
| `index.html` | 페이지 본문 (완전한 HTML 문서) |
| `styles.css` | 전체 스타일 (다크 단일 테마) |
| `favicon.svg` | 브라우저 탭 아이콘 |
| `README.md` | 이 문서 |

## 로컬에서 보기

`index.html`을 브라우저로 그냥 열면 됩니다.
(원한다면 로컬 서버로도 확인 가능)

```bash
# 예: 파이썬 내장 서버
python -m http.server 8000
# 브라우저에서 http://localhost:8000
```

## 배포 방법 (택 1)

### 1) Netlify — 가장 간단
1. https://app.netlify.com/drop 접속
2. `anam_audit` 폴더를 통째로 드래그&드롭
3. 끝. 바로 URL이 발급됩니다.

### 2) Vercel
```bash
npm i -g vercel
cd anam_audit
vercel
```

### 3) GitHub Pages
1. 이 폴더 내용을 새 GitHub 저장소에 push
2. 저장소 **Settings → Pages** 에서 Branch를 `main` / 루트(`/`)로 지정
3. 몇 분 뒤 `https://<사용자>.github.io/<저장소>/` 에서 공개

### 4) Cloudflare Pages / 기타
정적 호스팅이면 어디든 동일하게 동작합니다. 빌드 명령 없음, 출력 디렉터리 = 이 폴더.

## 커스텀 도메인
호스트(Netlify/Vercel/Cloudflare 등)의 Custom Domain 설정에서 DNS를 연결하면 됩니다.

---

## 배포 전 꼭 바꿔야 할 것 (플레이스홀더)

- **연락 이메일**: `index.html` 내 `audit@anam145.io` → 실제 이메일로 변경
- **Evidence 섹션의 취약점 예시 3건**: 데모용 샘플입니다. 실제 발견 사례(또는 공개 가능한 리다ted 사례)로 교체 권장
- **Roots 섹션(고려대·사이버국방·ANAM145)**: 실제 소속·실적으로 문구 확정
- **벤치마크**: 현재는 수치 없이 정성적 근거만 있습니다. 공개 가능한 실제 벤치마크가 생기면 Evidence 섹션에 추가
- **OG/메타**: 필요 시 `<meta property="og:image">` 등 소셜 공유 이미지 추가

## 참고
- 디자인은 다크(거의 순수 블랙) 단일 테마로 의도적으로 커밋했습니다.
- 외부 폰트/스크립트/CDN 의존성이 없어 오프라인·폐쇄망에서도 동작합니다.
- 문의 폼은 현재 `mailto:` 링크입니다. 실제 폼(예: Formspree, Tally, 자체 백엔드)이 필요하면 CTA 버튼을 폼으로 교체하세요.
