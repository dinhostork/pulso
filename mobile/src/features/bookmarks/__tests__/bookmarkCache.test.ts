import { decodeFeedPage, decodeSavedPage, decodeStoryDetail } from "@/api/decoders";
import { createQueryClient, queryKeys } from "@/server-state/query";
import { feedPage, storyCard, storyDetail } from "@/test-utils/stories";

import {
  applyConfirmedBookmark,
  withFeedBookmark,
  withoutSavedEntry,
  type SavedData,
} from "../bookmarkCache";

function feedData() {
  return {
    pages: [
      decodeFeedPage(feedPage([storyCard("1"), storyCard("2")], "c1")),
      decodeFeedPage(feedPage([storyCard("3")])),
    ],
    pageParams: [null, "c1"],
  };
}

function savedData(): SavedData {
  const entry = (id: string) => ({
    story_id: id,
    saved_at: "2026-09-20T10:00:00Z",
    availability: "AVAILABLE",
    story: storyCard(id, { bookmarked: true }),
  });
  return {
    pages: [
      decodeSavedPage({ results: [entry("1"), entry("2")], next_cursor: "s1" }),
      decodeSavedPage({
        results: [{ story_id: "9", saved_at: "2026-09-01T10:00:00Z", availability: "UNAVAILABLE" }],
        next_cursor: null,
      }),
    ],
    pageParams: [null, "s1"],
  };
}

describe("bookmark cache helpers", () => {
  it("updates only the loaded Feed page that holds the Story", () => {
    const data = feedData();
    const next = withFeedBookmark(data, "3", true);
    expect(next.pages[0]).toBe(data.pages[0]);
    expect(next.pages[1].results[0].viewer.bookmarked).toBe(true);
    expect(withFeedBookmark(next, "3", true)).toBe(next);
    expect(withFeedBookmark(data, "404", true)).toBe(data);
  });

  it("removes a Saved row, tombstones included, and keeps every page cursor", () => {
    const data = savedData();
    const without = withoutSavedEntry(data, "2");
    expect(without.pages[0].results.map((entry) => entry.story_id)).toEqual(["1"]);
    expect(without.pages[0].next_cursor).toBe("s1");
    expect(without.pages[1]).toBe(data.pages[1]);
    expect(withoutSavedEntry(without, "9").pages[1].results).toEqual([]);
    expect(withoutSavedEntry(data, "404")).toBe(data);
  });

  it("applies a confirmed state to this account's caches only and marks Saved stale", () => {
    const client = createQueryClient();
    client.setQueryData(queryKeys.feed("1"), feedData());
    client.setQueryData(queryKeys.feed("2"), feedData());
    client.setQueryData(queryKeys.story("1", "2"), decodeStoryDetail(storyDetail("2")));
    client.setQueryData(queryKeys.bookmarks("1"), savedData());

    applyConfirmedBookmark(client, "1", "2", false);
    const feed = client.getQueryData<ReturnType<typeof feedData>>(queryKeys.feed("1"))!;
    expect(feed.pages[0].results[1].viewer.bookmarked).toBe(false);
    const saved = client.getQueryData<SavedData>(queryKeys.bookmarks("1"))!;
    expect(saved.pages[0].results.map((entry) => entry.story_id)).toEqual(["1"]);
    expect(client.getQueryState(queryKeys.bookmarks("1"))?.isInvalidated).toBe(true);

    applyConfirmedBookmark(client, "1", "2", true);
    expect(
      client.getQueryData<ReturnType<typeof decodeStoryDetail>>(queryKeys.story("1", "2"))!.viewer
        .bookmarked,
    ).toBe(true);
    // Another account's cache and never-loaded detail queries stay untouched.
    expect(client.getQueryData(queryKeys.feed("2"))).toEqual(feedData());
    expect(client.getQueryData(queryKeys.story("1", "3"))).toBeUndefined();
    client.clear();
  });
});
