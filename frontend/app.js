/**
 * 지표 응답을 받아 화면을 그린다. 계산은 하지 않는다.
 *
 * 비율도 중앙값도 서버가 이미 냈다(`app/analysis/metrics.py`). 여기서 다시
 * 나누기 시작하면 같은 지표의 정의가 두 곳에 생기고, 언젠가 한쪽만 바뀐다.
 * 이 파일이 하는 일은 **값을 픽셀로 옮기는 것**과 **값이 없는 이유를 구분해
 * 보여주는 것**뿐이다.
 *
 * ## innerHTML을 쓰지 않는다
 *
 * 저장소 이름이 URL 쿼리(`?repo=`)에서 온다. 링크 한 줄로 남에게 건네질 수 있는
 * 값이라, 응답이든 쿼리든 화면에 넣을 때는 전부 `textContent`로 넣는다.
 *
 * ## 도넛의 반지름이 15.9154...인 이유
 *
 * 둘레가 정확히 100이 되는 반지름이다(`2πr = 100`). `stroke-dasharray`에 넣는
 * 길이가 그대로 백분율이 되어 삼각함수도 호(arc) 경로도 필요 없어진다.
 */

import {
  EMPTY,
  MetricState,
  barWidths,
  distributionEntries,
  donutSegments,
  formatCount,
  formatDuration,
  formatPercent,
  formatTimestamp,
  metricState,
} from "./format.js";

const SVG_NS = "http://www.w3.org/2000/svg";

/** 둘레가 100이 되는 반지름. dasharray 값이 곧 백분율이다. */
const DONUT_RADIUS = 50 / Math.PI;
const DONUT_CIRCUMFERENCE = 100;
const DONUT_CENTER = 21;
const DONUT_STROKE = 6;

/**
 * 그릴 순서. JSON 키 순서에 맡기지 않는다 -- 서버가 필드를 재정렬하면 범례
 * 색과 막대가 조용히 어긋난다.
 */
const CATEGORY_ORDER = ["bug", "feature_request", "question", "other"];
const SENTIMENT_ORDER = ["positive", "neutral", "frustrated"];

const LABELS = {
  bug: "버그",
  feature_request: "기능 요청",
  question: "질문",
  other: "기타",
  positive: "긍정",
  neutral: "중립",
  frustrated: "답답함",
};

/** 상태 전환기가 읽어도 되는 스냅샷 이름. 쿼리 값을 경로에 그대로 넣지 않는다. */
const FIXTURES = new Set(["ready", "pending", "empty"]);

const REPO_STORAGE_KEY = "oss-health-dashboard:repo";

/** `?repo=`도 저장된 값도 없을 때 초기 화면에서 제안하는 저장소. */
const SUGGESTED_REPO = "PrefectHQ/fastmcp";

// ---------------------------------------------------------------------------
// DOM 헬퍼
// ---------------------------------------------------------------------------

/**
 * 요소를 만든다.
 *
 * @param {string} tag 태그 이름.
 * @param {Object} [attrs] 속성. `class`·`data-*` 모두 그대로 쓴다.
 * @param {string} [text] 넣을 글자. `textContent`로 넣는다.
 * @returns {HTMLElement} 만들어진 요소.
 */
function el(tag, attrs = {}, text = null) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  }
  if (text !== null) node.textContent = text;
  return node;
}

/**
 * SVG 요소를 만든다. `createElement`로는 SVG가 만들어지지 않는다 -- 네임스페이스가
 * 달라 화면에 아무것도 그려지지 않고 오류도 나지 않는다.
 *
 * @param {string} tag 태그 이름.
 * @param {Object} [attrs] 속성.
 * @param {string} [text] 넣을 글자.
 * @returns {SVGElement} 만들어진 요소.
 */
function svg(tag, attrs = {}, text = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(name, String(value));
  }
  if (text !== null) node.textContent = text;
  return node;
}

/**
 * 카드 안의 이름표 붙은 자리를 찾는다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {string} field `data-field` 값.
 * @returns {HTMLElement} 찾은 요소.
 */
function slot(card, field) {
  return card.querySelector(`[data-field="${field}"]`);
}

