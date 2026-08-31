/**
 * `format.js` 테스트. Node 내장 러너로 돈다 -- `node --test frontend/`.
 *
 * ## 왜 npm 의존성이 없나
 *
 * 차트 라이브러리를 안 쓰기로 한 이유(이슈 #12)가 테스트 러너에도 그대로
 * 적용된다. 지표가 4개뿐인 화면에 `package.json`과 `node_modules`를 들이면
 * 재현성과 CI 시간을 잃는 쪽이 크다. Node 18+에 내장된 `node:test`로 충분하다.
 *
 * ## 무엇을 고정하려는 테스트인가
 *
 * 대부분은 **`null`을 0으로 바꾸지 않는가**이다. 이 프로젝트가 반복해서 피해 온
 * 실패 방식이고(`app/analysis/metrics.py`), 화면은 그 실패가 마지막으로
 * 되살아나기 쉬운 자리다 -- `value || 0` 한 번이면 빈 저장소가 건강해 보인다.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

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

describe("metricState", () => {
  it("status가 pending이면 값이 있든 없든 PENDING이다", () => {
    assert.equal(metricState("pending", null), MetricState.PENDING);
    assert.equal(metricState("pending", 0.5), MetricState.PENDING);
  });

  it("ready인데 값이 null이면 UNAVAILABLE이다 -- PENDING과 다른 상태다", () => {
    assert.equal(metricState("ready", null), MetricState.UNAVAILABLE);
    assert.equal(metricState("ready", undefined), MetricState.UNAVAILABLE);
    assert.notEqual(MetricState.UNAVAILABLE, MetricState.PENDING);
  });

  it("ready이고 값이 0이면 READY다 -- 0은 '값이 없음'이 아니다", () => {
    assert.equal(metricState("ready", 0), MetricState.READY);
    assert.equal(metricState("ready", 0.0), MetricState.READY);
  });
});

describe("formatPercent", () => {
  it("비율을 백분율로 쓴다", () => {
    assert.equal(formatPercent(0.1234), "12.3%");
    assert.equal(formatPercent(1), "100.0%");
  });

  it("0%와 '낼 수 없음'을 다르게 쓴다", () => {
    assert.equal(formatPercent(0), "0.0%");
    assert.equal(formatPercent(null), EMPTY);
    assert.equal(formatPercent(undefined), EMPTY);
  });
});

describe("formatCount", () => {
  it("자릿수를 구분한다", () => {
    assert.equal(formatCount(1234), "1,234");
    assert.equal(formatCount(0), "0");
  });

  it("건수가 없으면 EMPTY다", () => {
    assert.equal(formatCount(null), EMPTY);
  });
});

describe("formatDuration", () => {
  it("크기에 맞는 단위를 고른다", () => {
    assert.equal(formatDuration(42), "42초");
    assert.equal(formatDuration(300), "5분");
    assert.equal(formatDuration(3600 * 3.4), "3.4시간");
    assert.equal(formatDuration(86400 * 2.25), "2.3일");
  });

  it("단위 경계에서 한 칸 위로 넘어간다", () => {
    assert.equal(formatDuration(59), "59초");
    assert.equal(formatDuration(60), "1분");
    assert.equal(formatDuration(3599), "60분");
    assert.equal(formatDuration(3600), "1.0시간");
    assert.equal(formatDuration(86400), "1.0일");
  });

  it("응답이 없으면 0초가 아니라 EMPTY다", () => {
    assert.equal(formatDuration(null), EMPTY);
    assert.notEqual(formatDuration(null), formatDuration(0));
  });

  it("음수를 감추지 않는다 -- 수집 버그가 EMPTY로 묻히면 안 된다", () => {
    assert.equal(formatDuration(-300), "-5분");
  });
});

describe("formatTimestamp", () => {
  it("파싱할 수 없으면 EMPTY다", () => {
    assert.equal(formatTimestamp(null), EMPTY);
    assert.equal(formatTimestamp(""), EMPTY);
    assert.equal(formatTimestamp("어제"), EMPTY);
  });

  it("ISO 8601을 읽는다", () => {
    assert.notEqual(formatTimestamp("2026-08-24T12:00:00Z"), EMPTY);
  });
});

describe("donutSegments", () => {
  const CIRCUMFERENCE = 100;

  it("건수에 비례해 조각을 나누고 이어 붙인다", () => {
    const segments = donutSegments(
      [
        { key: "a", value: 3 },
        { key: "b", value: 1 },
      ],
      CIRCUMFERENCE,
    );

    assert.deepEqual(
      segments.map((segment) => segment.length),
      [75, 25],
    );
    // 두 번째 조각은 첫 조각이 쓴 75만큼 당겨져 이어진다.
    assert.deepEqual(
      segments.map((segment) => segment.dashOffset),
      [-0, -75],
    );
  });

  it("길이의 합이 둘레와 같다", () => {
    const segments = donutSegments(
      [
        { key: "a", value: 7 },
        { key: "b", value: 11 },
        { key: "c", value: 2 },
      ],
      CIRCUMFERENCE,
    );
    const total = segments.reduce((sum, segment) => sum + segment.length, 0);
    assert.ok(Math.abs(total - CIRCUMFERENCE) < 1e-9);
  });

  it("0건인 조각은 버린다 -- 둥근 끝이 빈 자리에 점을 찍는다", () => {
    const segments = donutSegments(
      [
        { key: "a", value: 0 },
        { key: "b", value: 5 },
        { key: "c", value: 0 },
      ],
      CIRCUMFERENCE,
    );

    assert.deepEqual(
      segments.map((segment) => segment.key),
      ["b"],
    );
  });

  it("전부 0이면 빈 배열이다 -- 꽉 찬 도넛을 그리지 않는다", () => {
    assert.deepEqual(
      donutSegments(
        [
          { key: "a", value: 0 },
          { key: "b", value: 0 },
        ],
        CIRCUMFERENCE,
      ),
      [],
    );
    assert.deepEqual(donutSegments([], CIRCUMFERENCE), []);
  });
});

describe("barWidths", () => {
  it("가장 큰 값이 100%가 된다", () => {
    const bars = barWidths([
      { key: "a", value: 5 },
      { key: "b", value: 10 },
    ]);

    assert.deepEqual(
      bars.map((bar) => bar.widthPercent),
      [50, 100],
    );
  });

  it("전부 0이면 나눗셈을 하지 않고 0을 준다", () => {
    const bars = barWidths([
      { key: "a", value: 0 },
      { key: "b", value: 0 },
    ]);

    assert.deepEqual(
      bars.map((bar) => bar.widthPercent),
      [0, 0],
    );
  });
});

describe("distributionEntries", () => {
  const ORDER = ["bug", "feature_request", "question", "other"];

  it("JSON 키 순서가 아니라 지정한 순서를 따른다", () => {
    const entries = distributionEntries(
      { counts: { other: 1, bug: 2, question: 0, feature_request: 3 }, ratios: null },
      ORDER,
    );

    assert.deepEqual(
      entries.map((entry) => entry.key),
      ORDER,
    );
    assert.deepEqual(
      entries.map((entry) => entry.value),
      [2, 3, 0, 1],
    );
  });

  it("ratios가 null이면 항목마다 ratio도 null이다 -- 0으로 채우지 않는다", () => {
    const entries = distributionEntries({ counts: { bug: 0 }, ratios: null }, ORDER);

    assert.ok(entries.every((entry) => entry.ratio === null));
  });

  it("분포 자체가 null이면(pending) 빈 배열이다", () => {
    assert.deepEqual(distributionEntries(null, ORDER), []);
    assert.deepEqual(distributionEntries(undefined, ORDER), []);
  });
});
