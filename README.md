# gemini-review

Google OAuth(Gemini CLI) 기반 GitHub PR **전체 코드베이스** 리뷰 봇.
GitHub App 웹훅으로 PR 이벤트를 받아, 레포를 체크아웃하고 전체 파일을 컨텍스트로 넣어
`gemini -p` CLI로 리뷰를 생성한 뒤 PR에 리뷰를 게시합니다.

## 특징

- GitHub App 설치 토큰 기반 인증 (PAT 불필요)
- diff가 아닌 **전체 코드베이스**를 컨텍스트로 사용
- Gemini CLI를 `subprocess`로 호출 → 로그인된 **Google 계정의 OAuth 토큰** 사용 (기본 모델 `gemini-2.5-pro`)
- Preview 모델 capacity/availability, 네트워크·스트림 절단(`ERR_STREAM_PREMATURE_CLOSE` 등) 실패 시 안정 모델로 자동 fallback
- 한국어 리뷰 고정 출력 (JSON 스키마 강제)
- **리뷰 3분류**: `좋은 점` / `개선할 점` / `기술 단위 코멘트(라인 고정)`
- 라인 고정 코멘트만 인라인으로 게시, 라인 번호 없는 지적은 `개선할 점`으로 이동
- 기초 타입(`str`/`list`/`String`/`Array` 등) 수준의 팁 배제, **Python/TypeScript/React 공식 상위 API**에 초점
- 리뷰는 **단일 슬롯 직렬화** 처리 (동시 1건, 나머지 큐 대기)
- 컨텍스트 예산 초과 시 **변경된 라인의 diff-only fallback 리뷰** 자동 시도, diff 도 한도 초과면 안내 코멘트 게시
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
├── interfaces/       # Protocol: GitHubClient, ReviewEngine, RepoFetcher, FileCollector,
│                     #            FindingVerifier (출처 grounding),
│                     #            FindingDeduper (history grounding),
│                     #            FindingResolutionChecker (후속 수정 follow-up)
├── domain/           # PullRequest (file_patches 포함), ReviewResult, Finding, FileDump,
│                     #  PostedReviewComment (frozen dataclass)
├── application/
│   ├── review_pr_use_case.py   # 오케스트레이션 (verify → dedupe → post → follow-up)
│   └── webhook_handler.py      # HMAC 검증 + 직렬화 큐 워커
├── infrastructure/
│   ├── github_app_client.py                # JWT → installation token → REST (+ reply API)
│   ├── git_repo_fetcher.py                 # clone/fetch/checkout
│   ├── file_dump_collector.py              # 필터 + 우선순위 + 토큰 예산
│   ├── gemini_prompt.py                    # 한국어 시스템 규칙 + 파일 직렬화 + diff 모드 prompt
│   ├── gemini_parser.py                    # JSON 추출 (코드펜스 스트립) + fallback
│   ├── gemini_cli_engine.py                # subprocess(gemini -p) — review() / review_diff()
│   ├── diff_parser.py                      # patch → addable lines + RIGHT-line annotated 포맷
│   ├── source_grounded_finding_verifier.py # Layer B: phantom-quote 출처 grounding 강등
│   ├── cross_pr_finding_deduper.py         # Layer D: 이전 push 와 중복 finding history 강등
│   └── diff_based_resolution_checker.py    # Layer E: 후속 push 에서 라인 수정 시 follow-up reply
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
| `GEMINI_FALLBACK_MODELS` | `gemini-2.5-pro` | `GEMINI_MODEL`이 429/capacity/preview unavailable, 또는 스트림 절단(`ERR_STREAM_PREMATURE_CLOSE` / `ECONNRESET`/`socket hang up`)로 실패할 때 재시도할 comma-separated 모델 목록 |
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
- 예산 초과 시:
  1. 변경 파일이 모두 들어갔으면 그대로 전체 리뷰 수행
  2. 변경 파일이 일부 잘려 나갔지만 GitHub `/files` 의 patch 가 살아 있으면 →
     **diff-only fallback 리뷰** 로 우회 (변경 라인의 unified diff 만 입력)
  3. patch 도 없거나 diff 자체가 컨텍스트 한도 초과 → 안내 코멘트만 게시

## 리뷰 출력 (3분류)

모델은 아래 JSON 스키마를 엄격히 따라야 합니다.

```json
{
  "summary": "...",
  "event": "COMMENT | REQUEST_CHANGES | APPROVE",
  "positives":    ["좋은 점 ..."],
  "improvements": ["개선할 점 (파일/모듈/아키텍처 단위) ..."],
  "comments": [
    {"path": "src/x.py", "line": 42, "body": "[Critical|Major|Minor|Suggestion] 기술 단위 코멘트 (라인 고정)"}
  ]
}
```

- `positives` → PR 리뷰 본문 "**좋은 점**" 섹션으로 렌더
- `improvements` → 본문 "**개선할 점**" 섹션으로 렌더
- `comments` → GitHub 인라인 리뷰 코멘트로 라인에 붙음 (**line 필수**, **`[등급]` 접두사 필수**)

