import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import type { Page, SavedEntry, StoryDetail } from "@/api/types";
import type { FeedData } from "@/features/feed/feedPages";
import { queryKeys } from "@/server-state/query";

export type SavedData = InfiniteData<Page<SavedEntry>, string | null>;

/** Marks one Story's viewer flag in every loaded Feed page, keeping untouched pages identical. */
export function withFeedBookmark(data: FeedData, storyId: string, bookmarked: boolean): FeedData {
  let changed = false;
  const pages = data.pages.map((page) => {
    if (
      !page.results.some((story) => story.id === storyId && story.viewer.bookmarked !== bookmarked)
    )
      return page;
    changed = true;
    return {
      ...page,
      results: page.results.map((story) =>
        story.id === storyId ? { ...story, viewer: { bookmarked } } : story,
      ),
    };
  });
  return changed ? { ...data, pages } : data;
}

/**
 * Drops one Story from every loaded Saved page. Each page keeps its own
 * `next_cursor`: the server's keyset cursor names the last row it served, so it
 * stays valid after that row is deleted.
 */
export function withoutSavedEntry(data: SavedData, storyId: string): SavedData {
  let changed = false;
  const pages = data.pages.map((page) => {
    if (!page.results.some((entry) => entry.story_id === storyId)) return page;
    changed = true;
    return { ...page, results: page.results.filter((entry) => entry.story_id !== storyId) };
  });
  return changed ? { ...data, pages } : data;
}

/**
 * Applies one server-confirmed Bookmark state to the account's Feed, Story
 * detail and Saved caches, then marks Saved stale so its order and new rows
 * come from the server. Only confirmed states arrive here; nothing is inferred.
 */
export function applyConfirmedBookmark(
  client: QueryClient,
  accountId: string,
  storyId: string,
  bookmarked: boolean,
): void {
  client.setQueryData<FeedData>(queryKeys.feed(accountId), (data) =>
    data === undefined ? undefined : withFeedBookmark(data, storyId, bookmarked),
  );
  client.setQueryData<StoryDetail>(queryKeys.story(accountId, storyId), (detail) =>
    detail === undefined || detail.viewer.bookmarked === bookmarked
      ? detail
      : { ...detail, viewer: { bookmarked } },
  );
  if (!bookmarked) {
    client.setQueryData<SavedData>(queryKeys.bookmarks(accountId), (data) =>
      data === undefined ? undefined : withoutSavedEntry(data, storyId),
    );
  }
  void client.invalidateQueries({ queryKey: queryKeys.bookmarks(accountId), exact: true });
}
