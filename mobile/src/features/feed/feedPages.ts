import type { InfiniteData } from "@tanstack/react-query";

import type { FeedPage, StoryCard } from "@/api/types";
import { MAX_FEED_PAGES } from "@/server-state/query";

/** One rendered Feed row: a Story and its zero-based absolute position in the rendered order. */
export interface FeedItem {
  story: StoryCard;
  position: number;
}

export type FeedData = InfiniteData<FeedPage, string | null>;

/**
 * Flattens the cursor chain in server order. A Story ID seen on an earlier
 * page keeps its first slot, so a retried or overlapping page can never render
 * a Story twice; the device never re-sorts the immutable server ordering.
 */
export function feedItems(data: FeedData | undefined): FeedItem[] {
  if (!data) return [];
  const seen = new Set<string>();
  const items: FeedItem[] = [];
  for (const page of data.pages) {
    for (const story of page.results) {
      if (seen.has(story.id)) continue;
      seen.add(story.id);
      items.push({ story, position: items.length });
    }
  }
  return items;
}

/** The next cursor of the chain, or none once the retained-page bound is reached. */
export function nextFeedCursor(lastPage: FeedPage, pages: readonly FeedPage[]): string | undefined {
  if (pages.length >= MAX_FEED_PAGES) return undefined;
  return lastPage.next_cursor ?? undefined;
}

/**
 * True when the server still has older Stories but this session already holds
 * the maximum number of pages: the Feed offers an explicit restart instead of
 * growing without bound or dropping pages from the middle of the chain.
 */
export function atRetainedPageBound(data: FeedData | undefined): boolean {
  if (!data || data.pages.length < MAX_FEED_PAGES) return false;
  return data.pages[data.pages.length - 1].next_cursor !== null;
}