/** 자식을 전부 지운다. `innerHTML = ""`보다 의도가 분명하다. */
function clear(node) {
  node.replaceChildren();
}

// ---------------------------------------------------------------------------
// 카드 공통
// ---------------------------------------------------------------------------

/** 상태별 배지 글자. `ready`는 배지를 달지 않는다 -- 정상이 기본값이다. */
const BADGE_TEXT = {
  [MetricState.PENDING]: "수집 전",
  [MetricState.UNAVAILABLE]: "계산 불가",
};

/**
 * 상태별 설명 문구.
 *
 * 두 문장이 **다른 말을 해야 한다.** 하나는 기다리라는 뜻이고 하나는 기다려도
 * 소용없다는 뜻인데, 둘 다 "값 없음"으로 쓰면 사용자가 무엇을 해야 할지 모른다.
 */
const NOTE_TEXT = {
  [MetricState.PENDING]: "아직 수집하지 않았습니다. 수집이 끝나면 값이 생깁니다.",
  [MetricState.UNAVAILABLE]: "분모가 0이라 비율을 낼 수 없습니다. 기다려도 값이 생기지 않습니다.",
};

/**
 * 카드의 상태를 화면에 반영한다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {string} state `MetricState` 중 하나.
 * @param {string} [noteOverride] 기본 문구 대신 쓸 설명.
 * @returns {void}
 */
function setCardState(card, state, noteOverride = null) {
  card.dataset.state = state;

  const badge = slot(card, "badge");
  const badgeText = BADGE_TEXT[state];
  badge.hidden = badgeText === undefined;
  badge.textContent = badgeText ?? "";
  badge.dataset.state = state;

  const note = slot(card, "note");
  const noteText = noteOverride ?? NOTE_TEXT[state];
  note.hidden = noteText === undefined;
  note.textContent = noteText ?? "";
  note.dataset.state = state;
}

/**
 * 건수 목록을 채운다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {Array<{label: string, value: number, excluded?: boolean, color?: string}>} rows
 *   적을 건수. `excluded`는 **분모·중앙값에서 빠진 건수**라 눈에 띄게 표시하고,
 *   `color`가 있으면 위 차트의 어느 조각인지 색으로 잇는다.
 * @returns {void}
 */
function renderCounts(card, rows) {
  const list = slot(card, "counts");
  clear(list);

  for (const row of rows) {
    const kind = row.excluded ? "excluded" : "included";
    const term = el("dt", { "data-kind": kind });
    if (row.color) {
      term.append(el("span", { class: "legend__swatch", style: `background:${row.color}` }));
    }
    term.append(document.createTextNode(row.label));

    list.append(
      term,
      el("dd", { "data-kind": kind, "data-zero": String(row.value === 0) }, formatCount(row.value)),
    );
  }
}

/**
 * 큰 숫자 한 줄을 만든다.
 *
 * @param {string} value 이미 서식이 끝난 값.
 * @param {string} [unit] 값 뒤에 붙일 설명.
 * @returns {HTMLElement} 만들어진 요소.
 */
function headline(value, unit = null) {
  const box = el("div", { class: "headline" });
  box.append(el("span", { class: "headline__value", "data-empty": String(value === EMPTY) }, value));
  if (unit) box.append(el("span", { class: "headline__unit" }, unit));
  return box;
}

// ---------------------------------------------------------------------------
// 차트
// ---------------------------------------------------------------------------

/**
 * 도넛을 그린다.
 *
 * @param {Array<{key: string, value: number}>} entries 조각별 건수.
 * @param {(key: string) => string} colorOf 조각 색을 주는 함수.
 * @param {string} centerText 가운데에 쓸 글자.
 * @returns {SVGElement} 도넛.
 */
