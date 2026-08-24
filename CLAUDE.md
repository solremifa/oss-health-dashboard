# CLAUDE.md

OSS 저장소 헬스체크 대시보드의 작업 규약 문서입니다.
Claude Code가 이 저장소에서 코드를 작성할 때 반드시 이 문서를 기준으로 삼습니다.

이 프로젝트는 `stay-reviews`(hw03)의 규약을 계승합니다. 거기서 이미 검증된 규칙은
재발명하지 않고 그대로 가져왔고, 이 프로젝트에 고유한 것만 따로 표시했습니다.

---

## 1. 프로젝트 개요

GitHub 저장소의 이슈를 **수집 → 검증 → 저장 → LLM 분석 → 지표 계산 → API/프론트 노출**
하는 도구. 대상은 `PrefectHQ/fastmcp` 한 곳입니다.

```
GitHub API → collectors → models(DB) → analysis → api → frontend
```

**0단계 사전 조사 결과가 `docs/findings.md`에 있습니다. 수집 관련 코드를 쓰기 전에
반드시 읽으세요.** 거기 적힌 함정 6가지는 전부 "그냥 짰으면 조용히 틀린 숫자가
나왔을" 종류입니다.

## 2. 스코프 — 지표 4개로 고정

아래 4개만 구현합니다. 전부 `created_at`이 최근 `WINDOW_DAYS`(기본 180일) 안인
이슈만 대상입니다.

| 지표 | 정의 |
|---|---|
| 방치된 이슈 비율 | `state=open` AND `now - created_at ≥ STALE_DAYS` AND 메인테이너 응답 없음 **/** open 이슈 전체 |
| 버그 vs 기능요청 비율 | LLM이 분류한 `category` 분포 **/** 분석 완료 건수 |
| 메인테이너 응답 속도 | `median(첫 메인테이너 응답 시각 - created_at)` |
| 이슈 감정 톤 분포 | LLM이 판정한 `sentiment` 분포 |

**종합 헬스 스코어 같은 가중치 알고리즘을 만들지 마세요.** 4개 숫자를 그대로
보여줍니다. 이 범위를 벗어나는 지표가 필요하다고 판단되면 구현 전에 먼저 물어보세요.

두 가지를 명시적으로 하지 않습니다:

- **"메인테이너 응답 없음"을 0으로 채우지 않습니다.** 중앙값에서 제외하되 건수를
  별도로 노출합니다. 결측이 아니라 의미 있는 값입니다.
- **미분석 이슈를 분모에서 조용히 빼지 않습니다.** 응답에 건수를 드러냅니다.

## 3. 기술 스택

| 항목 | 선택 |
|---|---|
| 언어 | Python 3.12 (이 PC에 3.11이 없어 3.12로 확정) |
| HTTP 클라이언트 | **`httpx2`** (`import httpx2`) — `httpx`는 deprecated |
| 웹 프레임워크 | FastAPI |
| 검증/직렬화 | Pydantic **v2** + pydantic-settings |
| LLM | Anthropic API (`anthropic` 1.x) — 모델 **`claude-opus-5`** |
| ORM | SQLAlchemy 2.x |
| DB | SQLite (초기) → PostgreSQL (추후 이관) |
| 마이그레이션 | Alembic |
| 린트·포맷 | ruff |
| 테스트 | pytest |
| CI | GitHub Actions |
| 프론트엔드 | 순수 HTML/JS/CSS + 손으로 쓴 SVG (차트 라이브러리 없음) |

### PyGithub이 아니라 httpx2를 쓰는 이유

이 프로젝트에서 설명할 가치가 있는 부분이 **커서 페이지네이션, ETag 조건부 요청,
레이트리밋 백오프**인데 PyGithub은 이걸 전부 내부에 감춥니다. 라이브러리가 대신
해주면 "증분 수집을 어떻게 설계했나"에 답할 내용이 사라집니다. 추가로:

- PyGithub의 자체 객체 모델이 Pydantic 검증 레이어와 이중화됩니다.
- `httpx2.MockTransport`로 429·5xx·깨진 JSON을 몇 줄로 주입할 수 있어 실패 경로
  테스트 비용이 결정적으로 낮습니다.

