import { ApiError } from "@/api/errors";
import type { FeedImpressionEvent, FeedImpressionResponse } from "@/api/types";
import { deferred, type Deferred } from "@/test-utils/stories";

import { DELIVERY_POLICY } from "../policy";
import { ImpressionQueue } from "../queue";

let sequence = 0;
function event(storyId?: string): FeedImpressionEvent {
  sequence += 1;
  return {
    event_id: `4f4bb18e-b0e0-4e7f-8cf8-${String(sequence).padStart(12, "0")}`,
    story_id: storyId ?? String(sequence),
    feed_session_id: "dd42a0ee-0727-4796-bd11-dba56f1c498b",
    position: 0,
    surface: "HOME_FEED",
    policy_version: 1,
    occurred_at: "2026-09-19T10:06:01.000Z",
  };
}

const accepted = (events: FeedImpressionEvent[]): FeedImpressionResponse => ({
  results: events.map((item) => ({ event_id: item.event_id, outcome: "accepted", code: null })),
});

/** A queue whose sends are held until the test resolves or rejects them. */
function harness() {
  const sends: {
    events: FeedImpressionEvent[];
    body: string;
    reply: Deferred<FeedImpressionResponse>;
    signal: AbortSignal;
  }[] = [];
  const queue = new ImpressionQueue({
    now: Date.now,
    send: (events, signal) => {
      const reply = deferred<FeedImpressionResponse>();
      sends.push({ events, body: JSON.stringify({ events }), reply, signal });
      return reply.promise;
    },
  });
  queue.onSessionChange({ epoch: 1, accountId: "1" });
  return { queue, sends, epoch: 1 };
}

async function advance(ms: number) {
  await jest.advanceTimersByTimeAsync(ms);
}

beforeEach(() => {
  jest.useFakeTimers({ now: new Date("2026-09-19T10:06:00Z") });
  sequence = 0;
});
afterEach(() => jest.useRealTimers());

describe("ImpressionQueue bounds", () => {
  it("uses the validated v1 bounds", () => {
    expect(DELIVERY_POLICY).toMatchObject({
      maxQueued: 100,
      batchSize: 20,
      ttlMs: 600_000,
      flushIntervalMs: 5000,
      maxRetries: 3,
    });
  });

  it("holds at most 100 events and drops the overflow", () => {
    const { queue, epoch } = harness();
    for (let index = 0; index < 100; index += 1) expect(queue.enqueue(event(), epoch)).toBe(true);
    expect(queue.enqueue(event(), epoch)).toBe(false);
    expect(queue.size()).toBe(100);
    expect(queue.diagnostics().dropped.overflow).toBe(1);
  });

  it("sends a full batch of 20 at once and never more than 20 per request", async () => {
    const { queue, sends, epoch } = harness();
    for (let index = 0; index < 25; index += 1) queue.enqueue(event(), epoch);
    await advance(0);
    expect(sends).toHaveLength(1);
    expect(sends[0].events).toHaveLength(20);
  });

  it("sends a partial batch 5 seconds after its oldest event", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    await advance(2000);
    queue.enqueue(event(), epoch);
    await advance(2999);
    expect(sends).toHaveLength(0);
    await advance(1);
    expect(sends).toHaveLength(1);
    expect(sends[0].events).toHaveLength(2);
  });

  it("keeps one request in flight at a time", async () => {
    const { queue, sends, epoch } = harness();
    for (let index = 0; index < 45; index += 1) queue.enqueue(event(), epoch);
    await advance(0);
    queue.flushNow();
    await advance(10_000);
    expect(sends).toHaveLength(1);

    sends[0].reply.resolve(accepted(sends[0].events));
    await advance(0);
    expect(sends).toHaveLength(2);
    expect(sends[1].events.map((item) => item.story_id)).toEqual(
      Array.from({ length: 20 }, (_, index) => String(index + 21)),
    );
  });

  it("drops events older than the 10-minute TTL", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    const rateLimited = (seconds: number) =>
      new ApiError("http", "feed_impressions.deliver", { status: 429, retryAfterSeconds: seconds });
    sends[0].reply.reject(rateLimited(590));
    await advance(590_000);
    expect(sends).toHaveLength(2);
    sends[1].reply.reject(rateLimited(30));
    await advance(30_000);
    expect(sends).toHaveLength(2);
    expect(queue.size()).toBe(0);
    expect(queue.diagnostics().dropped.expired).toBe(1);
  });

  it("flushes a waiting partial batch immediately on request (leaving Feed/background)", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    expect(sends).toHaveLength(1);
  });
});

