/**
 * 값 하나를 화면에 쓸 문자열·치수로 바꾸는 순수 함수들. DOM도 fetch도 모른다.
 *
 * ## 왜 app.js에서 떼어냈나
 *
 * 이 프로젝트에는 JS 테스트 러너가 없고, 넣을 생각도 없다(차트 라이브러리를 안
 * 쓰는 이유와 같다). 대신 DOM을 만지지 않는 함수만 여기 모아 두면 Node에 내장된
 * `node --test`로 의존성 없이 검증할 수 있다. 화면을 그리는 코드는 `app.js`에
 * 남는다.
 *
 * ## `null`은 두 가지 뜻이고, 화면에서도 달라야 한다
 *
 * API 응답의 `null`은 자리에 따라 뜻이 다르다(`app/api/schemas.py`).
 *
 * | 자리 | 뜻 | 이 모듈의 상태 |
 * |---|---|---|
 * | `status="pending"`일 때 지표 4개 | 아직 계산할 수 없다(수집 전) | `PENDING` |
 * | `ratio` · `median_seconds` | 비율을 낼 수 없다(분모 0) | `UNAVAILABLE` |
 *
 * 앞은 **기다리면 값이 생긴다**이고 뒤는 **지금 데이터로는 낼 수 없다**이다.
 * 둘을 같은 회색 대시로 그리면 사용자는 기다려야 할지 말지 알 수 없고, 0으로
 * 채우면 **빈 저장소가 가장 건강해 보인다.** 그래서 상태를 값에서 분리해
 * `metricState()` 하나로 판정하고, 화면은 그 상태에 따라 다른 것을 그린다.
 */

/** 값을 낼 수 없을 때 숫자 자리에 넣는 글자. 0을 넣지 않는다. */
export const EMPTY = "—";

/**
 * 지표 한 칸이 놓인 상태.
 *
 * `READY`가 아닌 두 상태는 **서로 다른 이유로** 숫자가 없다. 화면에서 합치지
 * 않는다.
 */
export const MetricState = Object.freeze({
  /** 수집 전이라 아직 계산할 수 없다. 다시 물어보면 값이 생긴다. */
  PENDING: "pending",
  /** 분모가 0이라 비율을 낼 수 없다. 기다려도 값이 생기지 않는다. */
  UNAVAILABLE: "unavailable",
  /** 값이 있다. */
  READY: "ready",
});

/**
 * 지표 한 칸의 상태를 판정한다.
 *
 * @param {string} status 응답의 `status` 필드(`"ready"` | `"pending"`).
 * @param {*} value 그 칸이 그릴 값. 없으면 `null`/`undefined`.
 * @returns {string} `MetricState` 중 하나.
 */
export function metricState(status, value) {
  if (status !== "ready") return MetricState.PENDING;
  if (value === null || value === undefined) return MetricState.UNAVAILABLE;
  return MetricState.READY;
}

/**
 * 0~1 비율을 백분율 문자열로 만든다.
 *
 * @param {number|null|undefined} ratio 비율. 낼 수 없으면 `null`.
 * @param {number} [digits=1] 소수점 자릿수.
 * @returns {string} `"12.3%"`. 값이 없으면 `EMPTY`.
 */
export function formatPercent(ratio, digits = 1) {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return EMPTY;
  return `${(ratio * 100).toFixed(digits)}%`;
}

/**
 * 정수 건수를 자릿수 구분 기호와 함께 쓴다.
 *
 * @param {number|null|undefined} count 건수.
 * @returns {string} `"1,234"`. 값이 없으면 `EMPTY`.
 */
export function formatCount(count) {
  if (count === null || count === undefined || !Number.isFinite(count)) return EMPTY;
  return count.toLocaleString("ko-KR");
}

/**
 * 초를 사람이 읽는 기간으로 바꾼다.
 *
 * API는 **초 단위로** 보낸다. 서버에서 시간으로 반올림해 보내면 "3시간"과
 * "3.4시간"이 같은 값이 되어 되돌릴 수 없다(`app/api/schemas.py`). 반올림은
 * 화면에서만 한다.
 *
 * 음수는 감추지 않는다. 응답 시각이 생성 시각보다 앞서는 데이터는 수집 버그인데,
 * 그걸 `EMPTY`로 바꾸면 "응답이 없는 저장소"처럼 보여 조용히 묻힌다.
 *
 * @param {number|null|undefined} seconds 기간(초). 낼 수 없으면 `null`.
 * @returns {string} `"42분"` · `"3.4시간"` · `"2.1일"`. 값이 없으면 `EMPTY`.
 */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EMPTY;

  const sign = seconds < 0 ? "-" : "";
  const abs = Math.abs(seconds);

  if (abs < 60) return `${sign}${Math.round(abs)}초`;
  if (abs < 3600) return `${sign}${Math.round(abs / 60)}분`;
  if (abs < 86400) return `${sign}${(abs / 3600).toFixed(1)}시간`;
  return `${sign}${(abs / 86400).toFixed(1)}일`;
}