GraphQL은 미인증에서 `limit: 0`이고, REST 예산이 시간당 한도의 10%라 이득이 없습니다.

### DB 이관 대비 규칙

- 접속 문자열은 **`DATABASE_URL` 환경변수 하나로만** 결정합니다. 경로 하드코딩 금지.
- **SQLite 전용 SQL 문법 금지** (`INSERT OR REPLACE`, `AUTOINCREMENT` 등).
- 원시 SQL보다 SQLAlchemy 표현식을 우선합니다. 필요하면 이유를 주석으로 남깁니다.
- 모든 타임스탬프는 **timezone-aware UTC**로 저장합니다. naive datetime 금지.

## 4. 디렉토리 레이아웃

```
oss-health-dashboard/
├── CLAUDE.md
├── README.md / README.ja.md
├── pyproject.toml       # 의존성·빌드·pytest·ruff 설정 (단일 소스)
├── alembic.ini          # ASCII 전용 — 시스템 로캘(cp949)로 읽힌다
├── .env.example         # .env는 절대 커밋하지 않는다
├── docs/findings.md     # 0단계 사전 조사 기록
├── migrations/          # Alembic (env.py + versions/)
├── app/
│   ├── config.py        # pydantic-settings 기반 설정
│   ├── logging.py       # 로깅 설정
│   ├── collectors/      # GitHub API 수집 → 공통 스키마 정규화
│   ├── models/          # SQLAlchemy ORM + Pydantic 스키마 + repository
│   ├── analysis/        # classify.py(LLM) + metrics.py(순수 함수)
│   └── api/             # FastAPI 앱·라우터·의존성·응답 모델
├── fixtures/            # 실제 API 응답 스냅샷 + 깨진 샘플
├── scripts/             # 사람이 직접 실행하는 진입점
├── tests/               # app/ 구조를 그대로 반영
└── frontend/            # index.html / app.js / styles.css
```

**레이아웃 밖에 파일을 만들지 마세요.** 새 최상위 디렉토리가 필요하면 먼저 물어보세요.

## 5. 레이어 의존 규칙

의존 방향은 한 방향입니다. 역방향 import는 금지합니다.

```
collectors ──┐
             ├──→ models
api ──→ analysis ──┘
```

- `models/`는 **다른 어떤 레이어도 import하지 않습니다.**
- `collectors/`는 `models/`의 스키마만 참조하고 **DB에 직접 쓰지 않습니다.**
- `analysis/`의 `metrics.py`는 순수 함수입니다. `classify.py`는 LLM 호출로 I/O가
  있지만, 핵심 변환 함수는 DB를 모르고 세션을 만지는 것은 배치 러너 하나뿐입니다.
- `api/`는 모든 레이어를 쓸 수 있지만 스스로 로직을 갖지 않습니다.
- 순환 import가 생기면 우회하지 말고 책임 배치를 다시 검토하세요.

## 6. 수집 규칙 — docs/findings.md에서 온 것

**이 절의 항목은 전부 실측 근거가 있습니다. 임의로 바꾸지 마세요.**

- **PR을 저장하지 않습니다.** `/issues` 응답의 절반 이상이 PR입니다. 아이템에
  `"pull_request" in item`이면 버립니다. 값이 아니라 **키의 존재 여부**로 판별합니다.
  `is_pull_request` 플래그 컬럼도 두지 않습니다 — 지표 4개가 전부 이슈 기반이라
  PR 행은 영원히 안 읽히는 죽은 데이터가 됩니다.
- **`repository.open_issues_count`를 "열린 이슈 수"로 쓰지 마세요.** PR을 포함합니다.
- **수집 축과 집계 축이 다릅니다.** `since`(=`updated_at`)로 넓게 받고, 집계할 때
  `created_at`으로 다시 거릅니다. 둘을 하나로 합치려 하면 증분 수집이 깨지거나
  지표가 틀립니다.
