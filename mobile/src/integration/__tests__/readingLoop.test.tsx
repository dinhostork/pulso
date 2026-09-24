/**
 * The Phase 3 reading loop through the mocked HTTP boundary (#52).
 *
 * Every payload about the Elsby harbor Story is `reading-loop.json`, which the
 * backend suite `tests/reading/test_reading_loop.py` produces from the recorded
 * corpus through the real Story services, serializers and HTTP views. Here the
 * real transport, decoders, session controller, query cache, navigation and
 * screens consume it; only `fetch` and the operating system browser are fakes.
 */
import * as Linking from "expo-linking";
import { router } from "expo-router";
import { act, fireEvent, screen, waitFor, within } from "expo-router/testing-library";
import { AppState, type AppStateStatus } from "react-native";

import type { FeedImpressionEvent } from "@/api/types";
import {
  renderReadingApp,
  testSession,
  TEST_USERS,
  type TestRuntime,
} from "@/test-utils/readingApp";
import { readingServer, type ReadingServer } from "@/test-utils/readingServer";
import { storyDetail } from "@/test-utils/stories";

jest.mock("expo-linking", () => ({
  ...jest.requireActual("expo-linking"),
  openURL: jest.fn(async () => true),
}));
// Node's CSPRNG stands in for the native generator, which Jest cannot load.
jest.mock("expo-crypto", () => ({ randomUUID: jest.fn(() => crypto.randomUUID()) }));

// A backend-produced contract fixture: a test input only, never runtime data.
const loop = require("../../../../docs/contracts/mobile-feed/reading-loop.json") as {
  story: ReturnType<typeof storyDetail>;
  updating_story: ReturnType<typeof storyDetail>;
  sources: unknown;
  moved_article_id: string;
};

const openURL = Linking.openURL as jest.Mock;
const STORY = loop.story.id;
const TITLE = "Storm forces closure of Elsby harbor";

function productPaths(runtime: TestRuntime) {
  return runtime.productCalls.map((call) => `${call.method} ${call.path} ${call.user}`);
}

function authCalls(runtime: TestRuntime) {
  return runtime.fetch.mock.calls
    .map(([url]) => new URL(String(url)).pathname)
    .filter((path) => path.startsWith("/api/auth/"));
}

async function signIn(username: string) {
  await fireEvent.changeText(await screen.findByLabelText("Username"), username);
  await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS[username].password);
  await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
}

async function signOut() {
  await fireEvent.press(screen.getAllByRole("button", { name: "Sign out" })[0]);
  await screen.findByLabelText("Username");
}

async function coldStart(server: ReadingServer) {
  const runtime = testSession(undefined, server.handler);
  await renderReadingApp(runtime);
  return runtime;
}

function feedCard() {
  return within(screen.getByTestId(`story-card-${STORY}`));
}

beforeEach(() => openURL.mockClear());

describe("Phase 3 reading loop", () => {
  it("signs in, reads, opens a citation, saves, revisits, removes and switches account", async () => {
    const server = readingServer({ stories: [loop.story], sources: { [STORY]: loop.sources } });
    const runtime = await coldStart(server);
    await signIn("reader");

    // Feed: one card for the multi-source Story, from the server's facts.
    const card = within(await screen.findByTestId(`story-card-${STORY}`));
    expect(card.getByText(TITLE)).toBeTruthy();
    expect(card.getByText("3 sources · 4 articles")).toBeTruthy();

    // Detail: the SUMMARY citation decodes to its Article, Source and URL.
    await fireEvent.press(screen.getByTestId(`story-open-${STORY}`));
    const citations = within(await screen.findByTestId("citations-303"));
    expect(citations.getByText("Cited: Kestrel Post")).toBeTruthy();
    await fireEvent.press(citations.getByRole("button", { name: "Show cited publications (1)" }));
    const row = within(screen.getByTestId("citation-303-402"));
    expect(row.getByText("Kestrel Post")).toBeTruthy();
    await fireEvent.press(row.getByRole("button", { name: "Open publication (external site)" }));
    expect(openURL.mock.calls).toEqual([["https://kestrel-post.example/stories/harbor-storm-02"]]);

    // Sources: every current publication, one row per Article.
    await fireEvent.press(screen.getByRole("button", { name: "View current sources" }));
    await screen.findByTestId("source-404");
    for (const id of ["401", "402", "403", "404"]) {
      expect(screen.getByTestId(`source-${id}`)).toBeTruthy();
    }
    await act(() => router.back());

    // Save from detail: detail, Feed and Saved agree.
    await fireEvent.press(screen.getByTestId(`detail-bookmark-${STORY}`));
    await waitFor(() =>
      expect(screen.getByTestId(`detail-bookmark-${STORY}`)).toHaveAccessibleName(
        "Remove from Saved",
      ),
    );
    await act(() => router.back());
    expect(feedCard().getByLabelText("Status: Saved")).toBeTruthy();
    await fireEvent.press(screen.getByTestId("tab-saved"));
    expect(await screen.findByTestId(`saved-card-${STORY}`)).toBeTruthy();

    // The access token expires: the removal refreshes once and replays the DELETE.
    server.fail("DELETE", new RegExp(`^/api/bookmarks/${STORY}$`), { kind: "status", status: 401 });
    await fireEvent.press(screen.getByTestId(`saved-bookmark-${STORY}`));
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
    expect(authCalls(runtime)).toEqual(["/api/auth/login", "/api/auth/me", "/api/auth/refresh"]);
    await fireEvent.press(screen.getByTestId("tab-feed"));
    expect(feedCard().queryByLabelText("Status: Saved")).toBeNull();
    expect(server.savedIds("reader")).toEqual([]);

    // Save again, then another account signs in on the same device.
    await fireEvent.press(screen.getByTestId(`story-bookmark-${STORY}`));
    await waitFor(() => expect(feedCard().getByLabelText("Status: Saved")).toBeTruthy());
    await signOut();
    await signIn("other");
    await screen.findByTestId(`story-card-${STORY}`);
    expect(screen.queryByLabelText("Status: Saved")).toBeNull();
    await fireEvent.press(screen.getByTestId("tab-saved"));
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
    expect(server.savedIds("reader")).toEqual([STORY]);
    expect(productPaths(runtime).filter((call) => call.endsWith(" other"))).toEqual([
      "GET /api/feed other",
      "GET /api/bookmarks other",
    ]);
  });

  it("keeps a previous-generation citation reachable after its Article left the Story", async () => {
    const updating = loop.updating_story;
    const moved = loop.moved_article_id;
    const current = (loop.sources as { results: { id: string }[] }).results.filter(
      (article) => article.id !== moved,
    );
    const server = readingServer({
      stories: [updating],
      sources: { [STORY]: { results: current, next_cursor: null } },
    });
    const runtime = await coldStart(server);
    await signIn("reader");
    await act(() => router.push(`/stories/${STORY}`));

    expect(await screen.findByTestId("story-updating")).toBeTruthy();
    const title = within(screen.getByTestId("citations-301"));
    await fireEvent.press(title.getByRole("button", { name: "Show cited publications (1)" }));
    const row = within(screen.getByTestId(`citation-301-${moved}`));
    expect(row.getByText("No longer among this Story's current sources")).toBeTruthy();
    await fireEvent.press(row.getByRole("button", { name: "Open publication (external site)" }));
    expect(openURL).toHaveBeenLastCalledWith(
      "https://elsby-courier.example/stories/harbor-storm-01",
    );

    await fireEvent.press(screen.getByRole("button", { name: "View current sources" }));
    expect(await screen.findByTestId(`earlier-${moved}`)).toBeTruthy();
    expect(screen.queryByTestId(`source-${moved}`)).toBeNull();
    expect(await screen.findByTestId("source-402")).toBeTruthy();
    // Sources are requested in the synthesis context the citations belong to.
    expect(
      runtime.productCalls
        .filter((call) => call.path === `/api/stories/${STORY}/sources`)
        .map((call) => call.query.get("synthesis_id")),
    ).toEqual([updating.synthesis_id]);
  });
});

