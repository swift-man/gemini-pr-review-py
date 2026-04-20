# gemini-review

Google OAuth(Gemini CLI) 기반 GitHub PR **전체 코드베이스** 리뷰 봇.
GitHub App 웹훅으로 PR 이벤트를 받아, 레포를 체크아웃하고 전체 파일을 컨텍스트로 넣어
`gemini -p` CLI로 리뷰를 생성한 뒤 PR에 리뷰를 게시합니다.

## 특징

- GitHub App 설치 토큰 기반 인증 (PAT 불필요)
- diff가 아닌 **전체 코드베이스**를 컨텍스트로 사용
- Gemini CLI를 `subprocess`로 호출 → 로그인된 **Google 계정의 OAuth 토큰** 사용 (기본 모델 `gemini-2.5-pro`)
- 한국어 리뷰 고정 출력 (JSON 스키마 강제)
- **리뷰 3분류**: `좋은 점` / `개선할 점` / `기술 단위 코멘트(라인 고정)`
- 라인 고정 코멘트만 인라인으로 게시, 라인 번호 없는 지적은 `개선할 점`으로 이동
- 기초 타입(`str`/`list`/`String`/`Array` 등) 수준의 팁 배제, **Python/TypeScript/React 공식 상위 API**에 초점
- 리뷰는 **단일 슬롯 직렬화** 처리 (동시 1건, 나머지 큐 대기)
- 컨텍스트 예산 초과 시 리뷰 대신 안내 코멘트 게시
- SOLID — 계층 분리, `Protocol`로 의존성 역전

## 아키텍처

```
GitHub PR event
  → FastAPI /webhook (HMAC 검증, 202 즉시 응답)
  → serialized queue
      1. Installation Token 발급 (JWT → GitHub App API)
      2. PR 메타 / 변경 파일 조회
      3. git clone --filter=blob:none + checkout head SHA (캐시)
      4. 파일 수집 + 필터 + 우선순위 + 토큰 예산
      5. `gemini -m ... -p` 호출 (stdin: 프롬프트)
      6. JSON 파싱 → POST /pulls/{n}/reviews
```

```
src/gemini_review/
├── interfaces/       # Protocol: GitHubClient, ReviewEngine, RepoFetcher, FileCollector
├── domain/           # PullRequest, ReviewResult, Finding, FileDump (frozen dataclass)
├── application/
│   ├── review_pr_use_case.py   # 오케스트레이션
│   └── webhook_handler.py      # HMAC 검증 + 직렬화 큐 워커
├── infrastructure/
│   ├── github_app_client.py    # JWT → installation token → REST
│   ├── git_repo_fetcher.py     # clone/fetch/checkout
│   ├── file_dump_collector.py  # 필터 + 우선순위 + 토큰 예산
│   ├── gemini_prompt.py        # 한국어 시스템 규칙 + 파일 직렬화
│   ├── gemini_parser.py        # JSON 추출 (코드펜스 스트립) + fallback
│   └── gemini_cli_engine.py    # subprocess(gemini -p) 호출 + OAuth 선점검
├── config.py         # pydantic-settings
└── main.py           # FastAPI 조립 (DI)
```

## 전제 조건

- macOS, Python 3.11+
- `git` 설치
- `gemini` CLI가 PATH에 있고 **Google 계정으로 로그인**되어 있어야 함
  - 설치: `npm i -g @google/gemini-cli`
  - 로그인: 터미널에서 `gemini` 실행 → 열린 브라우저에서 Google 계정 동의
  - 확인: `ls ~/.gemini/oauth_creds.json` (파일이 존재하고 `refresh_token` 키가 있어야 함)
- GitHub App 생성 및 대상 레포에 설치
  - 권한: Pull requests (R/W), Contents (R), Metadata (R)
  - 이벤트 구독: `Pull request`

## 설치

```bash
bash scripts/install_local_review.sh
cp scripts/local_review_env.example.sh scripts/local_review_env.sh
$EDITOR scripts/local_review_env.sh   # App ID / key path / webhook secret 입력
```

## 실행

```bash
bash scripts/run_webhook_server.sh
# → http://127.0.0.1:8000/webhook 수신 대기
```

테스트 웹훅 발사:
```bash
REPO_FULL_NAME=owner/repo PR_NUMBER=1 INSTALLATION_ID=1234567 \
    bash scripts/send_test_webhook.sh
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GITHUB_APP_ID` | — | GitHub App ID (필수) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | PEM 경로 (또는 `GITHUB_APP_PRIVATE_KEY` inline) |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC 서명 검증용 비밀 (필수) |
| `GEMINI_BIN` | `gemini` | Gemini CLI 실행 파일 |
| `GEMINI_MODEL` | `gemini-2.5-pro` | 모델 (`gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`) |
| `GEMINI_MAX_INPUT_TOKENS` | `900000` | 전체 컨텍스트 토큰 예산 (2.5-pro 는 최대 1M 토큰) |
| `GEMINI_TIMEOUT_SEC` | `600` | 호출 타임아웃 |
| `GEMINI_OAUTH_CREDS_PATH` | `~/.gemini/oauth_creds.json` | Google OAuth 자격 증명 파일 |
| `REPO_CACHE_DIR` | `~/.gemini-review/repos` | clone 캐시 위치 |
| `FILE_MAX_BYTES` | `204800` | 단일 파일 크기 상한 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 바인딩 주소 |
| `DRY_RUN` | `0` | `1`이면 로그만 남기고 게시 안 함 |