function donut(entries, colorOf, centerText) {
  const box = svg("svg", {
    class: "donut",
    viewBox: `0 0 ${DONUT_CENTER * 2} ${DONUT_CENTER * 2}`,
    role: "img",
  });

  const common = {
    cx: DONUT_CENTER,
    cy: DONUT_CENTER,
    r: DONUT_RADIUS.toFixed(4),
    "stroke-width": DONUT_STROKE,
  };

  // 바탕 원. 조각이 하나도 없어도 도넛의 자리는 남는다 -- 자리까지 사라지면
  // 카드 높이가 흔들려 옆 카드와 어긋난다.
  box.append(svg("circle", { ...common, class: "donut__track" }));

  // 대시는 3시 방향에서 시작한다. 12시부터 시계 방향으로 읽히도록 돌린다.
  const rotation = `rotate(-90 ${DONUT_CENTER} ${DONUT_CENTER})`;
  for (const segment of donutSegments(entries, DONUT_CIRCUMFERENCE)) {
    box.append(
      svg("circle", {
        ...common,
        class: "donut__segment",
        stroke: colorOf(segment.key),
        "stroke-dasharray": `${segment.length} ${DONUT_CIRCUMFERENCE - segment.length}`,
        "stroke-dashoffset": segment.dashOffset,
        transform: rotation,
      }),
    );
  }

  box.append(
    svg(
      "text",
      {
        class: "donut__center",
        x: DONUT_CENTER,
        y: DONUT_CENTER,
        "dominant-baseline": "central",
        "data-empty": String(centerText === EMPTY),
      },
      centerText,
    ),
  );
  return box;
}

/**
 * 도넛 옆에 붙는 범례를 만든다.
 *
 * @param {Array<{key: string, value: number, ratio: number|null}>} entries 항목.
 * @param {(key: string) => string} colorOf 색을 주는 함수.
 * @returns {HTMLElement} 범례.
 */
function legend(entries, colorOf) {
  const list = el("div", { class: "legend" });
  for (const entry of entries) {
    const row = el("div", { class: "legend__row" });
    row.append(
      el("span", { class: "legend__swatch", style: `background:${colorOf(entry.key)}` }),
      el("span", { class: "legend__name" }, LABELS[entry.key] ?? entry.key),
      el(
        "span",
        { class: "legend__value" },
        // 비율을 낼 수 없어도 건수는 쓴다. 건수까지 지우면 "분석 완료 0건"이라는
        // 사실 자체가 화면에서 사라진다.
        `${formatCount(entry.value)}건 · ${formatPercent(entry.ratio)}`,
      ),
    );
    list.append(row);
  }
  return list;
}

/**
 * 가로 막대 묶음을 그린다.
 *
 * @param {Array<{key: string, value: number, ratio: number|null}>} entries 항목.
 * @param {(key: string) => string} colorOf 색을 주는 함수.
 * @returns {HTMLElement} 막대 묶음.
 */
function bars(entries, colorOf) {
  const box = el("div", { class: "bars" });
  for (const entry of barWidths(entries)) {
    const row = el("div", { class: "bar-row" });
    const track = el("div", { class: "bar-row__track" });
    track.append(
      el("div", {
        class: "bar-row__fill",
        style: `width:${entry.widthPercent}%;background:${colorOf(entry.key)}`,
      }),
    );
    row.append(
      el("span", { class: "bar-row__name" }, LABELS[entry.key] ?? entry.key),
      track,
      el("span", { class: "bar-row__value" }, `${formatCount(entry.value)}건 · ${formatPercent(entry.ratio)}`),
    );
    box.append(row);
  }
  return box;
}

/**
 * 여러 건수를 한 줄에 쌓아 비중을 보여준다.
 *
 * @param {Array<{value: number, color: string, label: string}>} parts 쌓을 조각.
 * @returns {HTMLElement} 쌓은 막대.
 */
function stackedBar(parts) {
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  const box = el("div", { class: "stack", role: "img" });
  box.setAttribute(
    "aria-label",
    parts.map((part) => `${part.label} ${part.value}건`).join(", "),
  );

  if (total <= 0) return box;

  for (const part of parts) {
    box.append(
      el("div", {
        class: "stack__part",
        "data-zero": String(part.value === 0),
        style: `width:${(part.value / total) * 100}%;background:${part.color}`,
        title: `${part.label} ${part.value}건`,
      }),
    );
  }
  return box;
}

/** CSS 변수에서 색을 읽는다. 색을 CSS와 JS 두 곳에 적으면 한쪽만 바뀐다. */
function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

const categoryColor = (key) => cssColor(`--cat-${key}`);
const sentimentColor = (key) => cssColor(`--sent-${key}`);

