import type { QueryClient } from "@tanstack/react-query";

import type { StoryCard } from "@/api/types";
import { queryKeys } from "@/server-state/query";

/**
 * Server-confirmed Bookmark states of this account, each stamped with the
 * order in which it was confirmed. A read response only describes the moment
 * its request was answered, so a Feed page or Story detail requested before a
 * confirmation may carry the older flag; `withConfirmedBookmarks` lets the
 * confirmation win for those Stories only. Requests started later carry newer
 * server state and are left untouched.
 */
interface Confirmations {
  stories: Record<string, { bookmarked: boolean; sequence: number }>;
}

// Monotonic across accounts and cache garbage collection, so a request's mark
// can never compare as newer than a later confirmation. The entry itself may be
// collected once unused for the cache's gcTime: every request that could still
// be older than a confirmation times out long before that (15 s).
let sequence = 0;

/** Taken when a read request starts. */
export function confirmationMark(): number {
  return sequence;
}

/**
 * Kept in the account's own query-cache entry, so session boundaries clear
 * it with the rest of that account's server state. It is not a Bookmark list:
 * it only holds answers the server already gave to this device's writes.
 */
export function recordConfirmation(
  client: QueryClient,
  accountId: string,
  storyId: string,
  bookmarked: boolean,
): void {
  sequence += 1;
  const confirmed = { bookmarked, sequence };
  client.setQueryData<Confirmations>(queryKeys.bookmarkConfirmations(accountId), (current) => ({
    stories: { ...current?.stories, [storyId]: confirmed },
  }));
}

function confirmedSince(client: QueryClient, accountId: string, mark: number) {
  const stories =
    client.getQueryData<Confirmations>(queryKeys.bookmarkConfirmations(accountId))?.stories ?? {};
  return new Map(
    Object.entries(stories)
      .filter(([, confirmed]) => confirmed.sequence > mark)
      .map(([storyId, confirmed]) => [storyId, confirmed.bookmarked]),
  );
}

/** True when a write was confirmed after `mark`, so a response started then may be stale. */
export function hasConfirmationsSince(client: QueryClient, accountId: string, mark: number) {
  return confirmedSince(client, accountId, mark).size > 0;
}

/** Applies confirmations newer than the request to the Stories it returned; others are untouched. */
export function withConfirmedBookmarks<T extends Pick<StoryCard, "id" | "viewer">>(
  client: QueryClient,
  accountId: string,
  mark: number,
  stories: T[],
): T[] {
  const newer = confirmedSince(client, accountId, mark);
  if (newer.size === 0) return stories;
  return stories.map((story) => {
    const bookmarked = newer.get(story.id);
    return bookmarked === undefined || bookmarked === story.viewer.bookmarked
      ? story
      : { ...story, viewer: { bookmarked } };
  });
}
