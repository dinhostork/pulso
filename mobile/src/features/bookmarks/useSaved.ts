import { useInfiniteQuery, useQueryClient, type DefaultError } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import { useMobileApi } from "@/api/MobileApiProvider";
import type { Page, SavedEntry } from "@/api/types";
import { nextCursor, queryKeys } from "@/server-state/query";

import type { SavedData } from "./bookmarkCache";

export type SavedStatus = "loading" | "error" | "ready";
export type SavedRefreshStatus = "idle" | "refreshing" | "failed";
/** `expired`: the server rejected the chain's cursor (24 h expiry); a refresh restarts it. */
export type SavedPageStatus = "idle" | "loading" | "error" | "expired" | "end";

export interface SavedController {
  /** Saved entries in server order, one per Story ID. */
  entries: SavedEntry[];
  status: SavedStatus;
  refreshStatus: SavedRefreshStatus;
  pageStatus: SavedPageStatus;
  retryFirstLoad(): void;
  loadMore(): void;
  retryNextPage(): void;
  /** Starts a new cursor chain; the loaded rows are replaced only if its first page arrives. */
  refresh(): Promise<boolean>;
}

/** Flattens the loaded pages in server order; a Story seen on an earlier page keeps its slot. */
export function savedEntries(data: SavedData | undefined): SavedEntry[] {
  if (!data) return [];
  const seen = new Set<string>();
  const entries: SavedEntry[] = [];
  for (const page of data.pages) {
    for (const entry of page.results) {
      if (seen.has(entry.story_id)) continue;
      seen.add(entry.story_id);
      entries.push(entry);
    }
  }
  return entries;
}

/**
 * The account's Saved list: one account-scoped infinite query following the
 * server's `(saved_at DESC)` cursor chain page by page. The server is the only
 * Bookmark authority, so a cold start, a remount after invalidation and an
 * explicit refresh all read it again; nothing is stored on the device.
 */
export function useSaved(accountId: string): SavedController {
  const api = useMobileApi();
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => queryKeys.bookmarks(accountId), [accountId]);
  const query = useInfiniteQuery<
    Page<SavedEntry>,
    DefaultError,
    SavedData,
    typeof queryKey,
    string | null
  >({
    queryKey,
    queryFn: ({ pageParam, signal }) => api.bookmarks({ cursor: pageParam ?? undefined, signal }),
    initialPageParam: null,
    getNextPageParam: nextCursor,
  });
  const { data, hasNextPage, isFetching, isFetchingNextPage, isFetchNextPageError } = query;
  const { fetchNextPage, refetch } = query;

  const pageRequest = useRef(false);
  const requestNextPage = useCallback(() => {
    pageRequest.current = true;
    void fetchNextPage({ cancelRefetch: false }).finally(() => {
      pageRequest.current = false;
    });
  }, [fetchNextPage]);

  const loadMore = useCallback(() => {
    if (pageRequest.current || !hasNextPage || isFetching || isFetchNextPageError) return;
    requestNextPage();
  }, [hasNextPage, isFetching, isFetchNextPageError, requestNextPage]);

  const retryNextPage = useCallback(() => {
    if (pageRequest.current || !hasNextPage) return;
    requestNextPage();
  }, [hasNextPage, requestNextPage]);

  const [refreshStatus, setRefreshStatus] = useState<SavedRefreshStatus>("idle");
  const refreshAttempt = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      const attempt = refreshAttempt.current;
      refreshAttempt.current = null;
      attempt?.abort();
    },
    [queryKey],
  );

  const refresh = useCallback(async (): Promise<boolean> => {
    refreshAttempt.current?.abort();
    const attempt = new AbortController();
    refreshAttempt.current = attempt;
    setRefreshStatus("refreshing");
    let page: Page<SavedEntry>;
    try {
      page = await api.bookmarks({ signal: attempt.signal });
    } catch {
      if (refreshAttempt.current === attempt) {
        refreshAttempt.current = null;
        setRefreshStatus("failed");
      }
      return false;
    }
    if (refreshAttempt.current !== attempt) return false;
    await queryClient.cancelQueries({ queryKey, exact: true });
    if (refreshAttempt.current !== attempt) return false;
    refreshAttempt.current = null;
    queryClient.setQueryData<SavedData>(queryKey, { pages: [page], pageParams: [null] });
    setRefreshStatus("idle");
    return true;
  }, [api, queryClient, queryKey]);

  const entries = useMemo(() => savedEntries(data), [data]);
  const status: SavedStatus = data !== undefined ? "ready" : query.isError ? "error" : "loading";
  const pageStatus: SavedPageStatus = isFetchingNextPage
    ? "loading"
    : isFetchNextPageError
      ? query.error instanceof ApiError && query.error.code === "invalid_cursor"
        ? "expired"
        : "error"
      : hasNextPage
        ? "idle"
        : "end";

  // Removing every loaded row must not strand older saved Stories behind an empty list.
  useEffect(() => {
    if (status === "ready" && entries.length === 0 && pageStatus === "idle") loadMore();
  }, [status, entries.length, pageStatus, loadMore]);

  return {
    entries,
    status,
    // A failed background refetch (after a bookmark change elsewhere) is shown like a failed refresh.
    refreshStatus: refreshStatus === "idle" && query.isRefetchError ? "failed" : refreshStatus,
    pageStatus,
    retryFirstLoad: () => void refetch(),
    loadMore,
    retryNextPage,
    refresh,
  };
}
