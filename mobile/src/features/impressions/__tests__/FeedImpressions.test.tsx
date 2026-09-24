import { router } from "expo-router";
import { act, fireEvent, screen, waitFor } from "expo-router/testing-library";
import { AppState, type AppStateStatus } from "react-native";

import type { FeedImpressionEvent } from "@/api/types";
import {
  json,
  renderReadingApp,
  storedSession,
  TEST_USERS,
  type ProductRequest,
} from "@/test-utils/readingApp";
import { refreshControl } from "@/test-utils/hostNodes";
import { feedPage, storyCard, storyDetail } from "@/test-utils/stories";

// Node's CSPRNG stands in for the native generator, which Jest cannot load.
jest.mock("expo-crypto", () => ({ randomUUID: jest.fn(() => crypto.randomUUID()) }));

interface Delivery {
  user: string | null;
  events: FeedImpressionEvent[];
}

/**
 * A product API whose Feed replies are queued (the last repeats) and whose
 * impression endpoint records every request with the user its token names.
 */
function server(feed: Response[], impressions: () => Response | null = () => null) {
  const deliveries: Delivery[] = [];
  const handler = ({ path, body, user }: ProductRequest) => {
    if (path === "/api/feed") return (feed.length > 1 ? feed.shift()! : feed[0]).clone();
    if (path === "/api/feed-impressions") {
      const events = (body as { events: FeedImpressionEvent[] }).events;
      deliveries.push({ user, events });
      return (
        impressions() ??
        json({
          results: events.map((item) => ({
            event_id: item.event_id,
            outcome: "accepted",
            code: null,
          })),
        })
      );
    }
    const story = /^\/api\/stories\/(\d+)$/.exec(path);
    return story ? json(storyDetail(story[1])) : json({ code: "not_found", detail: "x" }, 404);
  };
  return { handler, deliveries };
}

const appStateListeners: ((state: AppStateStatus) => void)[] = [];

function setAppState(state: AppStateStatus) {
  (AppState as { currentState: AppStateStatus }).currentState = state;
  for (const listener of [...appStateListeners]) listener(state);
}

async function openFeed(feed: Response[], impressions?: () => Response | null) {
  const api = server(feed, impressions);
  const runtime = await storedSession("reader", api.handler);
  const app = await renderReadingApp(runtime);
  await waitFor(() => expect(runtime.controller.snapshot().status).toBe("authenticated"));
  return { ...api, runtime, app };
}

const layout = (y: number, height: number) => ({
  nativeEvent: { layout: { x: 0, y, width: 360, height } },
});

/** Lays out cards of 400 points (16-point gaps) in a viewport of 800. */
async function layOut(storyIds: string[], viewport = 800) {
  for (const [index, storyId] of storyIds.entries()) {
    await fireEvent(screen.getByTestId(`feed-cell-${storyId}`), "layout", layout(index * 416, 400));
  }
  await fireEvent(screen.getByTestId("feed-list"), "layout", layout(0, viewport));
}

async function scrollTo(y: number) {
  await fireEvent.scroll(screen.getByTestId("feed-list"), {
    nativeEvent: {
      contentOffset: { x: 0, y },
      layoutMeasurement: { width: 360, height: 800 },
      contentSize: { width: 360, height: 5000 },
    },
  });
}

async function advance(ms: number) {
  await act(() => jest.advanceTimersByTimeAsync(ms));
}

function delivered(deliveries: Delivery[]) {
  return deliveries.flatMap((delivery) => delivery.events);
}

beforeEach(() => {
  appStateListeners.length = 0;
  jest.spyOn(AppState, "addEventListener").mockImplementation((_type, listener) => {
    appStateListeners.push(listener as (state: AppStateStatus) => void);
    return {
      remove: () => {
        appStateListeners.splice(appStateListeners.indexOf(listener as never), 1);
      },
    } as ReturnType<typeof AppState.addEventListener>;
  });
  (AppState as { currentState: AppStateStatus }).currentState = "active";
});
afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

/** Fake timers start after the real async sign-in and first render. */
async function withFakeClock() {
  jest.useFakeTimers({ now: new Date("2026-09-19T10:06:00Z") });
}