// ---------------------------------------------------------------------------
// 카드 4개
// ---------------------------------------------------------------------------

/**
 * 방치된 이슈 비율 카드를 그린다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {Object} data 지표 응답.
 * @returns {void}
 */
function renderStale(card, data) {
  const metric = data.stale_issues;
  const state = metricState(data.status, metric?.ratio);
  const figure = slot(card, "figure");
  clear(figure);

  if (state === MetricState.PENDING) {
    setCardState(card, state);
    renderCounts(card, []);
    figure.append(donut([], () => "", EMPTY));
    return;
  }

  setCardState(
    card,
    state,
    state === MetricState.UNAVAILABLE
      ? `기간 안에 생성된 open 이슈가 ${formatCount(metric.open_total)}건이라 비율을 낼 수 없습니다.`
      : null,
  );

  const remaining = Math.max(metric.open_total - metric.stale_count, 0);
  const layout = el("div", { class: "donut-layout" });
  layout.append(
    donut(
      [
        { key: "stale", value: metric.stale_count },
        { key: "rest", value: remaining },
      ],
      (key) => cssColor(key === "stale" ? "--stale-yes" : "--stale-no"),
      // 가운데에는 분자/분모를 적는다. 옆의 큰 숫자와 같은 비율을 자릿수만 줄여
      // 적으면(25.6% 옆에 26%) 두 값이 어긋난 것처럼 읽힌다.
      `${formatCount(metric.stale_count)} / ${formatCount(metric.open_total)}`,
    ),
  );

  const side = el("div", { class: "legend" });
  side.append(headline(formatPercent(metric.ratio), `${metric.stale_days}일 이상 방치 기준`));
  layout.append(side);
  figure.append(layout);

  renderCounts(card, [
    { label: "방치됨 (분자)", value: metric.stale_count },
    { label: "기간 안 open 이슈 (분모)", value: metric.open_total },
    { label: "첫 응답 미조사 — 판정 보류", value: metric.unchecked_count, excluded: true },
  ]);
}

/**
 * 분포 카드(분류·감정)를 그린다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {Object} data 지표 응답.
 * @param {Object} options 그리는 방법.
 * @param {string} options.key 응답에서 읽을 필드 이름.
 * @param {string[]} options.order 그릴 순서.
 * @param {(key: string) => string} options.colorOf 색을 주는 함수.
 * @param {"bars"|"donut"} options.shape 그림 종류.
 * @returns {void}
 */
function renderDistribution(card, data, { key, order, colorOf, shape }) {
  const metric = data[key];
  const state = metricState(data.status, metric?.ratios);
  const figure = slot(card, "figure");
  clear(figure);

  if (state === MetricState.PENDING) {
    setCardState(card, state);
    renderCounts(card, []);
    figure.append(shape === "donut" ? donut([], colorOf, EMPTY) : el("div", { class: "bars" }));
    return;
  }

  setCardState(
    card,
    state,
    state === MetricState.UNAVAILABLE
      ? "분석에 성공한 이슈가 0건이라 비율을 낼 수 없습니다. 아래 건수는 그대로입니다."
      : null,
  );

  const entries = distributionEntries(metric, order);

  if (shape === "donut") {
    const layout = el("div", { class: "donut-layout" });
    layout.append(
      donut(entries, colorOf, formatCount(metric.analyzed_count)),
      legend(entries, colorOf),
    );
    figure.append(layout);
  } else {
    figure.append(bars(entries, colorOf));
  }

  renderCounts(card, [
    { label: "분석 완료 (분모)", value: metric.analyzed_count },
    { label: "미분석 — 분모에서 빠짐", value: metric.unanalyzed_count, excluded: true },
    { label: "기간 안 이슈 전체", value: metric.total },
  ]);
}

/**
 * 메인테이너 응답 속도 카드를 그린다.
 *
 * @param {HTMLElement} card 카드 요소.
 * @param {Object} data 지표 응답.
 * @returns {void}
 */
