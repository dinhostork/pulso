import { router } from "expo-router";
import { act, fireEvent, screen, waitFor, within } from "expo-router/testing-library";

import { FeedScreen } from "@/features/feed/FeedScreen";
import { MAX_FEED_PAGES, queryKeys } from "@/server-state/query";
import {
  EMPTY_FEED,
  json,
  renderReadingApp,
  storedSession,
  TEST_USERS,
  type ProductHandler,
  type ProductRequest,
} from "@/test-utils/readingApp";
import { hostNodes, refreshControl } from "@/test-utils/hostNodes";
import { deferred, feedPage, storyCard, type Deferred } from "@/test-utils/stories";

import type { CardVisibility } from "../visibility";

type Reply = Response | (() => Response | Promise<Response>);

/**
 * A scripted `/api/feed`: replies are queued per cursor ("" is the first
 * page) and consumed in order; the last reply for a cursor repeats.
 */
function feedServer(initial: Record<string, Reply[]> = {}) {
  const replies = new Map(Object.entries(initial));
  const handler: ProductHandler = async (request: ProductRequest) => {
    if (request.path !== "/api/feed") {
      return json({ code: "story_not_found", detail: "The Story was not found." }, 404);
    }
    const queue = replies.get(request.query.get("cursor") ?? "") ?? [json(EMPTY_FEED)];
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    return typeof reply === "function" ? reply() : reply.clone();
  };
  return {
    handler,
    reply(cursor: string, ...next: Reply[]) {
      replies.set(cursor, next);
    },
    /** The next request for `cursor` waits until the returned gate opens. */
    hold(cursor: string, response: () => Response): Deferred<void> {
      const gate = deferred<void>();
      const rest = replies.get(cursor) ?? [];
      replies.set(cursor, [
        async () => {
          await gate.promise;
          return response();
        },
        ...(rest.length ? rest : [json(EMPTY_FEED)]),
      ]);
      return gate;
    },
  };
}

async function openFeed(server: ReturnType<typeof feedServer>, username = "reader") {
  const runtime = await storedSession(username, server.handler);
  const app = await renderReadingApp(runtime);
  await waitFor(() => expect(runtime.controller.snapshot().status).toBe("authenticated"));
  return { runtime, app };
}

function feedCalls(runtime: { productCalls: ProductRequest[] }) {
  return runtime.productCalls
    .filter((call) => call.path === "/api/feed")
    .map((call) => call.query.get("cursor"));
}

function list() {
  return screen.getByTestId("feed-list");
}

/** `fireEvent` reaches FlatList's `onEndReached` from the list's host scroll view. */
async function endReached(distanceFromEnd = 0) {
  await fireEvent(list(), "endReached", { distanceFromEnd });
}

/** `fireEvent` reaches RefreshControl's `onRefresh` from its host node. */
async function pullToRefresh() {
  await fireEvent(refreshControl(), "refresh");
}

afterEach(() => jest.restoreAllMocks());

