import { router } from "expo-router";
import { act, fireEvent, screen, waitFor, within } from "expo-router/testing-library";

import { queryKeys } from "@/server-state/query";
import { renderReadingApp, storedSession, testSession, TEST_USERS } from "@/test-utils/readingApp";
import { refreshControl } from "@/test-utils/hostNodes";
import { readingServer, type ReadingServer } from "@/test-utils/readingServer";
import { storyDetail } from "@/test-utils/stories";
import { createWebMemoryRefreshTokenStore } from "@/session/storage";

// Node's CSPRNG stands in for the native generator, which Jest cannot load.
jest.mock("expo-crypto", () => ({ randomUUID: jest.fn(() => crypto.randomUUID()) }));

const STORIES = [
  storyDetail("1", { title: "Harbor reopens" }),
  storyDetail("2", { title: "Council vote delayed" }),
];

async function openApp(server: ReadingServer, url = "/", username = "reader") {
  const runtime = await storedSession(username, server.handler);
  const app = await renderReadingApp(runtime, url);
  await waitFor(() => expect(runtime.controller.snapshot().status).toBe("authenticated"));
  return { runtime, app };
}

function feedCard(storyId: string) {
  return within(screen.getByTestId(`story-card-${storyId}`));
}

async function openSaved() {
  await fireEvent.press(screen.getByTestId("tab-saved"));
}

async function openFeedTab() {
  await fireEvent.press(screen.getByTestId("tab-feed"));
}

async function openDetail(storyId: string, app: { pathname(): string }) {
  await fireEvent.press(screen.getByTestId(`story-open-${storyId}`));
  await waitFor(() => expect(app.pathname()).toBe(`/stories/${storyId}`));
  return screen.findByTestId(`detail-bookmark-${storyId}`);
}

function bookmarkWrites(server: ReadingServer) {
  return server.writes.map((write) => `${write.method} ${write.storyId} ${write.user}`);
}

describe("bookmark state across Feed, detail and Saved", () => {
  it("reflects a save from the Feed in detail and Saved", async () => {
    const server = readingServer({ stories: STORIES });
    const { app } = await openApp(server);
    const button = await screen.findByTestId("story-bookmark-1");
    expect(button).toHaveAccessibleName("Save");

    await fireEvent.press(button);
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Remove from Saved");
    expect(feedCard("2").queryByLabelText("Status: Saved")).toBeNull();

    const detail = await openDetail("1", app);
    expect(detail).toHaveAccessibleName("Remove from Saved");
    await act(() => router.back());

    await openSaved();
    expect(await screen.findByTestId("saved-card-1")).toBeTruthy();
    expect(screen.queryByTestId("saved-card-2")).toBeNull();
    expect(bookmarkWrites(server)).toEqual(["PUT 1 reader"]);
  });

  it("reflects a save from detail in the Feed and Saved", async () => {
    const server = readingServer({ stories: STORIES });
    const { app } = await openApp(server);
    await screen.findByTestId("story-card-2");
    await fireEvent.press(await openDetail("2", app));
    await waitFor(() =>
      expect(screen.getByTestId("detail-bookmark-2")).toHaveAccessibleName("Remove from Saved"),
    );
    expect(screen.getByLabelText("Status: Saved")).toBeTruthy();
    await act(() => router.back());

    expect(feedCard("2").getByLabelText("Status: Saved")).toBeTruthy();
    await openSaved();
    expect(await screen.findByTestId("saved-card-2")).toBeTruthy();
  });

  it("reflects a removal from Saved in the Feed and detail", async () => {
    const server = readingServer({ stories: STORIES, saved: { reader: ["1"] } });
    const { app } = await openApp(server);
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    await openDetail("1", app);
    await act(() => router.back());

    await openSaved();
    await fireEvent.press(await screen.findByTestId("saved-bookmark-1"));
    await waitFor(() => expect(screen.queryByTestId("saved-card-1")).toBeNull());
    expect(app.pathname()).toBe("/saved");

    await openFeedTab();
    expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull();
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Save");
    const detail = await openDetail("1", app);
    expect(detail).toHaveAccessibleName("Save");
    expect(server.savedIds("reader")).toEqual([]);
  });

  it("reflects a removal from the Feed in Saved, and from detail everywhere", async () => {
    const server = readingServer({ stories: STORIES, saved: { reader: ["1", "2"] } });
    const { app } = await openApp(server);
    await openSaved();
    expect(await screen.findByTestId("saved-card-1")).toBeTruthy();
    await openFeedTab();

    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull());
    await openSaved();
    await waitFor(() => expect(screen.queryByTestId("saved-card-1")).toBeNull());
    expect(screen.getByTestId("saved-card-2")).toBeTruthy();

    await fireEvent.press(screen.getByTestId("saved-open-2"));
    await waitFor(() => expect(app.pathname()).toBe("/stories/2"));
    await fireEvent.press(await screen.findByTestId("detail-bookmark-2"));
    await waitFor(() =>
      expect(screen.getByTestId("detail-bookmark-2")).toHaveAccessibleName("Save"),
    );
    // The reading route is unchanged by the removal.
    expect(app.pathname()).toBe("/stories/2");
    await act(() => router.back());
    await waitFor(() => expect(screen.getByTestId("saved-empty")).toBeTruthy());
    await openFeedTab();
    expect(feedCard("2").queryByLabelText("Status: Saved")).toBeNull();
    expect(bookmarkWrites(server)).toEqual(["DELETE 1 reader", "DELETE 2 reader"]);
  });
});