- **페이지네이션은 `rel="next"` URL을 통째로 따라갑니다.** 이슈 목록은 커서 기반이라
  `rel="last"`가 없고, 코멘트는 오프셋 기반입니다. 엔드포인트마다 다르므로
  `rel="next"`만 신뢰합니다. `page` 번호를 재조립하지 마세요.
- **ETag는 요청 지문(fingerprint)과 함께 저장합니다.** `Accept` 헤더가 다르면 같은
  ETag로도 304가 아니라 200이 오고, **에러 없이 전체를 다시 받습니다.**
- **304도 레이트리밋을 소모하는 것으로 계산합니다.** 문서와 실측이 달랐습니다.
- **메인테이너 첫 응답은 세 조건을 모두 만족해야 합니다.**
  `user.type == "User"` (봇 제외 — 첫 코멘트의 50%가 봇이고 봇의
  `author_association`은 `CONTRIBUTOR`라 association만으로는 못 거릅니다) AND
  `author_association in {OWNER, MEMBER, COLLABORATOR}` (제3자 제외) AND
  작성자 본인의 self-reply가 아닐 것.
- **재시도는 상한이 있습니다.** 무한 재시도 금지. 상한 초과 시 예외를 올리고
  ERROR 로그를 남깁니다.

## 7. DB 레이어 규칙

- **데이터 무결성은 DB 제약이 최종 판정자입니다.** 저장 전 SELECT로 확인하는 방식은
  확인과 삽입 사이에 다른 트랜잭션이 끼어들어 동시 수집 시 샙니다. 제약을 걸고
  코드는 `IntegrityError`를 처리만 합니다.
- **SQLite는 FK를 기본으로 강제하지 않습니다.** 연결마다 `PRAGMA foreign_keys=ON`을
  거는 리스너를 전역 등록합니다. 제거하면 FK가 장식이 되고 PostgreSQL로 옮긴 뒤에야
  터집니다.
- **`IntegrityError`를 뭉뚱그려 잡지 마세요.** UNIQUE 위반만 골라 처리하고 FK·NOT NULL
  위반은 올립니다.
- **제약 위반은 `session.begin_nested()`(SAVEPOINT) 안에서 처리합니다.** 그냥 잡으면
  세션 전체가 무효화되어 이후 저장이 모두 실패합니다.
- **제약에 NULL이 들어가는 컬럼을 쓰지 마세요.** NULL끼리는 서로 다르다고 보아
  UNIQUE가 조용히 무력화됩니다. API의 `body: null`은 `""`로 정규화해 NOT NULL로 둡니다.
- **`repository`는 커밋하지 않습니다.** 트랜잭션 경계는 호출자가 정합니다.
- **datetime 컬럼은 `UtcDateTime`을 씁니다.** SQLite는 시간대를 저장하지 못해 naive로
  돌려줍니다. 맨 `DateTime`을 쓰면 DB 종류에 따라 스키마 검증이 깨집니다.
- **enum 컬럼은 `Enum(..., native_enum=False, create_constraint=True, values_callable=...)`**
  셋 다 필요합니다. `create_constraint`는 SQLAlchemy 1.4부터 기본값이 False라 빼먹으면
  CHECK 없는 맨 VARCHAR가 되고, `values_callable`이 없으면 이름이 저장되어 값과
  어긋납니다.
- **enum에 값을 추가하려면 마이그레이션도 함께 고칩니다.** CHECK 제약이 마이그레이션에
  박혀 있어 enum만 고치면 저장 시점에 터집니다.
- **스키마를 바꾸면 마이그레이션을 함께 만듭니다.** ORM만 고치면 `create_all()`로 만든
  테스트 DB에서는 통과하고 배포 DB에서만 깨집니다.
- **`issue_first_responses`를 `issues`의 컬럼으로 합치지 마세요.** 합치면 "아직 조사
  안 함"과 "조사했는데 메인테이너 응답이 없음"이 둘 다 NULL이 되어 구분할 수 없습니다.
  행의 존재 = 조사 완료, `responded_at IS NULL` = 응답 없음입니다.