describe("qualified FeedImpressions on HOME_FEED", () => {
  it("sends nothing for fetched, received or mounted cards without visible dwell", async () => {
    const { deliveries } = await openFeed([json(feedPage([storyCard("3"), storyCard("2")]))]);
    await screen.findByTestId("story-card-2");
    await withFakeClock();
    await advance(60_000);
    expect(deliveries).toEqual([]);
  });

  it("delivers one event per visible Story with absolute positions after 1 s + flush", async () => {
    const { deliveries } = await openFeed([
      json(feedPage([storyCard("3"), storyCard("2"), storyCard("1")])),
    ]);
    await screen.findByTestId("story-card-1");
    await withFakeClock();
    await layOut(["3", "2", "1"]);
    await advance(999);
    await advance(5000);
    // At 999 ms nothing qualified, so the first flush window saw nothing... until 1000 ms.
    await advance(5000);

    const events = delivered(deliveries);
    expect(events.map((item) => [item.story_id, item.position])).toEqual([
      ["3", 0],
      ["2", 1],
    ]);
    expect(new Set(events.map((item) => item.feed_session_id)).size).toBe(1);
    expect(deliveries.every((delivery) => delivery.user === "reader")).toBe(true);
  });

  it("does not repeat a Story after scrolling away and back in the same session", async () => {
    const { deliveries } = await openFeed([json(feedPage([storyCard("3"), storyCard("2")]))]);
    await screen.findByTestId("story-card-2");
    await withFakeClock();
    await layOut(["3", "2"]);
    await advance(1000);
    await scrollTo(832);
    await advance(2000);
    await scrollTo(0);
    await advance(2000);
    await advance(10_000);
    expect(delivered(deliveries).map((item) => item.story_id)).toEqual(["3", "2"]);
  });

  it("does not count a fast tap into a Story, and detail and back add no duplicate", async () => {
    const { deliveries, app } = await openFeed([json(feedPage([storyCard("3")]))]);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(300);
    await fireEvent.press(screen.getByTestId("story-open-3"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/3"));
    // Detail is not HOME_FEED: however long it is read, nothing qualifies.
    await advance(30_000);
    expect(delivered(deliveries)).toEqual([]);

    await act(() => router.back());
    await waitFor(() => expect(app.pathname()).toBe("/"));
    await advance(1000);
    await advance(5000);
    expect(delivered(deliveries).map((item) => item.story_id)).toEqual(["3"]);

    await fireEvent.press(screen.getByTestId("story-open-3"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/3"));
    await act(() => router.back());
    await advance(20_000);
    expect(delivered(deliveries)).toHaveLength(1);
  });

  it("emits nothing while Saved is shown", async () => {
    const { deliveries } = await openFeed([json(feedPage([storyCard("3")]))]);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(500);
    await fireEvent.press(screen.getByTestId("tab-saved"));
    await advance(30_000);
    expect(deliveries).toEqual([]);
  });

  it("resets the dwell when the app goes to the background", async () => {
    const { deliveries } = await openFeed([json(feedPage([storyCard("3")]))]);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(900);
    await act(async () => setAppState("background"));
    await advance(30_000);
    expect(delivered(deliveries)).toEqual([]);

    await act(async () => setAppState("active"));
    await advance(999);
    await act(async () => setAppState("inactive"));
    await advance(30_000);
    expect(delivered(deliveries)).toEqual([]);

    await act(async () => setAppState("active"));
    await advance(1000);
    await advance(5000);
    expect(delivered(deliveries).map((item) => item.story_id)).toEqual(["3"]);
  });

  it("starts a new session after a successful refresh but not after a failed one", async () => {
    const { deliveries } = await openFeed([
      json(feedPage([storyCard("3")])),
      json({ code: "server_error", detail: "x" }, 503),
      json(feedPage([storyCard("3")])),
    ]);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(1000);
    await advance(5000);
    expect(delivered(deliveries)).toHaveLength(1);
    const [first] = delivered(deliveries);

    await fireEvent(refreshControl(), "refresh");
    expect(await screen.findByTestId("feed-refresh-error")).toBeTruthy();
    await advance(10_000);
    expect(delivered(deliveries)).toHaveLength(1);

    await fireEvent(refreshControl(), "refresh");
    await waitFor(() => expect(screen.queryByTestId("feed-refresh-error")).toBeNull());
    await layOut(["3"]);
    await advance(1000);
    await advance(5000);
    const events = delivered(deliveries);
    expect(events).toHaveLength(2);
    expect(events[1].story_id).toBe("3");
    expect(events[1].feed_session_id).not.toBe(first.feed_session_id);
    expect(events[1].event_id).not.toBe(first.event_id);
  });

  it("keeps the Feed usable while telemetry fails, retrying the same event", async () => {
    const { deliveries } = await openFeed(
      [json(feedPage([storyCard("3")], "c1")), json(feedPage([storyCard("1")]))],
      () => json({ code: "server_error", detail: "x" }, 503),
    );
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(1000);
    await advance(5000);
    await advance(2000);
    expect(deliveries).toHaveLength(2);
    expect(JSON.stringify(deliveries[1].events)).toBe(JSON.stringify(deliveries[0].events));

    await fireEvent(screen.getByTestId("feed-list"), "endReached", { distanceFromEnd: 0 });
    expect(await screen.findByTestId("story-card-1")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("FeedImpression delivery through the session", () => {
  it("replays a 401 once through the shared single-flight refresh, same payload", async () => {
    let unauthorized = 1;
    const { deliveries, runtime } = await openFeed([json(feedPage([storyCard("3")]))], () =>
      unauthorized-- > 0 ? json({ code: "not_authenticated", detail: "x" }, 401) : null,
    );
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(1000);
    await advance(5000);

    expect(deliveries).toHaveLength(2);
    expect(JSON.stringify(deliveries[1].events)).toBe(JSON.stringify(deliveries[0].events));
    const refreshes = runtime.fetch.mock.calls.filter(([url]) =>
      String(url).endsWith("/api/auth/refresh"),
    );
    // One refresh at restore plus one for the 401: the queue adds no auth loop of its own.
    expect(refreshes).toHaveLength(2);
    expect(runtime.impressions.size()).toBe(0);
    expect(runtime.impressions.diagnostics().acknowledged).toBe(1);
  });
});

describe("FeedImpression account isolation", () => {
  it("never sends account A's exposure with account B's credentials", async () => {
    const { deliveries, runtime } = await openFeed([json(feedPage([storyCard("3")]))]);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(1000);
    expect(runtime.impressions.size()).toBe(1);

    // A signs out before the flush; B signs in on the same device.
    await act(() => runtime.controller.logout());
    expect(runtime.impressions.size()).toBe(0);
    await act(() => runtime.controller.signIn("other", TEST_USERS.other.password));
    await advance(60_000);

    expect(deliveries.filter((delivery) => delivery.user === "other")).toEqual([]);
    expect(delivered(deliveries).map((item) => item.story_id)).not.toContain("3");
  });

  it("drops account A's in-flight batch when the account changes mid-request", async () => {
    let releaseA!: () => void;
    const held = new Promise<void>((resolve) => (releaseA = resolve));
    const api = server([json(feedPage([storyCard("3")]))]);
    const runtime = await storedSession("reader", async (request) => {
      if (request.path === "/api/feed-impressions" && request.user === "reader") await held;
      return api.handler(request);
    });
    await renderReadingApp(runtime);
    await screen.findByTestId("story-card-3");
    await withFakeClock();
    await layOut(["3"]);
    await advance(1000);
    await advance(5000);
    const impressionUsers = () =>
      runtime.productCalls
        .filter((call) => call.path === "/api/feed-impressions")
        .map((call) => call.user);
    expect(impressionUsers()).toEqual(["reader"]);

    await act(() => runtime.controller.logout());
    await act(() => runtime.controller.signIn("other", TEST_USERS.other.password));
    await act(async () => releaseA());
    await advance(60_000);

    // A's late outcome is ignored: nothing is re-queued or re-sent, least of all as B.
    expect(impressionUsers()).toEqual(["reader"]);
    expect(runtime.impressions.size()).toBe(0);
  });
});