## 동작 규칙

- 수신 이벤트: `opened`, `synchronize`, `reopened`, `ready_for_review`
- Draft PR은 skip
- 파일 필터: `.git`, `node_modules`, `dist`, `build`, `vendor`, `__pycache__` 등 디렉터리와
  `*.lock`, 바이너리, 미디어, 폰트, `package-lock.json` 등은 자동 제외
- 우선순위: 변경 파일 → `src/app/lib/pkg/...` → 기타
- 예산 초과 시: 변경 파일이 빠졌다면 리뷰를 **수행하지 않고** PR에 안내 코멘트만 게시

## 리뷰 출력 (3분류)

모델은 아래 JSON 스키마를 엄격히 따라야 합니다.

```json
{
  "summary": "...",
  "event": "COMMENT | REQUEST_CHANGES | APPROVE",
  "positives":    ["좋은 점 ..."],
  "improvements": ["개선할 점 (파일/모듈/아키텍처 단위) ..."],
  "comments": [
    {"path": "src/x.py", "line": 42, "body": "기술 단위 코멘트 (라인 고정)"}
  ]
}
```

- `positives` → PR 리뷰 본문 "**좋은 점**" 섹션으로 렌더
- `improvements` → 본문 "**개선할 점**" 섹션으로 렌더
- `comments` → GitHub 인라인 리뷰 코멘트로 라인에 붙음 (**line 필수**)

### 기술 단위 코멘트의 취향

기초 수준(`str`/`list`/`String`/`Array`/`JSON.parse` 등)의 팁은 제외하도록 프롬프트에서 강제합니다.
대신 아래와 같은 **공식 상위 API** 사용을 지적/권장하도록 유도합니다.

- **Python**: `collections.Counter/defaultdict/deque`, `itertools`, `functools.cache/singledispatch`,
  `dataclasses(frozen=True, slots=True)`, `typing.Protocol/TypedDict/assert_never`,
  `pathlib.Path`, `contextlib.ExitStack/suppress`, `asyncio.TaskGroup`, `enum.StrEnum`, pydantic `BaseModel`
- **TypeScript**: `Map/Set/WeakMap/WeakRef`, 유틸리티 타입(`Readonly/Pick/Omit/ReturnType/Awaited`),
  `satisfies`, discriminated union exhaustiveness, `structuredClone`, `AbortController`,
  `Promise.allSettled/any`, `Intl.*`, Zod `z.infer`
- **React**: `useMemo/useCallback`의 올바른 의존성, `useReducer`, `useId`,
  `useSyncExternalStore`, `startTransition`, `useDeferredValue`, `Suspense/ErrorBoundary`,
  `use()` hook, `useFormStatus/useOptimistic`, React Query `queryKey/staleTime`

모두 `src/gemini_review/infrastructure/gemini_prompt.py`에서 조정 가능합니다.

## 테스트

```bash
.venv/bin/pytest tests/unit -q
```

## 배포 (선택)

- `deploy/nginx-gemini-review.conf`: 리버스 프록시 예시 (TLS, `/webhook`, `/healthz`)
- macOS LaunchAgent / `tmux`+`nohup` 등으로 서버 상주
- GitHub App 웹훅 URL을 nginx 엔드포인트로 지정, 시크릿을 `GITHUB_WEBHOOK_SECRET`과 일치시킬 것

## 참고

동일 저자의 `codex-review` 와 웹훅 파이프라인(HMAC 검증 → 202 즉시 응답 → 직렬화 큐 워커,
App JWT 흐름, 전체 코드베이스 덤프)을 공유합니다. 본 프로젝트는 Codex CLI / ChatGPT OAuth
대신 **Gemini CLI + Google OAuth** 를 사용합니다.

### Gemini CLI 호출 방식에 관한 주의

CLI 버전에 따라 비대화 실행 플래그가 달라질 수 있습니다. 현 구현은 stdin 으로 프롬프트를
전달하는 `gemini -m <model> -p` 를 사용합니다. 설치된 CLI 버전에서 플래그가 다르다면
`src/gemini_review/infrastructure/gemini_cli_engine.py` 의 `cmd` 배열을 조정하거나
`GEMINI_BIN` 으로 래퍼 스크립트를 지정해 보정하세요.