describe("Feed content", () => {
  it("renders one card for a Story supported by three Articles", async () => {
    const server = feedServer({
      "": [
        json(
          feedPage([
            storyCard("45", {
              title: "Council approves the river plan",
              topics: ["Environment", "Cities"],
              context: "The plan followed two years of hearings.",
              articles: 3,
              sources: 2,
              citedArticleIds: ["101", "102", "103"],
            }),
          ]),
        ),
      ],
    });
    await openFeed(server);

    const card = await screen.findByTestId("story-card-45");
    expect(screen.getAllByTestId(/^story-card-/)).toHaveLength(1);
    const content = within(card);
    expect(content.getByText("Council approves the river plan")).toBeTruthy();
    expect(content.getByText("Story 45 summary.")).toBeTruthy();
    expect(content.getByText("Context")).toBeTruthy();
    expect(content.getByText("The plan followed two years of hearings.")).toBeTruthy();
    expect(content.getByText("Environment · Cities")).toBeTruthy();
    expect(content.getByText("2 sources · 3 articles")).toBeTruthy();
    expect(content.getByText(/^Latest publication /)).toBeTruthy();
    // Source count is a count of publishers, not a verification claim.
    expect(content.queryByText(/verified|independent|confirmed/i)).toBeNull();
  });

  it("uses singular wording for a one-source Story", async () => {
    const server = feedServer({ "": [json(feedPage([storyCard("42")]))] });
    await openFeed(server);
    expect(await screen.findByText("1 source · 1 article")).toBeTruthy();
  });

  it("never fabricates missing Topics, Summary, Context or publication time", async () => {
    const server = feedServer({
      "": [
        json(
          feedPage([
            storyCard("42", {
              summary: null,
              topics: [],
              firstPublishedAt: null,
              lastPublishedAt: null,
            }),
          ]),
        ),
      ],
    });
    await openFeed(server);
    const card = within(await screen.findByTestId("story-card-42"));
    expect(card.getByText("Publication time not available")).toBeTruthy();
    expect(card.queryByText("Context")).toBeNull();
    expect(card.queryByText(/summary/i)).toBeNull();
    expect(card.queryByLabelText(/^Topics:/)).toBeNull();
    // Story creation time is not presented as a publication time.
    expect(card.queryByText(/Latest publication/)).toBeNull();
    for (const invented of [/why it matters/i, /key points/i, /pulse/i, /opinion/i, /likes?\b/i]) {
      expect(screen.queryByText(invented)).toBeNull();
    }
  });

  it("shows the viewer's saved state as a worded label", async () => {
    const server = feedServer({ "": [json(feedPage([storyCard("42", { bookmarked: true })]))] });
    await openFeed(server);
    const card = within(await screen.findByTestId("story-card-42"));
    expect(card.getByLabelText("Status: Saved")).toBeTruthy();
  });

  it("requests only approved API paths: no images, media or impressions", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("2"), storyCard("1")], "c1"))],
      c1: [json(feedPage([storyCard("1")]))],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-2");
    await endReached();
    await screen.findByTestId("story-card-1");

    const urls = runtime.fetch.mock.calls.map(([url]) => new URL(String(url)));
    expect(urls.every((url) => url.origin === "https://api.example.com")).toBe(true);
    expect(urls.every((url) => url.pathname.startsWith("/api/"))).toBe(true);
    expect(runtime.productCalls.map((call) => call.path)).toEqual(["/api/feed", "/api/feed"]);
    const media = hostNodes().filter((node) => /image|video|webview/i.test(String(node.type)));
    expect(media).toEqual([]);
  });
});

