import * as Crypto from "expo-crypto";

import type { FeedImpressionEvent } from "@/api/types";

import { FeedExposureController } from "../exposure";
import { secureUuid } from "../ids";
import { ImpressionQueue } from "../queue";

// Node's CSPRNG stands in for the native generator, which Jest cannot load.
jest.mock("expo-crypto", () => ({ randomUUID: jest.fn(() => crypto.randomUUID()) }));

const randomUUID = Crypto.randomUUID as jest.Mock;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function setup() {
  const sent: FeedImpressionEvent[] = [];
  const queue = new ImpressionQueue({
    now: Date.now,
    send: async (events) => {
      sent.push(...events);
      return {
        results: events.map((item) => ({
          event_id: item.event_id,
          outcome: "accepted" as const,
          code: null,
        })),
      };
    },
  });
  queue.onSessionChange({ epoch: 4, accountId: "1" });
  const controller = new FeedExposureController({ queue });
  return { queue, controller, sent };
}

async function advance(ms: number) {
  await jest.advanceTimersByTimeAsync(ms);
}

beforeEach(() => {
  jest.useFakeTimers({ now: new Date("2026-09-19T10:06:00Z") });
  randomUUID.mockClear();
});
afterEach(() => jest.useRealTimers());

describe("secureUuid", () => {
  it("returns a v4 UUID from the platform's secure generator", () => {
    const value = secureUuid();
    expect(value).toMatch(UUID_V4);
    expect(randomUUID).toHaveBeenCalledTimes(1);
    expect(secureUuid()).not.toBe(value);
  });

  it("fails closed instead of falling back to an insecure source", () => {
    randomUUID.mockReturnValueOnce(undefined);
    expect(() => secureUuid()).toThrow("Secure UUID generation is unavailable");
  });
});

describe("FeedExposureController", () => {
  it("emits one event with the exact #43 shape and nothing else", async () => {
    const { controller, queue, sent } = setup();
    controller.startSession();
    controller.setGate(true);
    controller.observe([{ storyId: "45", position: 7, share: 0.5 }]);
    await advance(1000);
    expect(queue.size()).toBe(1);
    await advance(5000);

    expect(sent).toHaveLength(1);
    const [payload] = sent;
    expect(Object.keys(payload).sort()).toEqual([
      "event_id",
      "feed_session_id",
      "occurred_at",
      "policy_version",
      "position",
      "story_id",
      "surface",
    ]);
    expect(payload).toMatchObject({
      story_id: "45",
      position: 7,
      surface: "HOME_FEED",
      policy_version: 1,
      occurred_at: "2026-09-19T10:06:01.000Z",
      feed_session_id: controller.feedSessionId(),
    });
    expect(payload.event_id).toMatch(UUID_V4);
    expect(payload.feed_session_id).toMatch(UUID_V4);
    expect(payload.event_id).not.toBe(payload.feed_session_id);
  });

  it("keeps the session across gate changes and starts a new one on request", async () => {
    const { controller, queue } = setup();
    controller.startSession();
    const first = controller.feedSessionId();
    controller.setGate(true);
    controller.observe([{ storyId: "45", position: 0, share: 1 }]);
    await advance(1000);
    // Detail navigation and return: same session, no second event.
    controller.setGate(false);
    controller.setGate(true);
    await advance(2000);
    expect(controller.feedSessionId()).toBe(first);
    expect(queue.diagnostics().queued).toBe(1);

    // A successful explicit refresh: a new session lets the Story qualify again.
    controller.startSession();
    expect(controller.feedSessionId()).not.toBe(first);
    await advance(1000);
    expect(queue.diagnostics().queued).toBe(2);
  });

  it("produces nothing without a secure session ID", async () => {
    const { controller, queue } = setup();
    randomUUID.mockReturnValueOnce("not-a-uuid");
    controller.startSession();
    controller.setGate(true);
    controller.observe([{ storyId: "45", position: 0, share: 1 }]);
    await advance(2000);
    expect(queue.size()).toBe(0);
  });

  it("stops its timer on dispose", async () => {
    const { controller, queue } = setup();
    controller.startSession();
    controller.setGate(true);
    controller.observe([{ storyId: "45", position: 0, share: 1 }]);
    controller.dispose();
    await advance(10_000);
    expect(queue.diagnostics().queued).toBe(0);
  });

  it("drops a late exposure from a producer of an earlier session epoch", async () => {
    const { controller, queue } = setup();
    controller.startSession();
    controller.setGate(true);
    controller.observe([{ storyId: "45", position: 0, share: 1 }]);
    queue.onSessionChange({ epoch: 5, accountId: null });
    queue.onSessionChange({ epoch: 6, accountId: "2" });
    await advance(1000);
    expect(queue.size()).toBe(0);
    expect(queue.diagnostics().dropped.stale_session).toBe(1);
  });
});