/**
 * ISO 8601 시각을 화면용 문자열로 바꾼다.
 *
 * @param {string|null|undefined} isoText 응답의 `generated_at`.
 * @returns {string} 현지 시간 표기. 파싱할 수 없으면 `EMPTY`.
 */
export function formatTimestamp(isoText) {
  if (!isoText) return EMPTY;
  const parsed = new Date(isoText);
  if (Number.isNaN(parsed.getTime())) return EMPTY;
  return parsed.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}

/**
 * 도넛 차트의 조각을 `stroke-dasharray` 값으로 계산한다.
 *
 * `<path>`의 호(arc) 명령 대신 원 하나에 점선 패턴을 얹는 방식이다. 삼각함수도
 * 큰 호 플래그(`large-arc-flag`)도 필요 없어 손으로 쓴 SVG가 몇 줄로 끝난다.
 *
 * **0건인 조각은 버린다.** 길이 0짜리 대시를 남기면 `stroke-linecap`이 둥근
 * 경우 아무것도 없는 자리에 점이 찍힌다.
 *
 * @param {Array<{key: string, value: number}>} entries 조각별 건수.
 * @param {number} circumference 원의 둘레(`2 * PI * r`).
 * @returns {Array<{key: string, value: number, length: number, dashOffset: number}>}
 *   그릴 조각. 합이 0이면 빈 배열 -- 그릴 것이 없다는 뜻이고, 호출한 쪽이
 *   "값 없음"을 그리게 된다.
 */
export function donutSegments(entries, circumference) {
  const total = entries.reduce((sum, entry) => sum + entry.value, 0);
  if (total <= 0) return [];

  let consumed = 0;
  const segments = [];
  for (const entry of entries) {
    if (entry.value <= 0) continue;
    const length = (entry.value / total) * circumference;
    segments.push({
      key: entry.key,
      value: entry.value,
      length,
      // 대시는 12시 방향에서 시계 방향으로 나아간다. 앞 조각이 쓴 만큼 음수로
      // 당겨야 이어 붙는다.
      dashOffset: -consumed,
    });
    consumed += length;
  }
  return segments;
}

/**
 * 가로 막대의 너비를 백분율로 계산한다.
 *
 * 가장 큰 값이 100%가 되도록 맞춘다. 전체 합을 기준으로 하면 값이 고르게 퍼진
 * 분포에서 막대가 전부 짧아져 서로 비교가 안 된다.
 *
 * @param {Array<{key: string, value: number}>} entries 막대별 건수.
 * @returns {Array<{key: string, value: number, widthPercent: number}>}
 *   너비가 채워진 막대. 최댓값이 0이면 전부 `0`이다.
 */
export function barWidths(entries) {
  const largest = entries.reduce((max, entry) => Math.max(max, entry.value), 0);
  return entries.map((entry) => ({
    ...entry,
    widthPercent: largest <= 0 ? 0 : (entry.value / largest) * 100,
  }));
}

/**
 * 응답의 분포 객체를 화면이 쓸 배열로 편다.
 *
 * `counts`는 **0건인 값도 키로 갖는다**(`app/analysis/metrics.py`). 순서는
 * 객체의 키 순서가 아니라 `order`가 정한다 -- JSON 키 순서에 화면 순서를 맡기면
 * 서버가 필드를 재정렬하는 순간 범례 색과 막대가 어긋난다.
 *
 * @param {{counts: Object<string, number>, ratios: Object<string, number>|null}|null|undefined}
 *   distribution 응답의 분포 객체.
 * @param {string[]} order 그릴 순서대로 나열한 enum 값.
 * @returns {Array<{key: string, value: number, ratio: number|null}>} 분포 항목.
 */
export function distributionEntries(distribution, order) {
  if (!distribution) return [];
  const counts = distribution.counts ?? {};
  const ratios = distribution.ratios ?? null;
  return order.map((key) => ({
    key,
    value: counts[key] ?? 0,
    ratio: ratios === null ? null : (ratios[key] ?? null),
  }));
}