describe("Feed states", () => {
  it("distinguishes first load, first-load failure and retry", async () => {
    const gate = deferred<void>();
    const server = feedServer({
      "": [
        async () => {
          await gate.promise;
          return json({ code: "server_error", detail: "x" }, 500);
        },
        json(feedPage([storyCard("5")])),
      ],
    });
    await openFeed(server);
    expect(await screen.findByText("Loading Stories")).toBeTruthy();

    gate.resolve();
    expect(await screen.findByText("Feed unavailable")).toBeTruthy();
    expect(screen.queryByText("No Stories yet")).toBeNull();
    expect(screen.queryByTestId(/^story-card-/)).toBeNull();

    await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("story-card-5")).toBeTruthy();
  });

  it("shows an empty success as empty, not as an error", async () => {
    const server = feedServer({ "": [json(EMPTY_FEED)] });
    await openFeed(server);
    expect(await screen.findByText("No Stories yet")).toBeTruthy();
    expect(screen.queryByText("Feed unavailable")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("refreshes an empty Feed into content", async () => {
    const server = feedServer({
      "": [json(EMPTY_FEED), json(feedPage([storyCard("8")]))],
    });
    await openFeed(server);
    await fireEvent.press(await screen.findByRole("button", { name: "Refresh" }));
    expect(await screen.findByTestId("story-card-8")).toBeTruthy();
  });
});

describe("Feed pagination", () => {
  it("loads the next page, then states the end of the Feed", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3"), storyCard("2")], "c1"))],
      c1: [json(feedPage([storyCard("1")]))],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    expect(screen.queryByTestId("feed-footer-end")).toBeNull();

    await endReached();

    expect(await screen.findByTestId("story-card-1")).toBeTruthy();
    expect(await screen.findByText("You have reached the end of the Feed.")).toBeTruthy();
    expect(feedCalls(runtime)).toEqual([null, "c1"]);

    await endReached();
    expect(feedCalls(runtime)).toEqual([null, "c1"]);
  });

  it("ignores repeated end-reached events while a page request is in flight", async () => {
    const server = feedServer({ "": [json(feedPage([storyCard("3")], "c1"))] });
    const gate = server.hold("c1", () => json(feedPage([storyCard("1")])));
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");

    await endReached();
    expect(await screen.findByTestId("feed-footer-loading")).toBeTruthy();
    await endReached();
    await endReached(10);
    expect(feedCalls(runtime)).toEqual([null, "c1"]);

    gate.resolve();
    expect(await screen.findByTestId("story-card-1")).toBeTruthy();
    expect(feedCalls(runtime)).toEqual([null, "c1"]);
  });

  it("keeps loaded pages when a page fails and retries only on request", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3")], "c1"))],
      c1: [json({ code: "server_error", detail: "x" }, 503), json(feedPage([storyCard("1")]))],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");

    await endReached();
    expect(await screen.findByText("More Stories could not be loaded.")).toBeTruthy();
    expect(screen.getByTestId("story-card-3")).toBeTruthy();
    expect(screen.queryByText("Feed unavailable")).toBeNull();

    // Scrolling again does not hammer the failing page.
    await endReached();
    expect(feedCalls(runtime)).toEqual([null, "c1"]);

    await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("story-card-1")).toBeTruthy();
    expect(screen.getByTestId("story-card-3")).toBeTruthy();
    expect(feedCalls(runtime)).toEqual([null, "c1", "c1"]);
  });

  it("offers a restart instead of a retry when the cursor has expired", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3")], "c1")), json(feedPage([storyCard("9")]))],
      c1: [json({ code: "invalid_cursor", detail: "The cursor is invalid or expired." }, 400)],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();

    expect(await screen.findByTestId("feed-footer-expired")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByTestId("story-card-3")).toBeTruthy();

    await fireEvent.press(screen.getByRole("button", { name: "Restart from the newest Stories" }));
    expect(await screen.findByTestId("story-card-9")).toBeTruthy();
    expect(feedCalls(runtime)).toEqual([null, "c1", null]);
  });

  it("does not duplicate a Story repeated by a later page", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3"), storyCard("2")], "c1"))],
      c1: [json(feedPage([storyCard("2"), storyCard("1")]))],
    });
    await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();
    await screen.findByTestId("story-card-1");
    expect(screen.getAllByTestId(/^story-card-/).map((card) => card.props.testID)).toEqual([
      "story-card-3",
      "story-card-2",
      "story-card-1",
    ]);
  });

  it("stops at the retained-page bound and restarts from the newest Stories on request", async () => {
    const replies: Record<string, Reply[]> = {};
    for (let page = 0; page <= MAX_FEED_PAGES; page += 1) {
      replies[page === 0 ? "" : `c${page}`] = [
        json(feedPage([storyCard(String(1000 - page))], `c${page + 1}`)),
      ];
    }
    const server = feedServer(replies);
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-1000");
    for (let page = 1; page < MAX_FEED_PAGES; page += 1) {
      await endReached();
      await screen.findByTestId(`story-card-${1000 - page}`);
    }

    expect(await screen.findByTestId("feed-footer-bound")).toBeTruthy();
    await endReached();
    expect(feedCalls(runtime)).toHaveLength(MAX_FEED_PAGES);
    expect(screen.getAllByTestId(/^story-card-/)).toHaveLength(MAX_FEED_PAGES);

    server.reply("", json(feedPage([storyCard("2000")], "fresh1")));
    await fireEvent.press(screen.getByRole("button", { name: "Restart from the newest Stories" }));

    expect(await screen.findByTestId("story-card-2000")).toBeTruthy();
    expect(screen.getAllByTestId(/^story-card-/)).toHaveLength(1);
    expect(screen.queryByTestId("feed-footer-bound")).toBeNull();
    expect(feedCalls(runtime).at(-1)).toBeNull();
  });
});