## 8. Anthropic API 사용 규칙

- **모델은 `claude-opus-5`.** 모델 ID에 날짜 접미사를 붙이지 않습니다.
- **구조화 출력(`output_config.format`)을 씁니다.** 프롬프트로만 "JSON만 내놔"라고
  하는 것보다 API가 스키마를 강제하는 쪽이 확실합니다.
- **분류 값은 자유 문자열이 아니라 enum으로 받습니다.** 허용 목록은 **한 곳에서
  파생**시켜 프롬프트·API 스키마·Pydantic·DB CHECK 네 곳이 어긋나지 않게 합니다.
- **`stop_reason`을 먼저 확인하고 `content`를 읽습니다.** `refusal`이면 `content`가
  비어 있고, `max_tokens`면 잘린 본문이 옵니다.
- **파싱·검증 실패는 재시도하고, API 오류는 재시도하지 않습니다.** 레이트리밋과 5xx는
  SDK가 이미 재시도하므로 다시 감싸면 재시도가 중첩됩니다.
- **재시도를 소진한 건은 ERROR로 로깅하고 건너뜁니다.** 그 이슈는 미분석으로 남고
  지표 응답에 건수가 드러납니다.
- **분석 결과에 `model`과 `prompt_version`을 함께 저장합니다.** 프롬프트나 모델이
  바뀌면 결과가 달라지므로 섞인 데이터를 나중에 구분할 수 있어야 합니다.
- **테스트는 API를 호출하지 않습니다.** 대역(fake client)에 응답을 미리 짜 넣습니다.
  네트워크·API 키에 의존하는 테스트를 만들지 마세요.

## 9. 코딩 컨벤션

### 타입힌트 · Docstring — 필수

모든 함수·메서드에 인자와 반환값 타입힌트를 빠짐없이 붙입니다(반환 없으면 `-> None`).
ruff의 `ANN` 규칙으로 강제합니다.

Docstring은 **Google 스타일**로 통일합니다. 요약 1줄 → 빈 줄 → `Args:` / `Returns:` /
`Raises:` 순서, 해당 항목이 없으면 생략.

### Pydantic v2 관례

v1 API는 사용하지 않습니다.

| 금지 (v1) | 사용 (v2) |
|---|---|
| `@validator` | `@field_validator` / `@model_validator` |
| `.dict()` | `.model_dump()` |
| `.json()` | `.model_dump_json()` |
| `.parse_obj()` | `.model_validate()` |
| `class Config:` | `model_config = ConfigDict(...)` |

### 네이밍

모듈·함수·변수 `snake_case` / 클래스 `PascalCase` / 상수 `UPPER_SNAKE_CASE` /
비공개 접두사 `_`.

### 예외 처리

- 예외를 조용히 삼키지 않습니다. `except: pass` 금지.
- 광범위한 `except Exception`은 최상위 경계(수집 루프, 라우터)에서만, 로깅과 함께.
- 수집처럼 **부분 실패가 정상인 작업**은 실패를 감추지 말고 결과 객체에 명시적으로
  담아 반환합니다.

### 인코딩

- **파일 I/O에는 항상 `encoding="utf-8"`을 명시합니다.** 기본값에 기대지 않습니다.
  이 PC의 기본 인코딩은 cp949이고, 이슈 본문에는 이모지가 흔합니다.
- **`alembic.ini`에는 ASCII만** 넣습니다. 한글 주석은 `migrations/env.py`에.

## 10. 테스트 규칙

- `app/` 구조를 그대로 반영합니다 (`app/collectors/client.py` → `tests/test_client.py`).
- **실패 경로를 성공 경로만큼 검증합니다.** 반드시 커버할 시나리오:
  API 실패(4xx/5xx), 레이트리밋(429 + `Retry-After`), 재시도 상한 초과,
  잘못된 데이터(필수 필드 누락, `body: null`, 이모지), LLM 응답 파싱 실패,
  DB 제약 위반.