describe("ImpressionQueue acknowledgements and retry", () => {
  it("removes accepted, duplicate and rejected events and keeps unmentioned ones", async () => {
    const { queue, sends, epoch } = harness();
    const events = [event(), event(), event(), event()];
    for (const item of events) queue.enqueue(item, epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.resolve({
      results: [
        { event_id: events[0].event_id, outcome: "accepted", code: null },
        { event_id: events[1].event_id, outcome: "duplicate", code: null },
        { event_id: events[2].event_id, outcome: "rejected", code: "event_conflict" },
      ],
    });
    await advance(0);
    expect(queue.size()).toBe(1);
    expect(queue.diagnostics()).toMatchObject({
      acknowledged: 1,
      duplicate: 1,
      rejected: { event_conflict: 1 },
    });

    await advance(DELIVERY_POLICY.retryBaseMs);
    expect(sends[1].events).toEqual([events[3]]);
  });

  it("replays exactly the same payload and UUIDs after a lost response", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event("45"), epoch);
    queue.enqueue(event("46"), epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.reject(new ApiError("network", "feed_impressions.deliver"));
    await advance(DELIVERY_POLICY.retryBaseMs);
    expect(sends).toHaveLength(2);
    expect(sends[1].body).toBe(sends[0].body);
    sends[1].reply.resolve({
      results: sends[1].events.map((item) => ({
        event_id: item.event_id,
        outcome: "duplicate",
        code: null,
      })),
    });
    await advance(0);
    expect(queue.size()).toBe(0);
  });

  it.each([
    ["network", new ApiError("network", "x")],
    ["timeout", new ApiError("timeout", "x")],
    ["5xx", new ApiError("http", "x", { status: 503 })],
  ])("retries a %s failure with backoff and stops after 3 retries", async (_name, error) => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    const delays = [2000, 4000, 8000];
    for (const delay of delays) {
      sends.at(-1)!.reply.reject(error);
      await advance(delay - 1);
      const count = sends.length;
      await advance(1);
      expect(sends.length).toBe(count + 1);
    }
    expect(sends).toHaveLength(4);
    sends.at(-1)!.reply.reject(error);
    await advance(60_000);
    expect(sends).toHaveLength(4);
    expect(queue.size()).toBe(0);
    expect(queue.diagnostics().dropped.retry_exhausted).toBe(1);
  });

  it("honours Retry-After on 429 without spending the retry budget", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.reject(new ApiError("http", "x", { status: 429, retryAfterSeconds: 45 }));
    await advance(44_999);
    expect(sends).toHaveLength(1);
    await advance(1);
    expect(sends).toHaveLength(2);
    expect(sends[1].body).toBe(sends[0].body);
  });

  it("lets a 429 wait beyond the TTL expire the events instead of retrying forever", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.reject(new ApiError("http", "x", { status: 429, retryAfterSeconds: 3600 }));
    await advance(3_600_000);
    expect(sends).toHaveLength(1);
    expect(queue.size()).toBe(0);
    expect(queue.diagnostics().dropped.expired).toBe(1);
  });

  it("drops a batch the server rejects as a whole (400/413) without retrying", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.enqueue(event(), epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.reject(new ApiError("http", "x", { status: 400, code: "validation_error" }));
    await advance(60_000);
    expect(sends).toHaveLength(1);
    expect(queue.diagnostics().dropped.terminal).toBe(2);
  });

  it("keeps diagnostics content-free", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event("45"), epoch);
    queue.flushNow();
    await advance(0);
    sends[0].reply.resolve(accepted(sends[0].events));
    await advance(0);
    const text = JSON.stringify(queue.diagnostics());
    expect(text).not.toMatch(/45|4f4bb18e|dd42a0ee|HOME_FEED/);
  });
});

describe("ImpressionQueue account boundary", () => {
  it("clears queued events and timers on logout", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event(), epoch);
    queue.onSessionChange({ epoch: 2, accountId: null });
    await advance(60_000);
    expect(queue.size()).toBe(0);
    expect(sends).toHaveLength(0);
    expect(queue.enqueue(event(), 2)).toBe(false);
  });

  it("rejects events from the previous account's producer after an account change", async () => {
    const { queue, sends } = harness();
    queue.onSessionChange({ epoch: 2, accountId: null });
    queue.onSessionChange({ epoch: 3, accountId: "2" });
    expect(queue.enqueue(event(), 1)).toBe(false);
    expect(queue.diagnostics().dropped.stale_session).toBe(1);
    await advance(60_000);
    expect(sends).toHaveLength(0);
  });

  it("aborts account A's in-flight batch and ignores its late outcome", async () => {
    const { queue, sends, epoch } = harness();
    queue.enqueue(event("45"), epoch);
    queue.flushNow();
    await advance(0);
    queue.onSessionChange({ epoch: 2, accountId: null });
    queue.onSessionChange({ epoch: 3, accountId: "2" });
    expect(sends[0].signal.aborted).toBe(true);

    // A late failure for A must not re-queue A's event for B.
    sends[0].reply.reject(new ApiError("network", "x"));
    await advance(60_000);
    expect(sends).toHaveLength(1);
    expect(queue.size()).toBe(0);

    queue.enqueue(event("46"), 3);
    queue.flushNow();
    await advance(0);
    expect(sends[1].events.map((item) => item.story_id)).toEqual(["46"]);
  });
});
