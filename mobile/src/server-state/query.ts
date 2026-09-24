import { QueryClient, type DefaultError, type InfiniteData } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";
import type { Page } from "@/api/types";

export const MAX_FEED_PAGES = 10;

function accountScope(accountId: string): string {
  if (!/^[1-9]\d*$/.test(accountId)) {
    throw new TypeError("accountId must be a decimal-string ID");
  }
  return accountId;
}

function productId(value: string, name: string): string {
  if (!/^[1-9]\d*$/.test(value)) throw new TypeError(`${name} must be a decimal-string ID`);
  return value;
}

export const queryKeys = {
  account: (accountId: string) => ["account", accountScope(accountId)] as const,
  feed: (accountId: string) => [...queryKeys.account(accountId), "feed"] as const,
  story: (accountId: string, storyId: string) =>
    [...queryKeys.account(accountId), "story", productId(storyId, "storyId")] as const,
  sources: (accountId: string, storyId: string, synthesisId: string | null) =>
    [
      ...queryKeys.account(accountId),
      "story",
      productId(storyId, "storyId"),
      "sources",
      synthesisId === null ? null : productId(synthesisId, "synthesisId"),
    ] as const,
  bookmarks: (accountId: string) => [...queryKeys.account(accountId), "bookmarks"] as const,
  /** Bookmark states this device's writes confirmed, for ordering them against older reads. */
  bookmarkConfirmations: (accountId: string) =>
    [...queryKeys.account(accountId), "bookmark-confirmations"] as const,
  /** Mutation key (not a query): every Bookmark write for one Story, from any screen. */
  bookmarkWrite: (accountId: string, storyId: string) =>
    [...queryKeys.account(accountId), "bookmark-write", productId(storyId, "storyId")] as const,
};

export function readRetry(failureCount: number, error: DefaultError): boolean {
  if (failureCount >= 2 || !(error instanceof ApiError)) return false;
  if (error.kind === "network" || error.kind === "timeout") return true;
  return error.kind === "http" && (error.status === 429 || (error.status ?? 0) >= 500);
}

export function idempotentWriteRetry(failureCount: number, error: DefaultError): boolean {
  if (failureCount >= 1 || !(error instanceof ApiError)) return false;
  return (
    error.kind === "network" ||
    error.kind === "timeout" ||
    (error.kind === "http" && (error.status ?? 0) >= 500)
  );
}

export function readRetryDelay(attempt: number, error: DefaultError): number {
  if (error instanceof ApiError && error.status === 429 && error.retryAfterSeconds !== undefined) {
    return Math.min(error.retryAfterSeconds * 1000, 30_000);
  }
  return Math.min(500 * 2 ** attempt, 5_000);
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: readRetry, retryDelay: readRetryDelay, staleTime: 30_000 },
      mutations: { retry: false },
    },
  });
}

export const queryClient = createQueryClient();

export function nextCursor<T>(page: Page<T>): string | undefined {
  return page.next_cursor ?? undefined;
}

/**
 * Drops everything cached for an account that is leaving: its queries and its
 * mutation records, so a pending or failed write can never surface (or be
 * resumed) under the next account.
 */
export async function clearAccountServerState(client: QueryClient, accountId: string) {
  const queryKey = queryKeys.account(accountId);
  const mutations = client.getMutationCache();
  for (const mutation of mutations.findAll({ mutationKey: queryKey })) mutations.remove(mutation);
  await client.cancelQueries({ queryKey });
  client.removeQueries({ queryKey });
}

export function capFeedPages<T>(
  data: InfiniteData<T, string | null>,
): InfiniteData<T, string | null> {
  if (data.pages.length <= MAX_FEED_PAGES) return data;
  return {
    pages: data.pages.slice(0, MAX_FEED_PAGES),
    pageParams: data.pageParams.slice(0, MAX_FEED_PAGES),
  };
}