describe("Phase 3 exposure delivery", () => {
  const listeners: ((state: AppStateStatus) => void)[] = [];

  beforeEach(() => {
    listeners.length = 0;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_type, listener) => {
      listeners.push(listener as (state: AppStateStatus) => void);
      return { remove: () => undefined } as ReturnType<typeof AppState.addEventListener>;
    });
    (AppState as { currentState: AppStateStatus }).currentState = "active";
  });
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  async function advance(ms: number) {
    await act(() => jest.advanceTimersByTimeAsync(ms));
  }

  function deliveries(runtime: TestRuntime) {
    return runtime.productCalls
      .filter((call) => call.path === "/api/feed-impressions")
      .map((call) => ({
        user: call.user,
        ids: (call.body as { events: FeedImpressionEvent[] }).events.map((event) => event.event_id),
      }));
  }

  it("retries a failed delivery with the same event and never sends it as another account", async () => {
    const second = storyDetail("777", { title: "Council vote delayed" });
    const server = readingServer({ stories: [loop.story, second] });
    const runtime = await coldStart(server);
    await signIn("reader");
    await screen.findByTestId("story-card-777");
    jest.useFakeTimers({ now: new Date("2026-09-20T12:10:00Z") });

    const layout = (y: number) => ({
      nativeEvent: { layout: { x: 0, y, width: 360, height: 400 } },
    });
    await fireEvent(screen.getByTestId(`feed-cell-${STORY}`), "layout", layout(0));
    await fireEvent(screen.getByTestId("feed-cell-777"), "layout", layout(2000));
    await fireEvent(screen.getByTestId("feed-list"), "layout", layout(0));

    server.fail("POST", /^\/api\/feed-impressions$/, { kind: "status", status: 503 });
    await advance(1000);
    await advance(5000);
    await advance(2500);
    const attempts = deliveries(runtime);
    expect(attempts).toHaveLength(2);
    expect(attempts[1]).toEqual(attempts[0]);
    expect(attempts[0].user).toBe("reader");
    expect(attempts[0].ids).toHaveLength(1);
    expect(server.impressions.map((batch) => batch.events[0].story_id)).toEqual([STORY]);
    // The Feed stayed usable and unchanged while telemetry failed.
    expect(screen.getByTestId(`story-card-${STORY}`)).toBeTruthy();

    // A second exposure is qualified, then the account changes before delivery.
    await fireEvent.scroll(screen.getByTestId("feed-list"), {
      nativeEvent: {
        contentOffset: { x: 0, y: 2000 },
        layoutMeasurement: { width: 360, height: 400 },
        contentSize: { width: 360, height: 3000 },
      },
    });
    await advance(1000);
    expect(runtime.impressions.size()).toBe(1);
    jest.useRealTimers();
    await signOut();
    expect(runtime.impressions.size()).toBe(0);
    await signIn("other");
    await screen.findByTestId("story-card-777");
    expect(deliveries(runtime).every((delivery) => delivery.user === "reader")).toBe(true);
    expect(deliveries(runtime)).toHaveLength(2);
  });
});
