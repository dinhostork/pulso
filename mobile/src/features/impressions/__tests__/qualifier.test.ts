import { visibleShare, type CardVisibility } from "@/features/feed/visibility";

import { EXPOSURE_POLICY } from "../policy";
import { ExposureQualifier } from "../qualifier";

const card = (storyId: string, share: number, position = 0): CardVisibility => ({
  storyId,
  position,
  share,
});

function openQualifier(now = 0) {
  const qualifier = new ExposureQualifier(EXPOSURE_POLICY);
  qualifier.setGate(true, now);
  return qualifier;
}

describe("ExposureQualifier v1 thresholds", () => {
  it("uses 50% for 1000 continuous ms", () => {
    expect(EXPOSURE_POLICY).toEqual({ visibleShare: 0.5, dwellMs: 1000 });
  });

  it("never qualifies just below the visibility threshold", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 0.49)], 0);
    expect(qualifier.nextDeadline()).toBeNull();
    expect(qualifier.tick(60_000)).toEqual([]);
  });

  it("qualifies at exactly the visibility threshold", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 0.5, 3)], 0);
    expect(qualifier.nextDeadline()).toBe(1000);
    expect(qualifier.tick(1000)).toEqual([{ storyId: "7", position: 3, occurredAt: 1000 }]);
  });

  it("does not qualify at 999 ms and qualifies at exactly 1000 ms", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1)], 5000);
    expect(qualifier.tick(5999)).toEqual([]);
    expect(qualifier.tick(6000)).toHaveLength(1);
  });

  it("qualifies a card that was continuously visible even if the timer runs late", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1)], 0);
    // The next report (scrolled away) arrives after the dwell elapsed.
    expect(qualifier.observe([], 1300)).toEqual([{ storyId: "7", position: 0, occurredAt: 1000 }]);
  });
});

describe("ExposureQualifier resets", () => {
  it("resets the dwell when visibility drops below the threshold", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 0.8)], 0);
    qualifier.observe([card("7", 0.4)], 900);
    qualifier.observe([card("7", 0.8)], 950);
    expect(qualifier.tick(1500)).toEqual([]);
    expect(qualifier.tick(1950)).toHaveLength(1);
  });

  it("resets when the Feed loses focus or the app backgrounds", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1)], 0);
    expect(qualifier.setGate(false, 999)).toEqual([]);
    expect(qualifier.tick(5000)).toEqual([]);
    // Refocusing restarts the dwell from the refocus time with the same geometry.
    qualifier.setGate(true, 5000);
    expect(qualifier.tick(5999)).toEqual([]);
    expect(qualifier.tick(6000)).toHaveLength(1);
  });

  it("never qualifies while the gate is closed, whatever the geometry", () => {
    const qualifier = new ExposureQualifier(EXPOSURE_POLICY);
    qualifier.observe([card("7", 1)], 0);
    expect(qualifier.nextDeadline()).toBeNull();
    expect(qualifier.tick(10_000)).toEqual([]);
  });

  it("treats an unmounted or recycled card as no longer visible", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1, 0)], 0);
    // The cell now shows another Story: the old one left the report.
    qualifier.observe([card("8", 1, 0)], 500);
    expect(qualifier.tick(1000)).toEqual([]);
    expect(qualifier.tick(1500)).toEqual([{ storyId: "8", position: 0, occurredAt: 1500 }]);
  });

  it("produces nothing from rapid scrolling past cards", () => {
    const qualifier = openQualifier();
    // A fling shows each card fully for 200 ms before the next replaces it.
    for (let index = 0; index < 20; index += 1) {
      qualifier.observe([card(String(index + 1), 1, index)], index * 200);
    }
    qualifier.observe([], 4000);
    expect(qualifier.tick(60_000)).toEqual([]);
  });

  it("produces nothing from mount, prefetch or a fast tap", () => {
    const qualifier = openQualifier();
    // Mounted/prefetched offscreen cards have no visible share.
    qualifier.observe([card("7", 0, 0), card("8", 0, 1)], 0);
    expect(qualifier.tick(10_000)).toEqual([]);
    // A card tapped 300 ms after appearing closes the gate before qualifying.
    qualifier.observe([card("9", 1, 2)], 10_000);
    expect(qualifier.setGate(false, 10_300)).toEqual([]);
    expect(qualifier.tick(20_000)).toEqual([]);
  });
});

describe("ExposureQualifier sessions", () => {
  it("emits a Story once per session however often it returns", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1)], 0);
    expect(qualifier.tick(1000)).toHaveLength(1);
    qualifier.observe([], 2000);
    qualifier.observe([card("7", 1)], 3000);
    qualifier.setGate(false, 3500);
    qualifier.setGate(true, 4000);
    expect(qualifier.nextDeadline()).toBeNull();
    expect(qualifier.tick(60_000)).toEqual([]);
  });

  it("allows the Story again in a new session, restarting visible cards' dwell", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1)], 0);
    expect(qualifier.tick(1000)).toHaveLength(1);
    qualifier.reset(2000);
    expect(qualifier.tick(2999)).toEqual([]);
    expect(qualifier.tick(3000)).toEqual([{ storyId: "7", position: 0, occurredAt: 3000 }]);
  });

  it("reports the absolute rendered position at qualification", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", 1, 41), card("8", 1, 42)], 0);
    qualifier.observe([card("7", 1, 43), card("8", 1, 44)], 500);
    expect(qualifier.tick(1000).map((exposure) => exposure.position)).toEqual([43, 44]);
  });
});

describe("tall cards and large text", () => {
  const viewport = { offset: 0, height: 800 };

  it("qualifies a card taller than the screen that fills the viewport", () => {
    // Large accessibility type makes this card 2400 points tall; 50% of it can never fit.
    const qualifier = openQualifier();
    qualifier.observe([card("7", visibleShare({ top: -600, height: 2400 }, viewport))], 0);
    expect(qualifier.tick(1000)).toHaveLength(1);
  });

  it("does not qualify a tall card that covers less than half the viewport", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", visibleShare({ top: 401, height: 2400 }, viewport))], 0);
    expect(qualifier.tick(10_000)).toEqual([]);
  });

  it("qualifies an ordinary card at half its own height", () => {
    const qualifier = openQualifier();
    qualifier.observe([card("7", visibleShare({ top: 600, height: 400 }, viewport))], 0);
    expect(qualifier.tick(1000)).toHaveLength(1);
  });
});