- **레이트리밋 재시도는 정상 운영에서 거의 발동하지 않습니다.** 실제로 돌려보는
  것으로는 검증되지 않으니 429를 인위적으로 주입하세요.
- **네트워크와 API 키에 의존하는 테스트를 만들지 마세요.** `httpx2.MockTransport`와
  LLM 대역을 씁니다.
- SQLite 인메모리 + FastAPI TestClient는 **`StaticPool`이 필요합니다.**
  `create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)`
  없이는 스레드 오류로 터집니다.

## 11. Git / 커밋 규약

### 이슈 → 브랜치 → PR

기능 단위로 **GitHub 이슈를 먼저 만들고**, 각 이슈를 브랜치+PR로 닫습니다.

### 커밋 메시지

**Conventional Commits**: `feat:` `fix:` `test:` `docs:` `ci:` `chore:` `refactor:`

**버그를 고칠 때는 "왜 발생했는지 → 어떻게 재현했는지 → 어떻게 고쳤는지"를 짧게
남깁니다.** 나중에 면접에서 "문제 해결 경험"을 설명할 때 그대로 쓸 자료입니다.

대안을 두고 고른 설계(정규화 vs JSON, 커서 vs 오프셋 같은)는 이유를 남깁니다.
코드만 보고는 알 수 없습니다.

### 커밋 리듬

- **커밋 하나 = 검증된 한 단계.** `ruff check`와 `pytest`가 통과한 뒤에 커밋합니다.
- **다음 단계를 이전 커밋에 얹지 않습니다.** 어떤 커밋을 체크아웃해도 테스트가 도는
  상태를 유지합니다.
- **함께 바뀌어야 하는 것은 같은 커밋에 담습니다.** 모델 변경과 마이그레이션,
  코드 변경과 테스트, 규약 변경과 CLAUDE.md 갱신은 쪼개지 않습니다.
- **커밋 전에 `git status`로 의도치 않은 파일이 섞였는지 확인합니다.** 특히 `.env`.

## 12. 실행 / 테스트 명령

프로젝트 전용 venv는 `oss-health-dashboard/.venv` (Python 3.12)입니다.
상위 폴더의 venv는 다른 프로젝트와 공유되므로 사용하지 않습니다.

```powershell
# 최초 1회: 개발 의존성 포함 editable 설치
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 린트 · 포맷
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format .

# 테스트
.\.venv\Scripts\python.exe -m pytest

# 특정 테스트만
.\.venv\Scripts\python.exe -m pytest tests/test_config.py -k encoding
```

editable로 설치하므로 `PYTHONPATH` 설정 없이 `app` 패키지가 import됩니다.

**의존성은 `pyproject.toml`이 단일 소스입니다.** `pip install`만 하지 말고
`[project] dependencies`(런타임) 또는 `[project.optional-dependencies] dev`(개발 전용)에
반드시 추가하세요.

**아직 없는 것:** Alembic 설정(`alembic.ini`, `migrations/`), 개발 서버 실행 명령,
배포. 도입하면 이 절을 갱신하세요. **검증하지 않은 명령을 여기에 적지 마세요.**

## 13. Claude 작업 규칙

- **미정 사항을 지어내지 않습니다.** "미정"으로 표시된 항목은 임의로 결정하지 말고
  사용자에게 확인합니다.
- **스키마 변경은 짝으로 갱신합니다.** ORM 모델을 고치면 대응하는 Pydantic 스키마와
  마이그레이션도 함께 고칩니다.
- **문서를 코드와 함께 갱신합니다.** 구조·규약이 바뀌면 이 CLAUDE.md도 같은 작업에서
  수정합니다. 각 마일스톤이 끝나면 README를 갱신합니다.
- **스코프를 넓히지 않습니다.** 2절의 지표 4개를 벗어나는 것은 구현 전에 물어보세요.

## 14. 나중에 할 것 (지금 하지 말 것)

분석 결과를 Slack/Discord로 보내는 **MCP 서버** 연동. 핵심 파이프라인(수집 → 저장 →
분석 → 지표 → API → 대시보드)이 완결되기 전까지 착수하지 않습니다.
