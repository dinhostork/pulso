import { useInfiniteQuery, useQueryClient, type DefaultError } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import { useMobileApi } from "@/api/MobileApiProvider";
import type { FeedPage } from "@/api/types";
import { confirmationMark, withConfirmedBookmarks } from "@/features/bookmarks/confirmations";
import { queryKeys } from "@/server-state/query";

import {
  atRetainedPageBound,
  feedItems,
  nextFeedCursor,
  type FeedData,
  type FeedItem,
} from "./feedPages";

export type FeedStatus = "loading" | "error" | "ready";
export type RefreshStatus = "idle" | "refreshing" | "failed";
/** `expired`: the server rejected the chain's cursor (24 h expiry); only a restart helps. */
export type NextPageStatus = "idle" | "loading" | "error" | "expired" | "end" | "bound";

export interface FeedController {
  items: FeedItem[];
  /** First load only; later failures keep `ready` and the loaded cards. */
  status: FeedStatus;
  refreshStatus: RefreshStatus;
  nextPageStatus: NextPageStatus;
  /** Increments after each successful explicit refresh; automatic refetch never changes it. */
  refreshGeneration: number;
  retryFirstLoad(): void;
  /** Called by the list's end-reached event; repeated or concurrent calls start one request. */
  loadMore(): void;
  retryNextPage(): void;
  /** Starts a new cursor chain and replaces the cards only if its first page arrives. */
  refresh(): Promise<boolean>;
}

/**
 * The Feed's single server-state chain: one account-scoped infinite query
 * whose pages follow the server cursor exactly. Automatic refetch triggers are
 * off because a new chain would reorder cards beneath the reader; freshness is
 * the reader's explicit pull-to-refresh (or restart at the page bound).
 */
export function useFeed(accountId: string): FeedController {
  const api = useMobileApi();
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => queryKeys.feed(accountId), [accountId]);
  // A page answered before a Bookmark write was confirmed must not undo that write's state.
  const readFeed = useCallback(
    async (input: { cursor?: string; signal: AbortSignal }): Promise<FeedPage> => {
      const mark = confirmationMark();
      const page = await api.feed(input);
      const results = withConfirmedBookmarks(queryClient, accountId, mark, page.results);
      return results === page.results ? page : { ...page, results };
    },
    [api, queryClient, accountId],
  );
  const query = useInfiniteQuery<FeedPage, DefaultError, FeedData, typeof queryKey, string | null>({
    queryKey,
    queryFn: ({ pageParam, signal }) => readFeed({ cursor: pageParam ?? undefined, signal }),
    initialPageParam: null,
    getNextPageParam: nextFeedCursor,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
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
    // A failed page waits for its explicit retry instead of re-firing on every scroll.
    if (pageRequest.current || !hasNextPage || isFetching || isFetchNextPageError) return;
    requestNextPage();
  }, [hasNextPage, isFetching, isFetchNextPageError, requestNextPage]);

  const retryNextPage = useCallback(() => {
    if (pageRequest.current || !hasNextPage) return;
    requestNextPage();
  }, [hasNextPage, requestNextPage]);

  const [refreshStatus, setRefreshStatus] = useState<RefreshStatus>("idle");
  const [refreshGeneration, setRefreshGeneration] = useState(0);
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
    let page: FeedPage;
    try {
      page = await readFeed({ signal: attempt.signal });
    } catch {
      if (refreshAttempt.current === attempt) {
        refreshAttempt.current = null;
        setRefreshStatus("failed");
      }
      return false;
    }
    if (refreshAttempt.current !== attempt) return false;
    // Cancelling reverts any old-chain page request, so it can never append afterwards.
    await queryClient.cancelQueries({ queryKey, exact: true });
    if (refreshAttempt.current !== attempt) return false;
    refreshAttempt.current = null;
    queryClient.setQueryData<FeedData>(queryKey, { pages: [page], pageParams: [null] });
    setRefreshStatus("idle");
    setRefreshGeneration((generation) => generation + 1);
    return true;
  }, [readFeed, queryClient, queryKey]);

  const items = useMemo(() => feedItems(data), [data]);
  const status: FeedStatus = data !== undefined ? "ready" : query.isError ? "error" : "loading";
  const nextPageStatus: NextPageStatus = atRetainedPageBound(data)
    ? "bound"
    : isFetchingNextPage
      ? "loading"
      : isFetchNextPageError
        ? query.error instanceof ApiError && query.error.code === "invalid_cursor"
          ? "expired"
          : "error"
        : hasNextPage
          ? "idle"
          : "end";

  return {
    items,
    status,
    refreshStatus,
    nextPageStatus,
    refreshGeneration,
    retryFirstLoad: () => void refetch(),
    loadMore,
    retryNextPage,
    refresh,
  };
}