describe("Feed refresh", () => {
  it("replaces the cursor chain after a successful pull-to-refresh", async () => {
    const server = feedServer({
      "": [
        json(feedPage([storyCard("3")], "old1")),
        json(feedPage([storyCard("9"), storyCard("3")], "new1")),
      ],
      old1: [json(feedPage([storyCard("2")], "old2"))],
      new1: [json(feedPage([storyCard("1")]))],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();
    await screen.findByTestId("story-card-2");

    await pullToRefresh();

    expect(await screen.findByTestId("story-card-9")).toBeTruthy();
    expect(screen.queryByTestId("story-card-2")).toBeNull();
    await endReached();
    expect(await screen.findByTestId("story-card-1")).toBeTruthy();
    expect(feedCalls(runtime)).toEqual([null, "old1", null, "new1"]);
  });

  it("ignores a page from the old chain that arrives after the refresh", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3")], "old1")), json(feedPage([storyCard("9")], "new1"))],
    });
    const stalePage = server.hold("old1", () => json(feedPage([storyCard("2")], "old2")));
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();
    await screen.findByTestId("feed-footer-loading");

    await pullToRefresh();
    expect(await screen.findByTestId("story-card-9")).toBeTruthy();

    await act(async () => stalePage.resolve());
    await act(async () => undefined);
    expect(screen.queryByTestId("story-card-2")).toBeNull();
    expect(screen.queryByTestId("story-card-3")).toBeNull();
    expect(
      runtime.queryClient.getQueryData<{ pageParams: unknown[] }>(queryKeys.feed("1"))?.pageParams,
    ).toEqual([null]);
  });

  it("keeps the loaded cards when a refresh fails and offers a targeted retry", async () => {
    const server = feedServer({
      "": [
        json(feedPage([storyCard("3")], "c1")),
        json({ code: "server_error", detail: "x" }, 503),
        json(feedPage([storyCard("9")])),
      ],
      c1: [json(feedPage([storyCard("2")], "c2"))],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();
    await screen.findByTestId("story-card-2");

    await pullToRefresh();

    expect(
      await screen.findByText(
        "The Feed could not be refreshed. The Stories below were loaded earlier.",
      ),
    ).toBeTruthy();
    expect(screen.getByTestId("story-card-3")).toBeTruthy();
    expect(screen.getByTestId("story-card-2")).toBeTruthy();
    expect(screen.queryByText("Feed unavailable")).toBeNull();
    // The old chain is intact: the next page still follows its cursor.
    await endReached();
    expect(feedCalls(runtime)).toEqual([null, "c1", null, "c2"]);

    await fireEvent.press(screen.getByRole("button", { name: "Try refreshing again" }));
    expect(await screen.findByTestId("story-card-9")).toBeTruthy();
    expect(screen.queryByTestId("feed-refresh-error")).toBeNull();
  });

  it("keeps cards and shows no refresh error when an automatic refetch fails", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3")])), json({ code: "server_error", detail: "x" }, 503)],
    });
    const { runtime } = await openFeed(server);
    await screen.findByTestId("story-card-3");

    await act(() => runtime.queryClient.invalidateQueries({ queryKey: queryKeys.feed("1") }));

    expect(screen.getByTestId("story-card-3")).toBeTruthy();
    expect(screen.queryByTestId("feed-refresh-error")).toBeNull();
  });
});

describe("Feed navigation", () => {
  it("opens the Story and its sources with the card's Story ID", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("9007199254740993"), storyCard("41")]))],
    });
    const { app } = await openFeed(server);

    await fireEvent.press(await screen.findByTestId("story-open-41"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/41"));

    await act(() => router.back());
    await fireEvent.press(await screen.findByTestId("story-sources-9007199254740993"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/9007199254740993/sources"));
  });

  it("returns from a Story to the same loaded Feed without refetching", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3")], "c1"))],
      c1: [json(feedPage([storyCard("2")]))],
    });
    const { runtime, app } = await openFeed(server);
    await screen.findByTestId("story-card-3");
    await endReached();
    const listBefore = list();

    await fireEvent.press(await screen.findByTestId("story-open-2"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/2"));
    await act(() => router.back());
    await waitFor(() => expect(app.pathname()).toBe("/"));

    expect(screen.getByTestId("story-card-2")).toBeTruthy();
    expect(screen.getByTestId("story-card-3")).toBeTruthy();
    // The same list instance (and so its scroll offset) survived the detail visit.
    expect(list()).toBe(listBefore);
    expect(feedCalls(runtime)).toEqual([null, "c1"]);
  });
});