### 라인 코멘트 등급 (`comments[].body` 접두사)

각 인라인 코멘트의 본문은 반드시 다음 4개 영문 태그 중 하나로 시작합니다:

| 태그 | 의미 |
|---|---|
| `[Critical]` | 반드시 막아야 하는 문제 — 장애 가능성 / 데이터 손실 / 보안 / 크래시 |
| `[Major]` | merge 전에 고치는 게 좋은 문제 — 버그 / 예외 누락 / 동시성 / 큰 테스트 누락 |
| `[Minor]` | 당장 큰 문제는 아니지만 개선 가치 — 가독성 / 중복 / 네이밍 |
| `[Suggestion]` | 선택 제안 — 더 나은 방식 / 취향 / 리팩터링 아이디어 |

`event` 결정은 등급 분포와 연동됩니다. `[Critical]` 이 하나라도 있으면 `REQUEST_CHANGES`,
없으면 `[Major]` 분포에 따라 `COMMENT`/`REQUEST_CHANGES`, 그 이하만 있으면 `COMMENT`/`APPROVE`.

> 운영 메모: 모델이 접두사를 누락하거나 임의 태그(`[Info]` 등) 를 만들면 `gemini_parser`
> 가 게시 자체는 진행하면서 WARN 로그로 관측합니다 (하드 드롭 안 함). 빈도가 높게 관찰
> 되면 정규화 레이어로 강화 검토.

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

## 환각 방어 + Conversation 추적 — 다층 후처리

대규모 모델은 동일 PR 의 연속 push 에 대해 같은 위치에 phantom whitespace/오타 같은 거짓
인용을 반복 보고하는 경향이 있습니다 (예: 실제 코드는 `"@scope"` 인데 모델이 `" @scope"`
앞에 공백이 있다고 단언). 본 봇은 환각을 **3 단계 (B/C/D)** 로 방어하고, 메인테이너가
실제로 라인을 수정한 경우 **Layer E** 가 부모 코멘트 thread 에 follow-up 대댓글을 게시
해 conversation 흐름을 닫습니다.

1. **프롬프트 가이드 (Layer C)** — `gemini_prompt.py` 의 "Phantom 공백·오타 환각" 섹션이
   모델에게 인용한 텍스트가 실제 라인에 그대로 있는지 재확인하도록 강하게 지시합니다.
2. **출처 grounding (Layer B)** — `infrastructure/source_grounded_finding_verifier.py` 의
   `SourceGroundedFindingVerifier` 가 모델 응답을 받은 뒤, `[Critical]/[Major]` 본문에
   "공백·띄어쓰기·오타 / whitespace·spacing·typo" 같은 단언 키워드가 있고 backtick 인용
   substring 이 있을 때 체크아웃된 실제 라인과 대조해 **모든 인용이 일치하지 않으면**
   `[Suggestion]` 으로 강등합니다 (strict-only 정책).
3. **History grounding (Layer D)** — `infrastructure/cross_pr_finding_deduper.py` 의
   `CrossPrFindingDeduper` 가 같은 PR 의 이전 push 에서 본 봇이 게시한 인라인 코멘트를
   GitHub API 로 조회해, 새 `[Critical]/[Major]` finding 이 동일 `(path, line, severity-stripped
   body)` 로 다시 등장하면 `[Suggestion]` 으로 강등합니다. "이전 push 에서 메인테이너가
   무시한 신호" 로 보고 alert fatigue 차단.
4. **Resolution follow-up (Layer E)** — `infrastructure/diff_based_resolution_checker.py`
   의 `DiffBasedResolutionChecker` 가 본 봇이 이전 push 에서 단 `[Critical]/[Major]`
   라인의 본문을 로컬 git checkout 으로 비교 (`comment.commit_id` vs `pr.head_sha`),
   변경됐으면 부모 코멘트 thread 에 "📌 라인이 변경되었습니다 — 이전 / 현재 본문 + 의도
   확인 부탁드립니다" 대댓글 게시. "메인테이너가 처리한 신호" 로 보고 일방적 라인 코멘트
   가 conversation 으로 이어지게 함.

강등이 발생하면 `event` 도 `_normalize_event` 가 재정합합니다 (Layer B/D 공통).

### Strict-only 정책 (Layer B) 의 의도된 trade-off

