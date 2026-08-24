# 0단계 사전 조사 — GitHub API 실측 기록

> 코드를 한 줄도 쓰기 전에 대상 API를 실제로 호출해서 확인한 기록입니다.
> 조사 일자: **2026-08-24** / 대상: `PrefectHQ/fastmcp` / 미인증(unauthenticated) 상태에서 총 19회 호출.
>
> 이 문서를 먼저 만든 이유는, 아래 함정 중 **6개 전부가 "그냥 짰으면 조용히 틀린 숫자를 내놓았을" 종류**이기 때문입니다.
> 테스트가 통과하고 대시보드에 그래프가 그려져도 값이 틀렸을 것이고, 틀렸다는 사실조차 몰랐을 겁니다.

## 목차

- [0. 요약](#0-요약)
- [1. 레이트리밋 정책 (실측)](#1-레이트리밋-정책-실측)
- [2. 대상 저장소 현황](#2-대상-저장소-현황)
- [3. 응답 스키마](#3-응답-스키마)
- [4. 발견한 함정 6가지](#4-발견한-함정-6가지)
- [5. 요청 예산](#5-요청-예산)
- [6. 설계 결정에 반영된 내용](#6-설계-결정에-반영된-내용)

---

## 0. 요약

| # | 함정 | 안 걸렀다면 생겼을 결과 |
|---|---|---|
| 1 | `/issues`가 PR도 함께 반환 | 표본의 **55%가 PR** — 지표 4개 전부 오염 |
| 2 | `since`는 `created_at`이 아니라 `updated_at` 기준 | "최근 6개월"에 2024년 이슈가 섞여 들어옴 |
| 3 | 첫 코멘트의 **50%가 봇** | 메인테이너 응답 속도가 실제보다 훨씬 빠르게 측정됨 |
| 4 | Link 헤더가 커서 기반, `rel="last"` 없음 | `page=1..N` 루프가 통하지 않음 |
| 5 | ETag가 `Accept` 헤더별로 다름 + 304도 쿼터 소모 | 증분 수집이 조용히 전체 재수집으로 퇴화 |
| 6 | Windows 기본 인코딩(cp949) | 이모지 포함 본문에서 `UnicodeDecodeError` |

---

## 1. 레이트리밋 정책 (실측)

`GET /rate_limit` 응답 기준.

| 리소스 | 미인증 (실측) | PAT 인증 |
|---|---|---|
| core (REST) | **60 / 시간** | 5,000 / 시간 |
| graphql | **0 — 사용 불가** | 5,000 포인트 / 시간 |
| search | 10 / 분 | 30 / 분 |

모든 응답에 아래 헤더가 함께 옵니다. 재시도 로직은 이 헤더를 그대로 읽습니다.

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 60
X-RateLimit-Used: 0
X-RateLimit-Reset: 1787574721   # epoch 초
X-RateLimit-Resource: core
```

**GraphQL은 미인증에서 `limit: 0`입니다.** 토큰 없이는 후보에서 아예 탈락합니다.
그리고 미인증 60/h로는 이슈 1페이지 + 코멘트 몇 건이면 소진되므로 **PAT는 선택이 아니라 전제**입니다.

## 2. 대상 저장소 현황

```
full_name         PrefectHQ/fastmcp
stars             27,355
license           Apache-2.0
language          Python
default_branch    main
created_at        2024-11-30
pushed_at         2026-08-22       ← 활발히 유지보수 중
open_issues_count 274              ← 주의: PR을 포함한 값이라 지표로 쓰면 안 됨
```

최근 6개월(2026-02-24 이후) 물량 — Search API `total_count` 기준:

| 쿼리 | 건수 |
|---|---|
| `is:issue created:>=2026-02-24` | **489** |
| `is:issue created:>=2026-02-24 state:open` | **90** |
| `is:issue updated:>=2026-02-24` | 573 |
| `is:pr created:>=2026-02-24` | 1,032 |

지표를 내기에 충분하면서 LLM 비용이 감당되는 규모입니다.

## 3. 응답 스키마

`GET /repos/{owner}/{repo}/issues` 아이템의 실제 top-level 키:

```
id, node_id, number, title, body, state, state_reason, locked, active_lock_reason,
created_at, updated_at, closed_at, closed_by, comments,
user{login,id,type,...}, author_association, labels[], assignee, assignees[],
milestone, reactions, draft, pull_request, type, issue_field_values,
timeline_url, comments_url, events_url, labels_url, html_url, url, repository_url,
performed_via_github_app
```

지표별 필드 매핑:

| 지표 | 사용 필드 | 추가 호출 |
|---|---|---|
| 방치된 이슈 비율 | `state`, `created_at`, `updated_at`, `comments` | 불필요 |
| 버그 vs 기능요청 비율 | `title`, `body` → LLM | 불필요 |
| 메인테이너 응답 속도 | `created_at` + 코멘트 `created_at` | **필요** (`/issues/{n}/comments`) |
| 감정 톤 분포 | `title`, `body` → LLM | 불필요 |

표본 45건(순수 이슈)의 필드 실태:

```
body is None          0건       ← body가 null인 경우는 없었음
body 빈 문자열         0건
body 길이             min 35 / 중앙값 1,140 / max 12,630 자
user is None          0건
labels 비어 있음       1건
comments == 0         8건
non-ASCII 포함 본문    10건      ← 이모지 등
state                 closed 44 / open 1
state_reason          completed 41 / duplicate 2 / not_planned 1 / null 1
author_association    NONE 29 / CONTRIBUTOR 13 / MEMBER 2 / COLLABORATOR 1
type                  전부 null  ← 새 "issue type" 기능 미사용 저장소
```

라벨 상위 분포 — LLM 분류 결과를 대조할 정답지로 쓸 수 있습니다(현재 스코프 밖, 기록만):

```
enhancement 24, server 20, bug 15, auth 13, client 5, http 5,
proposal 3, invalid 2, duplicate 2, documentation 2
```

**설계 반영:** `body`가 null이 아니라고 관측됐지만 스키마상 nullable이므로 Pydantic에서는 `str | None`으로 받고 저장 시 정규화합니다. 관측 표본 45건은 "그런 경우가 없다"의 근거로 부족합니다.

---

## 4. 발견한 함정 6가지

### 함정 1 — `/issues` 엔드포인트는 PR도 함께 반환한다

GitHub은 Pull Request를 Issue의 하위 타입으로 취급합니다. 그래서 이슈 목록 API가 PR을 같이 돌려줍니다.

```
한 페이지(100건) 중  PR 55건 / 순수 이슈 45건
```

**절반 이상이 PR입니다.** 거르지 않으면 4개 지표가 전부 오염됩니다. 특히 "버그 vs 기능요청 비율"은 PR 본문이 섞여 분모부터 틀립니다.

판별법은 아이템에 **`pull_request` 키가 존재하는지**입니다. 값이 아니라 키의 존재 여부입니다.

```python
is_pull_request = "pull_request" in item
```

저장소의 `open_issues_count`(274)도 같은 이유로 PR을 포함하므로, "열린 이슈 수"로 쓰면 안 됩니다.

### 함정 2 — `since` 파라미터는 `updated_at` 기준이다

`since=2026-02-24T00:00:00Z`로 요청했는데 응답에 들어 있던 `created_at`의 최솟값은 **2024-12-05**였습니다.

```
요청  since = 2026-02-24  (6개월 전)
응답  created_at 범위 = 2024-12-05 ~ 2026-03-03   ← 1년 8개월 전 이슈가 섞여 있음
      updated_at 범위 = 2026-02-24 ~ 2026-03-03   ← since가 실제로 거른 축
```

`since`는 "이 시각 이후에 **갱신된**" 이슈를 주지, "생성된" 이슈를 주지 않습니다. 오래전에 열렸지만 최근에 코멘트가 달린 이슈가 전부 딸려옵니다.

**이 프로젝트의 결정: `created_at` 기준으로 간다.**
`since`로는 넓게 받고(증분 수집에 `updated_at`이 필요하므로), **집계 시점에 `created_at`으로 필터링**합니다. 두 시각의 역할이 다릅니다:

- `updated_at` → **무엇을 가져올지** 정하는 축 (증분 수집)
- `created_at` → **무엇을 셀지** 정하는 축 (지표 집계)

이 둘을 하나로 합치려 하면 증분 수집이 깨지거나 지표가 틀립니다.

### 함정 3 — 첫 코멘트의 절반이 봇이다

"메인테이너 응답 속도 = 이슈 생성 → 첫 코멘트까지의 시간"을 그대로 구현하면 틀립니다. 이슈 10건의 첫 코멘트 작성자를 실제로 뽑아봤습니다.

```
issue   first commenter               user.type  author_association
#3288   jlowin                        User       MEMBER
#2652   coderabbitai[bot]             Bot        CONTRIBUTOR
#3291   sharabash                     User       NONE
#3292   marvin-context-protocol[bot]  Bot        CONTRIBUTOR
#3293   jlowin                        User       MEMBER
#3277   marvin-context-protocol[bot]  Bot        CONTRIBUTOR
#3302   jlowin                        User       MEMBER
#3304   marvin-context-protocol[bot]  Bot        CONTRIBUTOR
#3305   jlowin                        User       MEMBER
#3296   marvin-context-protocol[bot]  Bot        CONTRIBUTOR

→ User 5 / Bot 5
```

**문제가 둘입니다.**

1. **봇이 절반입니다.** CI 봇과 리뷰 봇이 사람보다 먼저 응답합니다. 그대로 재면 응답 속도가 실제보다 극적으로 빨라집니다.
2. **`author_association`만으로는 봇을 못 거릅니다.** 위 표를 보면 봇들의 association이 전부 `CONTRIBUTOR`입니다. "CONTRIBUTOR 이상이면 메인테이너"라는 흔한 판별이 통하지 않습니다.

정확한 판별자는 **`user.type == "Bot"`** 이었습니다(표본 5/5에서 `login`의 `[bot]` 접미사와 일치). `login.endswith("[bot]")`도 같은 결과를 주지만, 사람이 그런 이름을 쓸 수 있으므로 `user.type`을 1차 기준으로 씁니다.

**세 번째 문제**도 있습니다. `#3291`의 첫 응답자 `sharabash`는 `user.type == "User"`이지만 `author_association == "NONE"` — **지나가던 제3자**입니다. 메인테이너가 아닙니다.

그래서 이 프로젝트의 "메인테이너 첫 응답" 정의는:

```python
is_maintainer_response = (
    comment.user.type == "User"                                           # 봇 제외
    and comment.author_association in {"OWNER", "MEMBER", "COLLABORATOR"}  # 제3자 제외
    and comment.user.login != issue.user.login                            # 작성자 self-reply 제외
)
```

세 조건 모두 필요합니다. 하나라도 빠지면 지표가 틀립니다.

> **부수 효과:** 이 정의를 쓰면 "메인테이너가 끝내 응답하지 않은 이슈"가 생깁니다.
> 이건 결측이 아니라 **의미 있는 값**이므로, 중앙값 계산에서 제외하되 별도로 건수를 보고합니다.
> 0으로 채우거나 조용히 버리면 안 됩니다.

### 함정 4 — 페이지네이션이 커서 기반이라 `rel="last"`가 없다

이슈 목록의 Link 헤더:

```
Link: <https://api.github.com/repositories/896296825/issues
       ?state=all&since=...&per_page=100&page=2
       &after=Y3Vyc29yOnYyOpLPAAABnLW2JLjO74rQJw%3D%3D>; rel="next"
```

`rel="next"` **하나뿐**이고 `rel="last"`가 없습니다. URL에 불투명한 `after=` 커서가 붙어 있습니다.

- 총 페이지 수를 미리 알 수 없습니다 → 진행률 표시를 만들 수 없습니다.
- `for page in range(1, N)` 방식의 흔한 구현이 통하지 않습니다. `page` 번호만 올리고 커서를 빼먹으면 잘못된 구간을 받습니다.
- **`rel="next"` URL을 통째로 그대로 따라가야 합니다.** 파라미터를 재조립하지 말 것.

반면 **코멘트 엔드포인트는 오프셋 기반이고 `rel="last"`가 있습니다**:

```
Link: <.../comments?per_page=1&page=2>; rel="next",
      <.../comments?per_page=1&page=3>; rel="last"
```

**엔드포인트마다 페이지네이션 방식이 다릅니다.** 하나를 보고 일반화하면 안 됩니다. 공용 페이지네이터는 `rel="next"`만 신뢰하도록 만듭니다 — 두 방식 모두에서 동작하는 유일한 규칙입니다.

### 함정 5 — ETag는 `Accept` 헤더별로 다르고, 304도 쿼터를 소모했다

증분 수집의 핵심인 조건부 요청을 확인하다 두 가지를 발견했습니다.

**(1) ETag는 요청 헤더 조합에 종속됩니다.**

```
Accept: application/vnd.github+json 으로 받은 ETag를
  Accept 없이 If-None-Match로 보냄   → 200 OK   (전체 응답 재수신)
  같은 ETag를 Accept 포함해서 보냄    → 304 Not Modified
```

에러가 나지 않습니다. **조용히 200이 돌아오고 전체 본문을 다시 받습니다.** 증분 수집이 작동하는 것처럼 보이면서 실제로는 매번 전체를 재수집합니다 — 로그를 봐도 정상으로 보입니다.
→ ETag를 저장할 때 **어떤 요청에 대한 ETag인지**를 함께 고정해야 합니다.

**(2) 304도 `X-RateLimit-Used`를 증가시켰습니다.**

```
조건부 요청 전            X-RateLimit-Used: 18
304 Not Modified 수신 후  X-RateLimit-Used: 19
```

GitHub 공식 문서는 304가 레이트리밋에 계상되지 않는다고 설명하지만, **실측은 반대**였습니다.
→ **"304는 공짜"라는 가정 위에 예산을 세우지 않습니다.** ETag의 이득은 대역폭과 파싱 비용 절감으로만 계산하고, 요청 수 예산은 304를 1회로 세어 보수적으로 잡습니다.

> 문서와 실제가 다를 때는 실제를 따릅니다. 다만 관측이 프록시 등 환경 영향일 가능성이 있으므로,
> 이 항목은 "확정된 GitHub 동작"이 아니라 **"이 환경에서의 관측"**으로 기록합니다.

### 함정 6 — Windows 기본 인코딩(cp949)이 응답을 깨뜨린다

응답 JSON을 파싱하다 실제로 터진 에러입니다.

```
UnicodeDecodeError: 'cp949' codec can't decode byte 0xf0 in position 1145:
illegal multibyte sequence
```

원인은 Python이 Windows에서 파일을 열 때 **로캘 기본 인코딩(이 PC는 cp949)** 을 쓰기 때문입니다. 저장소 description이 `🚀`로 시작하고, 이슈 본문 45건 중 **10건에 non-ASCII 문자**가 들어 있습니다.

- 파일 I/O에는 **항상 `encoding="utf-8"`을 명시**합니다. 기본값에 기대지 않습니다.
- `alembic.ini`는 시스템 로캘로 읽히므로 **ASCII만** 넣습니다(한글 주석은 `migrations/env.py`에).

이 프로젝트의 원칙 중 "인코딩 문제를 조용히 넘기지 않는다"에 정확히 해당하는 실제 사례라, 별도 항목으로 남깁니다.

---

## 5. 요청 예산

PAT 인증(5,000 req/h) 기준 초기 전량 수집:

| 단계 | 요청 수 | 산출 근거 |
|---|---|---|
| 이슈 목록 | ~17 | (이슈 573 + PR 약 1,100) ÷ 100/page |
| 코멘트 | ~489 | 이슈 1건당 첫 페이지 1회 |
| **초기 합계** | **~510** | 시간당 한도의 **약 10%** |
| 이후 증분 | 수십 건 | 변경된 이슈만 |

1시간 안에 여유롭게 완주합니다.

**따라서 레이트리밋 재시도 로직은 정상 운영에서 거의 발동하지 않습니다.**
"돌려보니 잘 되더라"로는 이 코드가 검증되지 않습니다. 429와 `Retry-After`를 테스트에서 인위적으로 주입해 검증해야 합니다.

## 6. 설계 결정에 반영된 내용

| 조사 결과 | 설계 결정 |
|---|---|
| 미인증 60/h, GraphQL 0 | PAT 필수. `.env` + `.env.example`, `.gitignore`에 `.env` |
| 요청 예산이 한도의 10% | GraphQL 불필요. REST로 충분 |
| 커서/오프셋 혼재 (함정 4) | `rel="next"`만 따라가는 공용 페이지네이터 |
| ETag 헤더 종속성 (함정 5) | ETag를 요청 식별자와 함께 저장 |
| 304도 쿼터 소모 (함정 5) | 요청 예산에서 304를 1회로 계산 |
| PR 55% 혼입 (함정 1) | `pull_request` 키로 필터, 저장 자체를 안 함 |
| `since`=updated_at (함정 2) | 수집은 `updated_at`, 집계는 `created_at`으로 축 분리 |
| 봇 50%, 제3자 응답 (함정 3) | 메인테이너 첫 응답에 3중 조건 적용 |
| non-ASCII 10/45 (함정 6) | 파일 I/O에 `encoding="utf-8"` 명시 |
| 재시도가 자연 발동 안 함 | 429/`Retry-After`를 테스트에서 주입해 검증 |

---

## 재현 방법

이 문서의 수치는 아래로 다시 확인할 수 있습니다(미인증도 동작하나 60 req/h 한도에 유의).

```bash
curl -s -H "Accept: application/vnd.github+json" https://api.github.com/rate_limit
```

```bash
curl -s -D - -o /dev/null -H "Accept: application/vnd.github+json" "https://api.github.com/repos/PrefectHQ/fastmcp/issues?state=all&since=2026-02-24T00:00:00Z&sort=updated&direction=asc&per_page=100"
```

```bash
curl -s -H "Accept: application/vnd.github+json" "https://api.github.com/search/issues?q=repo:PrefectHQ/fastmcp+is:issue+created:%3E%3D2026-02-24&per_page=1"
```