describe("bookmark write coordination", () => {
  it("serializes rapid taps into one request and shows an accessible busy state", async () => {
    const server = readingServer({ stories: STORIES });
    const { app } = await openApp(server);
    const gate = server.hold("PUT", /^\/api\/bookmarks\/1$/);
    const button = await screen.findByTestId("story-bookmark-1");

    await fireEvent.press(button);
    await fireEvent.press(button);
    await fireEvent.press(button);
    await waitFor(() => expect(screen.getByTestId("story-bookmark-1")).toBeBusy());
    expect(screen.getByTestId("story-bookmark-1")).toBeDisabled();
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Saving…");

    // The same Story is busy on every surface; another Story stays writable.
    const detail = await openDetail("1", app);
    expect(detail).toBeBusy();
    await fireEvent.press(detail);
    await act(() => router.back());
    await fireEvent.press(screen.getByTestId("story-bookmark-2"));
    await waitFor(() => expect(feedCard("2").getByLabelText("Status: Saved")).toBeTruthy());

    await act(async () => gate.resolve());
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(screen.getByTestId("story-bookmark-1")).not.toBeBusy();
    expect(bookmarkWrites(server)).toEqual(["PUT 1 reader", "PUT 2 reader"]);
  });

  it("never claims success when the server rejects the write", async () => {
    const server = readingServer({ stories: STORIES });
    const { runtime } = await openApp(server);
    server.fail("PUT", /^\/api\/bookmarks\/1$/, { kind: "status", status: 503 });

    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    expect(await screen.findByText("This Story was not saved. Try again.")).toBeTruthy();
    expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull();
    // The ambiguous 503 was reconciled by reading the Story again.
    expect(runtime.productCalls.map((call) => `${call.method} ${call.path}`)).toContain(
      "GET /api/stories/1",
    );

    const retry = screen.getByTestId("story-bookmark-1");
    expect(retry).toHaveAccessibleName("Try saving again");
    await fireEvent.press(retry);
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(screen.queryByText("This Story was not saved. Try again.")).toBeNull();
    expect(server.savedIds("reader")).toEqual(["1"]);
  });

  it("reports a rate limit without reading or retrying on its own", async () => {
    const server = readingServer({ stories: STORIES });
    const { runtime } = await openApp(server);
    server.fail("PUT", /^\/api\/bookmarks\/1$/, { kind: "status", status: 429 });
    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    expect(await screen.findByText(/Too many changes right now/)).toBeTruthy();
    expect(runtime.productCalls.filter((call) => call.path === "/api/stories/1")).toEqual([]);
    expect(bookmarkWrites(server)).toEqual(["PUT 1 reader"]);
  });

  it("explains that an archived Story cannot be saved and shows its unavailability", async () => {
    const server = readingServer({ stories: STORIES });
    const { app } = await openApp(server);
    await screen.findByTestId("story-card-1");
    await openDetail("1", app);
    server.archive("1");
    await fireEvent.press(screen.getByTestId("detail-bookmark-1"));
    expect(await screen.findByText("Story unavailable")).toBeTruthy();
    expect(app.pathname()).toBe("/stories/1");
    expect(server.savedIds("reader")).toEqual([]);
  });

  it("converges through revalidation when the response is lost after the server saved", async () => {
    const server = readingServer({ stories: STORIES });
    await openApp(server);
    server.fail("PUT", /^\/api\/bookmarks\/1$/, { kind: "network", commit: true });

    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    // The Saved label appears only after a fresh server read confirmed it.
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(screen.queryByText(/Try again/)).toBeNull();
    await openSaved();
    expect(await screen.findByTestId("saved-card-1")).toBeTruthy();

    // A deliberate repeat of the same PUT is harmless: still one Bookmark.
    await openFeedTab();
    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull());
    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(server.savedIds("reader")).toEqual(["1"]);
  });

  it("asks for an explicit retry when neither the write nor the check can be confirmed", async () => {
    const server = readingServer({ stories: STORIES });
    await openApp(server);
    server.fail("PUT", /^\/api\/bookmarks\/1$/, { kind: "network", commit: true });
    server.fail("GET", /^\/api\/stories\/1$/, { kind: "network", commit: false });

    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    expect(
      await screen.findByText("Pulso could not confirm whether this Story was saved. Try again."),
    ).toBeTruthy();
    expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull();

    // The retry repeats the intended PUT; the server already holds the Bookmark.
    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    expect(bookmarkWrites(server)).toEqual(["PUT 1 reader", "PUT 1 reader"]);
    expect(server.savedIds("reader")).toEqual(["1"]);
  });

  it("repeats an unconfirmed DELETE safely", async () => {
    const server = readingServer({ stories: STORIES, saved: { reader: ["1"] } });
    await openApp(server);
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    server.fail("DELETE", /^\/api\/bookmarks\/1$/, { kind: "network", commit: true });
    server.fail("GET", /^\/api\/stories\/1$/, { kind: "status", status: 502 });

    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    expect(
      await screen.findByText(
        "Pulso could not confirm whether this Story was removed from Saved. Try again.",
      ),
    ).toBeTruthy();
    // Unconfirmed: the card keeps its last server-read state instead of claiming removal.
    expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy();

    const retry = screen.getByTestId("story-bookmark-1");
    expect(retry).toHaveAccessibleName("Try removing again");
    await fireEvent.press(retry);
    await waitFor(() => expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull());
    expect(bookmarkWrites(server)).toEqual(["DELETE 1 reader", "DELETE 1 reader"]);
    expect(server.savedIds("reader")).toEqual([]);
    await openSaved();
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
  });
});