function renderResponseTime(card, data) {
  const metric = data.response_time;
  const state = metricState(data.status, metric?.median_seconds);
  const figure = slot(card, "figure");
  clear(figure);

  if (state === MetricState.PENDING) {
    setCardState(card, state);
    renderCounts(card, []);
    figure.append(headline(EMPTY));
    return;
  }

  setCardState(
    card,
    state,
    state === MetricState.UNAVAILABLE
      ? "중앙값에 넣을 응답이 0건이라 값을 낼 수 없습니다. 응답 없음 · 미조사 건수는 아래에 있습니다."
      : null,
  );

  // 누적 막대의 조각과 아래 건수 목록이 같은 색·같은 순서를 쓴다. 중앙값에
  // 들어간 몫과 빠진 몫의 비중이 보여야 "median만 보면 안 된다"가 전달된다.
  const parts = [
    { value: metric.responded_count, color: cssColor("--resp-answered"), label: "중앙값에 포함" },
    {
      value: metric.no_response_count,
      color: cssColor("--resp-none"),
      label: "메인테이너 응답 없음 — 0으로 채우지 않음",
      excluded: true,
    },
    {
      value: metric.unchecked_count,
      color: cssColor("--resp-unchecked"),
      label: "첫 응답 미조사",
      excluded: true,
    },
  ];

  const box = el("div", { class: "legend" });
  box.append(headline(formatDuration(metric.median_seconds), "첫 응답까지 (중앙값)"));
  box.append(stackedBar(parts));
  figure.append(box);

  renderCounts(card, parts);
}

// ---------------------------------------------------------------------------
// 화면 전체
// ---------------------------------------------------------------------------

const root = {
  repo: document.querySelector('[data-field="repo"]'),
  banner: document.querySelector('[data-role="banner"]'),
  grid: document.querySelector('[data-role="grid"]'),
  footer: document.querySelector('[data-role="footer"]'),
  form: document.querySelector('[data-role="repo-form"]'),
  input: document.querySelector("#repo-input"),
};

/**
 * 배너를 띄운다.
 *
 * @param {string|null} message 띄울 글. `null`이면 숨긴다.
 * @param {"info"|"warn"|"error"} [tone="info"] 배너 색.
 * @returns {void}
 */
function showBanner(message, tone = "info") {
  root.banner.hidden = message === null;
  root.banner.textContent = message ?? "";
  root.banner.dataset.tone = tone;
}

/**
 * 지표 응답 하나를 화면 전체에 반영한다.
 *
 * @param {Object} data 지표 응답.
 * @returns {void}
 */
function render(data) {
  root.repo.textContent = data.repo;
  root.grid.hidden = false;
  root.footer.hidden = false;

  if (data.status === "pending") {
    showBanner(
      `${data.repo}는 이 대시보드가 아는 저장소지만 아직 수집하지 않았습니다. ` +
        "수집이 끝나면 지표 4개가 채워집니다.",
      "warn",
    );
  } else {
    showBanner(null);
  }

  renderStale(document.querySelector('[data-metric="stale"]'), data);
  renderDistribution(document.querySelector('[data-metric="categories"]'), data, {
    key: "categories",
    order: CATEGORY_ORDER,
    colorOf: categoryColor,
    shape: "bars",
  });
  renderResponseTime(document.querySelector('[data-metric="response-time"]'), data);
  renderDistribution(document.querySelector('[data-metric="sentiments"]'), data, {
    key: "sentiments",
    order: SENTIMENT_ORDER,
    colorOf: sentimentColor,
    shape: "donut",
  });

  const footerText = {
    "generated-at": formatTimestamp(data.generated_at),
    "window-days": `${data.window_days}일`,
    "stale-days": data.stale_issues ? `${data.stale_issues.stale_days}일` : EMPTY,
    "issue-count": data.issue_count === null ? EMPTY : `${formatCount(data.issue_count)}건`,
  };
  for (const [field, text] of Object.entries(footerText)) {
    root.footer.querySelector(`[data-field="${field}"]`).textContent = text;
  }
}

// ---------------------------------------------------------------------------
// 데이터 가져오기
// ---------------------------------------------------------------------------

/**
 * 고정된 응답 스냅샷을 읽는다.
 *
 * 이름을 허용 목록으로 거른다. 쿼리 값을 경로에 그대로 이어 붙이면 `?fixture=`
 * 하나로 이 서버의 아무 파일이나 읽게 된다.
 *
 * @param {string} name 스냅샷 이름.
 * @returns {Promise<Object>} 지표 응답.
 */
