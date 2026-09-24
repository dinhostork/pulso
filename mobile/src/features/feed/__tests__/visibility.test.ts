import { FeedVisibilityTracker, visibleShare, type CardVisibility } from "../visibility";

describe("visibleShare", () => {
  const viewport = { offset: 1000, height: 800 };

  it("is the visible fraction of a card shorter than the viewport", () => {
    expect(visibleShare({ top: 1000, height: 400 }, viewport)).toBe(1);
    expect(visibleShare({ top: 1600, height: 400 }, viewport)).toBe(0.5);
    expect(visibleShare({ top: 1601, height: 400 }, viewport)).toBeCloseTo(0.4975);
    expect(visibleShare({ top: 800, height: 400 }, viewport)).toBe(0.5);
  });

  it("is zero outside the viewport, including a card touching its edge", () => {
    expect(visibleShare({ top: 1800, height: 400 }, viewport)).toBe(0);
    expect(visibleShare({ top: 600, height: 400 }, viewport)).toBe(0);
  });

  it("measures a card taller than the screen against the viewport height", () => {
    // A 2400-point card (long text or large accessibility type) filling the screen.
    expect(visibleShare({ top: 400, height: 2400 }, viewport)).toBe(1);
    // Showing 400 of its points covers half of the viewport.
    expect(visibleShare({ top: 1400, height: 2400 }, viewport)).toBe(0.5);
  });

  it("is zero for unmeasured geometry", () => {
    expect(visibleShare({ top: 0, height: 0 }, viewport)).toBe(0);
    expect(visibleShare({ top: 0, height: 100 }, { offset: 0, height: 0 })).toBe(0);
  });
});

describe("FeedVisibilityTracker", () => {
  it("reports nothing until the viewport and card are both measured", () => {
    const reports: CardVisibility[][] = [];
    const tracker = new FeedVisibilityTracker((cards) => reports.push(cards));
    tracker.setCard("7", 0, { top: 0, height: 300 });
    expect(reports.at(-1)).toEqual([]);
    tracker.setViewport({ offset: 0, height: 800 });
    expect(reports.at(-1)).toEqual([{ storyId: "7", position: 0, share: 1 }]);
  });

  it("follows scrolling, absolute positions and card removal", () => {
    const reports: CardVisibility[][] = [];
    const tracker = new FeedVisibilityTracker((cards) => reports.push(cards));
    tracker.setViewport({ offset: 0, height: 800 });
    tracker.setCard("30", 0, { top: 0, height: 400 });
    tracker.setCard("20", 41, { top: 400, height: 400 });
    tracker.setCard("10", 42, { top: 800, height: 400 });
    expect(reports.at(-1)?.map((card) => card.storyId)).toEqual(["30", "20"]);

    tracker.setViewport({ offset: 600, height: 800 });
    expect(reports.at(-1)).toEqual([
      { storyId: "20", position: 41, share: 0.5 },
      { storyId: "10", position: 42, share: 1 },
    ]);

    tracker.setPosition("10", 43);
    expect(reports.at(-1)?.at(-1)).toEqual({ storyId: "10", position: 43, share: 1 });

    tracker.removeCard("10");
    expect(reports.at(-1)).toEqual([{ storyId: "20", position: 41, share: 0.5 }]);
    const count = reports.length;
    tracker.removeCard("10");
    expect(reports).toHaveLength(count);
  });
});