describe("Feed account isolation", () => {
  it("never shows one account's Feed or saved state to the next account", async () => {
    const handler: ProductHandler = ({ path, user }) =>
      path === "/api/feed"
        ? json(
            feedPage([
              user === "reader"
                ? storyCard("100", { title: "Reader Story", bookmarked: true })
                : storyCard("200", { title: "Other Story" }),
            ]),
          )
        : json({ code: "story_not_found", detail: "x" }, 404);
    const { runtime } = await openFeed({ handler } as ReturnType<typeof feedServer>);
    expect(await screen.findByText("Reader Story")).toBeTruthy();

    await act(() => runtime.controller.logout());
    await fireEvent.changeText(await screen.findByLabelText("Username"), "other");
    await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS.other.password);
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Other Story")).toBeTruthy();
    expect(screen.queryByText("Reader Story")).toBeNull();
    expect(screen.queryByLabelText("Status: Saved")).toBeNull();
    expect(runtime.queryClient.getQueryData(queryKeys.feed("1"))).toBeUndefined();
    expect(runtime.productCalls.map((call) => call.user)).toEqual(["reader", "other"]);
  });
});

describe("Feed visibility seam", () => {
  function layout(y: number, height: number) {
    return { nativeEvent: { layout: { x: 0, y, width: 360, height } } };
  }

  async function openWithSeam(server: ReturnType<typeof feedServer>) {
    const reports: CardVisibility[][] = [];
    const onVisibilityChange = (cards: CardVisibility[]) => reports.push(cards);
    const runtime = await storedSession("reader", server.handler);
    await renderReadingApp(runtime, "/", {
      "(app)/(tabs)/index": () => <FeedScreen onVisibilityChange={onVisibilityChange} />,
    });
    return { runtime, reports };
  }

  it("reports nothing from fetch, page receipt or mount alone", async () => {
    const server = feedServer({ "": [json(feedPage([storyCard("3"), storyCard("2")]))] });
    const { runtime, reports } = await openWithSeam(server);
    await screen.findByTestId("story-card-2");
    expect(reports.every((cards) => cards.length === 0)).toBe(true);
    expect(runtime.productCalls.some((call) => call.path === "/api/feed-impressions")).toBe(false);
  });

  it("reports absolute positions and visible shares from list geometry", async () => {
    const server = feedServer({
      "": [json(feedPage([storyCard("3"), storyCard("2"), storyCard("1")]))],
    });
    const { runtime, reports } = await openWithSeam(server);
    await screen.findByTestId("story-card-1");

    await fireEvent(screen.getByTestId("feed-cell-3"), "layout", layout(0, 400));
    await fireEvent(screen.getByTestId("feed-cell-2"), "layout", layout(416, 400));
    await fireEvent(screen.getByTestId("feed-cell-1"), "layout", layout(832, 400));
    expect(reports.every((cards) => cards.length === 0)).toBe(true);
    await fireEvent(screen.getByTestId("feed-list"), "layout", layout(0, 800));
    expect(reports.at(-1)).toEqual([
      { storyId: "3", position: 0, share: 1 },
      { storyId: "2", position: 1, share: 0.96 },
    ]);

    await fireEvent.scroll(screen.getByTestId("feed-list"), {
      nativeEvent: {
        contentOffset: { x: 0, y: 632 },
        layoutMeasurement: { width: 360, height: 800 },
        contentSize: { width: 360, height: 1232 },
      },
    });
    expect(reports.at(-1)?.map((card) => [card.storyId, card.position, card.share])).toEqual([
      ["2", 1, 0.46],
      ["1", 2, 1],
    ]);
    expect(runtime.productCalls.some((call) => call.path === "/api/feed-impressions")).toBe(false);
  });
});