describe("bookmark writes racing an older Feed refresh", () => {
  async function staleRefresh(server: ReadingServer) {
    const gate = server.hold("GET", /^\/api\/feed$/, { snapshot: true });
    await fireEvent(refreshControl(), "refresh");
    return gate;
  }

  it("keeps a save confirmed after the refresh started", async () => {
    const server = readingServer({ stories: STORIES });
    const { runtime } = await openApp(server);
    await screen.findByTestId("story-card-2");
    // Another device saves Story 2 before the refresh reads: the refresh must show it.
    await server.elsewhere("PUT", "2");

    const gate = await staleRefresh(server);
    await waitFor(() =>
      expect(runtime.productCalls.filter((call) => call.path === "/api/feed")).toHaveLength(2),
    );
    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());

    // The refresh answered before the PUT committed: Story 1 unsaved, Story 2 saved.
    await act(async () => gate.resolve());
    await waitFor(() => expect(feedCard("2").getByLabelText("Status: Saved")).toBeTruthy());
    expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy();
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Remove from Saved");
    expect(server.savedIds("reader").sort()).toEqual(["1", "2"]);
  });

  it("keeps a removal confirmed after the refresh started", async () => {
    const server = readingServer({ stories: STORIES, saved: { reader: ["1", "2"] } });
    const { runtime } = await openApp(server);
    await waitFor(() => expect(feedCard("2").getByLabelText("Status: Saved")).toBeTruthy());
    // Another device removes Story 2 before the refresh reads: the refresh must show that.
    await server.elsewhere("DELETE", "2");

    const gate = await staleRefresh(server);
    await waitFor(() =>
      expect(runtime.productCalls.filter((call) => call.path === "/api/feed")).toHaveLength(2),
    );
    await fireEvent.press(screen.getByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull());

    // The refresh answered before the DELETE committed: Story 1 still saved.
    await act(async () => gate.resolve());
    await waitFor(() => expect(feedCard("2").queryByLabelText("Status: Saved")).toBeNull());
    expect(feedCard("1").queryByLabelText("Status: Saved")).toBeNull();
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Save");
    expect(server.savedIds("reader")).toEqual([]);
  });
});