async function loadFixture(name) {
  const response = await fetch(`./fixtures/${name}.json`);
  if (!response.ok) throw new Error(`스냅샷을 읽지 못했습니다: ${name} (HTTP ${response.status})`);
  return response.json();
}

/**
 * API에서 지표를 받아온다.
 *
 * @param {string} repoFullName `"owner/name"`.
 * @returns {Promise<Object>} 지표 응답.
 * @throws {Error} 404를 포함해 응답이 실패한 경우. 메시지는 API가 준 `detail`이다.
 */
async function loadMetrics(repoFullName) {
  const [owner, repo] = repoFullName.split("/");
  const path = `/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/metrics`;
  const response = await fetch(path);

  if (!response.ok) {
    // API가 왜 거절했는지를 그대로 보여준다. 404는 "GitHub에 없다"가 아니라
    // "이 대시보드가 다루는 저장소가 아니다"이고, 그 구분이 detail에 적혀 있다.
    const detail = await response
      .json()
      .then((body) => body.detail)
      .catch(() => null);
    throw new Error(detail ?? `지표를 받지 못했습니다 (HTTP ${response.status}).`);
  }
  return response.json();
}

/** 저장소 이름이 `owner/name` 꼴인지 본다. */
function isRepoFullName(value) {
  return /^[\w.-]+\/[\w.-]+$/.test(value);
}

/** localStorage는 시크릿 창·차단 설정에서 접근 자체가 예외를 던진다. */
function readStoredRepo() {
  try {
    return localStorage.getItem(REPO_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeRepo(value) {
  try {
    localStorage.setItem(REPO_STORAGE_KEY, value);
  } catch {
    // 기억하지 못해도 화면은 그대로 돈다. 조용히 넘어가도 되는 유일한 자리다.
  }
}

// ---------------------------------------------------------------------------
// 시작
// ---------------------------------------------------------------------------

/** 상태 전환기에서 지금 보고 있는 항목을 표시한다. */
function markActiveStateLink(params) {
  const active = params.get("fixture") ?? (params.get("repo") === "nobody/nothing" ? "not-found" : "live");
  for (const link of document.querySelectorAll("[data-state-link]")) {
    if (link.dataset.stateLink === active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

async function main() {
  const params = new URLSearchParams(location.search);
  markActiveStateLink(params);

  const fixture = params.get("fixture");
  if (fixture !== null) {
    if (!FIXTURES.has(fixture)) {
      showBanner(`모르는 스냅샷 이름입니다: ${fixture}`, "error");
      return;
    }
    showBanner(
      "고정된 응답 스냅샷을 보고 있습니다. API를 호출하지 않았습니다.",
      "info",
    );
    try {
      const data = await loadFixture(fixture);
      render(data);
      // render()가 pending 배너로 덮어쓰므로, 스냅샷임을 다시 알린다.
      if (data.status !== "pending") {
        showBanner("고정된 응답 스냅샷을 보고 있습니다. API를 호출하지 않았습니다.", "info");
      }
    } catch (error) {
      showBanner(error.message, "error");
    }
    return;
  }

  const repoFullName = params.get("repo") ?? readStoredRepo();
  root.input.value = repoFullName ?? SUGGESTED_REPO;

  if (repoFullName === null) {
    showBanner(
      "볼 저장소를 입력하세요. 예: PrefectHQ/fastmcp — 주소에 ?repo=owner/name 으로 붙여도 됩니다.",
      "info",
    );
    return;
  }

  if (!isRepoFullName(repoFullName)) {
    showBanner(`저장소 이름은 owner/name 꼴이어야 합니다: ${repoFullName}`, "error");
    return;
  }

  root.repo.textContent = repoFullName;
  storeRepo(repoFullName);

  try {
    render(await loadMetrics(repoFullName));
  } catch (error) {
    root.grid.hidden = true;
    root.footer.hidden = true;
    showBanner(error.message, "error");
  }
}

root.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = root.input.value.trim();
  if (value) location.search = `?repo=${encodeURIComponent(value)}`;
});

main();