`[Critical] 공백 오타: 'usrname' → 'username' 으로 수정하세요` 처럼 **현재값** 과
**수정안** 을 둘 다 backtick 으로 인용하는 정상 오타 지적도 strict 매칭에선 강등됩니다
(수정안 `username` 이 라인에 없기 때문). lenient/fix-pattern 휴리스틱은 phantom + real
혼합 본문 우회를 막지 못해 (codex PR #23 review #4–#6) NLP 없이 정확한 구별이 불가
하다는 결론에 따라 strict-only 로 단순화했습니다 — false positive (정당 finding 강등)
비용을 받아들이고 phantom 차단을 우선합니다. 강등된 finding 도 본문/원래 등급은 보존돼
PR 작성자가 직접 판단할 수 있습니다.

### Exact-match 정책 (Layer D) 의 의도된 trade-off

dedup 시그니처는 `(path, line, severity-prefix-stripped body 의 strip 결과)` 정확 매칭.
- 등급만 바꾼 본문 (`[Critical]` → `[Major]`) 은 dedup 발동 (의도된 강등 — 모델의 등급
  흔들기 우회 차단).
- 단어 한 글자 다른 본문은 dedup 안 됨 (의도된 보수 — 퍼지 매칭은 정당한 별개 finding
  까지 묶을 위험).
- API 호출 실패 (네트워크/auth/rate-limit) 시 graceful degrade — dedup 만 잠시 작동하지
  않고 리뷰 게시는 진행. dedup 부재의 비용 (alert fatigue 일시 재현) < 리뷰 게시 실패 비용.

dedup 의 식별 기준은 `performed_via_github_app.id == settings.github_app_id` — 봇 이름 변경
에 강건하고 사람·다른 봇의 코멘트는 자동 제외됩니다.

### Diff-only 정책 (Layer E) 의 의도된 trade-off

대댓글 판정은 **로컬 git checkout 의 라인 본문 비교** 만 사용:
- 라인 본문이 두 SHA 사이에 변경됐으면 → 대댓글 (확정 톤이 아닌 "확인 부탁드립니다")
- 본문이 같으면 → 대댓글 안 함 (메인테이너 처리 신호 X)
- prior commit 이 로컬에 없는 경우 (force-push 후 unreachable 등) → graceful skip

모델로 "원래 finding 의 의도와 일치하는 수정인가?" 를 판정하는 AI 모드는 추가 호출 비용
+ 환각 위험으로 v1 에선 채택 안 함 — diff-only 결과를 메인테이너가 직접 판단하는 톤으로
표기. 본 봇 자신이 이미 단 follow-up 대댓글이 있으면 (`in_reply_to_id` 추적) 같은 push 에
서 또 reply 하지 않음 (중복 회피).

비-차단급 ([Minor] / [Suggestion]) 는 follow-up 대상에서 제외 — 무시되는 게 정상이라
"수정됐나?" 대댓글이 노이즈가 됨.

## 컨텍스트 예산 초과 시 — Diff-only fallback

전체 코드베이스가 `GEMINI_MAX_INPUT_TOKENS` 를 초과해 변경 파일이 dump 에 다 못 들어가는
경우, 본 봇은 리뷰를 완전히 건너뛰지 않고 **변경된 라인의 unified diff 만** 입력으로
좁힌 fallback 리뷰를 자동 시도합니다. 거대한 모노레포·생성 코드 폭주 PR 에서도 "리뷰
0건" 보다 "narrower 리뷰" 가 사용자 가치가 크기 때문입니다.

### 처리 흐름

1. `FileDumpCollector` 가 예산 초과를 보고하고 변경 파일 일부가 누락되면 `_fallback_to_diff_review`
   진입.
2. `pr.file_patches` (PR 페치 시 `/pulls/{n}/files` 응답에서 캐시) 를
   `format_patch_with_line_numbers` 로 RIGHT-line annotated 한 뒤
   `assemble_pr_diff` 가 file 헤더 + diff 본문으로 join.
3. 결과 diff text 가 `max_tokens × 4` chars 를 초과하면 fallback 도 포기 → notice 게시.
   초과 안 하면 `engine.review_diff(pr, diff_text)` 호출.
4. `build_diff_prompt` 가 `DIFF_MODE_NOTICE` (cross-file 단언 금지 + 차단 등급 절제) 로
   모델에게 제약을 명시.
5. 결과는 일반 흐름과 동일하게 `SourceGroundedFindingVerifier` (Layer B) 와
   `CrossPrFindingDeduper` (Layer D) 후처리를 거쳐 게시.

### Diff-only 모드의 의도된 trade-off

- ❌ 모델이 unchanged 코드 / 다른 모듈 영향 / 호출 그래프 / import 사용처 같은 cross-file
  단언을 할 수 없음 (정보 부재) → 그런 의심은 [Suggestion] 으로 강제.
- ✅ 변경 라인 자체의 명확한 버그 (오타·null 체크 누락·예외 미처리·동기화 오류 등) 는
  diff 만으로도 검증 가능 → [Critical]/[Major] 발행 가능.
- ✅ inline 코멘트는 patch annotated 라인 번호 (`  NNNNN| `) 그대로 사용 → 422 위험 없음.
- ✅ 후처리 검증 (Layer B/D) 은 diff 모드에서도 동일하게 작동 — 디스크 grounding 과 history
  grounding 은 입력 모드와 무관.

운영 로그: fallback 진입 시 `WARNING budget exceeded for ... — falling back to diff-only
review` 가 기록되니, 빈도 모니터링으로 `GEMINI_MAX_INPUT_TOKENS` 상향 또는
`.gemini-reviewignore` 추가 시점 판단 가능.

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