describe("Saved list", () => {
  const many = Array.from({ length: 5 }, (_, index) => storyDetail(String(index + 10)));

  it("follows the server's pages and shows the end", async () => {
    const server = readingServer({
      stories: many,
      savedPageSize: 2,
      saved: { reader: ["10", "11", "12", "13", "14"] },
    });
    const { runtime } = await openApp(server, "/saved");
    expect(await screen.findByTestId("saved-card-10")).toBeTruthy();
    expect(screen.queryByTestId("saved-card-12")).toBeNull();

    await fireEvent(screen.getByTestId("saved-list"), "endReached", { distanceFromEnd: 0 });
    expect(await screen.findByTestId("saved-card-12")).toBeTruthy();
    await fireEvent(screen.getByTestId("saved-list"), "endReached", { distanceFromEnd: 0 });
    expect(await screen.findByTestId("saved-card-14")).toBeTruthy();
    expect(screen.getByTestId("saved-footer-end")).toBeTruthy();
    expect(
      runtime.productCalls
        .filter((call) => call.path === "/api/bookmarks")
        .map((call) => call.query.get("cursor")),
    ).toEqual([null, "c2", "c4"]);
  });

  it("offers a retry after a failed next page, keeping the loaded rows", async () => {
    const server = readingServer({
      stories: many,
      savedPageSize: 2,
      saved: { reader: ["10", "11", "12"] },
    });
    await openApp(server, "/saved");
    await screen.findByTestId("saved-card-10");
    server.fail("GET", /^\/api\/bookmarks$/, { kind: "status", status: 500 });
    await fireEvent(screen.getByTestId("saved-list"), "endReached", { distanceFromEnd: 0 });
    const footer = await screen.findByTestId("saved-footer-error");
    expect(screen.getByTestId("saved-card-11")).toBeTruthy();

    await fireEvent.press(within(footer).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("saved-card-12")).toBeTruthy();
    expect(screen.getByTestId("saved-footer-end")).toBeTruthy();
  });

  it("refreshes explicitly and keeps earlier rows when a refresh fails", async () => {
    const server = readingServer({ stories: many, saved: { reader: ["10"] } });
    await openApp(server, "/saved");
    await screen.findByTestId("saved-card-10");

    server.fail("GET", /^\/api\/bookmarks$/, { kind: "network", commit: false });
    await fireEvent(refreshControl(), "refresh");
    expect(await screen.findByTestId("saved-refresh-error")).toBeTruthy();
    expect(screen.getByTestId("saved-card-10")).toBeTruthy();

    // Saved on another device: the next successful refresh shows it first.
    await server.handler({
      method: "PUT",
      path: "/api/bookmarks/11",
      query: new URLSearchParams(),
      body: undefined,
      user: "reader",
    });
    await fireEvent.press(screen.getByRole("button", { name: "Try refreshing again" }));
    expect(await screen.findByTestId("saved-card-11")).toBeTruthy();
    expect(screen.queryByTestId("saved-refresh-error")).toBeNull();
    expect(screen.getByTestId("saved-card-10")).toBeTruthy();
  });

  it("shows an explicit empty state, also after removing the final row", async () => {
    const server = readingServer({ stories: many, saved: { reader: ["10"] } });
    await openApp(server, "/saved");
    await fireEvent.press(await screen.findByTestId("saved-bookmark-10"));
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
    expect(screen.getByTestId("saved-empty")).toBeTruthy();
  });

  it("shows an empty state for an account with nothing saved", async () => {
    await openApp(readingServer({ stories: many }), "/saved");
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
  });

  it("labels preparing and updating Stories honestly", async () => {
    const preparing = {
      ...storyDetail("30"),
      content_state: "PREPARING",
      synthesis_id: null,
      synthesized_at: null,
      title: "Story being prepared",
      elements: [],
      citations: {},
    };
    const updating = {
      ...storyDetail("31", { title: "Bridge repairs" }),
      content_state: "UPDATING",
    };
    const server = readingServer({
      stories: [preparing, updating] as ReturnType<typeof storyDetail>[],
      saved: { reader: ["30", "31"] },
    });
    await openApp(server, "/saved");
    const prepared = within(await screen.findByTestId("saved-card-30"));
    expect(prepared.getByLabelText("Status: Preparing")).toBeTruthy();
    expect(prepared.getByText("Story being prepared")).toBeTruthy();
    const updated = within(screen.getByTestId("saved-card-31"));
    expect(updated.getByLabelText("Status: Updating")).toBeTruthy();
    expect(updated.getByText("Bridge repairs")).toBeTruthy();
  });

  it("keeps an unavailable Story as a removable tombstone without obsolete facts", async () => {
    const server = readingServer({
      stories: [storyDetail("40", { title: "Replacement Story" })],
      unavailable: ["45"],
      saved: { reader: ["45"] },
    });
    const { app } = await openApp(server, "/saved");
    const tombstone = within(await screen.findByTestId("saved-unavailable-45"));
    expect(tombstone.getByLabelText("Status: Unavailable")).toBeTruthy();
    expect(tombstone.getByText("This Story is no longer available")).toBeTruthy();
    expect(tombstone.queryByText(/summary\./)).toBeNull();
    expect(screen.queryByText("Replacement Story")).toBeNull();

    await fireEvent.press(tombstone.getByRole("button", { name: "Remove from Saved" }));
    await waitFor(() => expect(screen.queryByTestId("saved-unavailable-45")).toBeNull());
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
    expect(app.pathname()).toBe("/saved");
    // Only the tombstone's own DELETE: nothing was re-saved or moved to another Story.
    expect(bookmarkWrites(server)).toEqual(["DELETE 45 reader"]);
    expect(server.savedIds("reader")).toEqual([]);
  });

  it("does not report FeedImpressions while Saved is shown", async () => {
    const server = readingServer({ stories: many, saved: { reader: ["10", "11"] } });
    await openApp(server, "/saved");
    await screen.findByTestId("saved-card-10");
    jest.useFakeTimers();
    try {
      await act(async () => {
        jest.advanceTimersByTime(60_000);
      });
    } finally {
      jest.useRealTimers();
    }
    expect(server.impressions).toEqual([]);
  });
});

describe("Saved persistence and account isolation", () => {
  it("reloads Saved from the server after a native restart", async () => {
    const server = readingServer({ stories: STORIES });
    const store = createWebMemoryRefreshTokenStore();
    await store.write("refresh-reader");
    const first = testSession(store, server.handler);
    await renderReadingApp(first);
    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    await screen.unmount();

    // A new process: fresh runtime and empty cache, only the stored credential survives.
    const restarted = testSession(store, server.handler);
    await renderReadingApp(restarted, "/saved");
    expect(await screen.findByTestId("saved-card-1")).toBeTruthy();
    expect(restarted.productCalls.map((call) => call.path)).toContain("/api/bookmarks");
  });

  it("never shows one account's Saved rows or badges to the next account", async () => {
    const server = readingServer({ stories: STORIES, saved: { reader: ["1"] } });
    const { runtime } = await openApp(server);
    await waitFor(() => expect(feedCard("1").getByLabelText("Status: Saved")).toBeTruthy());
    await openSaved();
    await screen.findByTestId("saved-card-1");

    await act(() => runtime.controller.logout());
    await fireEvent.changeText(await screen.findByLabelText("Username"), "other");
    await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS.other.password);
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByTestId("story-card-1");
    expect(screen.queryByLabelText("Status: Saved")).toBeNull();
    await openSaved();
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
    expect(runtime.queryClient.getQueryData(queryKeys.bookmarks("1"))).toBeUndefined();
  });

  it("drops a pending write when the account changes and never replays it", async () => {
    const server = readingServer({ stories: STORIES });
    const { runtime } = await openApp(server);
    const gate = server.hold("PUT", /^\/api\/bookmarks\/1$/);
    await fireEvent.press(await screen.findByTestId("story-bookmark-1"));
    await waitFor(() => expect(screen.getByTestId("story-bookmark-1")).toBeBusy());

    await act(() => runtime.controller.logout());
    expect(runtime.queryClient.getMutationCache().getAll()).toEqual([]);
    await fireEvent.changeText(await screen.findByLabelText("Username"), "other");
    await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS.other.password);
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByTestId("story-card-1");

    // Reader's write lands now: it commits for reader only and touches no cache.
    await act(async () => gate.resolve());
    await act(async () => {});
    expect(server.savedIds("reader")).toEqual(["1"]);
    expect(server.savedIds("other")).toEqual([]);
    expect(screen.queryByLabelText("Status: Saved")).toBeNull();
    expect(screen.getByTestId("story-bookmark-1")).toHaveAccessibleName("Save");
    expect(screen.getByTestId("story-bookmark-1")).not.toBeBusy();
    expect(bookmarkWrites(server)).toEqual(["PUT 1 reader"]);
    expect(runtime.queryClient.getQueryData(queryKeys.feed("1"))).toBeUndefined();
    await openSaved();
    expect(await screen.findByText("No saved Stories")).toBeTruthy();
  });
});
