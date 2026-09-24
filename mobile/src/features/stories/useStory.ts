import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type DefaultError,
  type InfiniteData,
} from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import { useMobileApi } from "@/api/MobileApiProvider";
import type { Page, SourceArticle } from "@/api/types";
import { confirmationMark, withConfirmedBookmarks } from "@/features/bookmarks/confirmations";
import { queryKeys } from "@/server-state/query";

export function useStoryDetail(accountId: string, storyId: string) {
  const api = useMobileApi();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: queryKeys.story(accountId, storyId),
    queryFn: async ({ signal }) => {
      // A detail answered before a Bookmark write was confirmed keeps that write's state.
      const mark = confirmationMark();
      const detail = await api.story(storyId, signal);
      return withConfirmedBookmarks(queryClient, accountId, mark, [detail])[0];
    },
  });
}

function isSourceContextChange(error: unknown): boolean {
  return (
    error instanceof ApiError && error.status === 409 && error.code === "source_context_changed"
  );
}

/**
 * `restarted`: the source list changed during pagination and was reloaded
 * once automatically. `changed_again`: it changed again before the reader
 * acted, so automatic restarts stop until the reader reloads explicitly.
 */
export type SourceContextStatus = "stable" | "restarted" | "changed_again";
export type SourcePageStatus = "idle" | "loading" | "error" | "end";

type SourceData = InfiniteData<Page<SourceArticle>, string | null>;

/**
 * The current-member publication pages of one Story, bound to its synthesis
 * context. Pages from different source contexts are never merged: a 409
 * `source_context_changed` discards every loaded page and restarts from the
 * first page once; a second change shows an explicit reload instead of looping.
 */
export function useStorySources(accountId: string, storyId: string, synthesisId: string | null) {
  const api = useMobileApi();
  const queryClient = useQueryClient();
  const queryKey = useMemo(
    () => queryKeys.sources(accountId, storyId, synthesisId),
    [accountId, storyId, synthesisId],
  );
  const query = useInfiniteQuery<
    Page<SourceArticle>,
    DefaultError,
    SourceData,
    typeof queryKey,
    string | null
  >({
    queryKey,
    queryFn: ({ pageParam, signal }) =>
      api.sources(storyId, {
        cursor: pageParam ?? undefined,
        synthesisId: synthesisId ?? undefined,
        signal,
      }),
    initialPageParam: null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });
  const { hasNextPage, isFetching, isFetchNextPageError, fetchNextPage } = query;

  const automaticRestartUsed = useRef(false);
  const [contextStatus, setContextStatus] = useState<SourceContextStatus>("stable");
  const restart = useCallback(
    () => void queryClient.resetQueries({ queryKey, exact: true }),
    [queryClient, queryKey],
  );

  const pageRequest = useRef(false);
  const requestNextPage = useCallback(() => {
    pageRequest.current = true;
    void fetchNextPage({ cancelRefetch: false })
      .then((result) => {
        if (!result.isFetchNextPageError || !isSourceContextChange(result.error)) return;
        if (automaticRestartUsed.current) {
          setContextStatus("changed_again");
          return;
        }
        automaticRestartUsed.current = true;
        setContextStatus("restarted");
        restart();
      })
      .finally(() => {
        pageRequest.current = false;
      });
  }, [fetchNextPage, restart]);

  const loadMore = useCallback(() => {
    if (pageRequest.current || !hasNextPage || isFetching || isFetchNextPageError) return;
    requestNextPage();
  }, [hasNextPage, isFetching, isFetchNextPageError, requestNextPage]);

  const retryNextPage = useCallback(() => {
    if (pageRequest.current || !hasNextPage) return;
    requestNextPage();
  }, [hasNextPage, requestNextPage]);

  /** The reader's explicit reload after repeated changes; it re-arms one automatic restart. */
  const reloadAfterChange = useCallback(() => {
    automaticRestartUsed.current = false;
    setContextStatus("stable");
    restart();
  }, [restart]);

  const articles = useMemo(() => {
    const seen = new Set<string>();
    const rows: SourceArticle[] = [];
    for (const article of (query.data?.pages ?? []).flatMap((page) => page.results)) {
      if (seen.has(article.id)) continue;
      seen.add(article.id);
      rows.push(article);
    }
    return rows;
  }, [query.data]);

  const pageStatus: SourcePageStatus = query.isFetchingNextPage
    ? "loading"
    : isFetchNextPageError && !isSourceContextChange(query.error)
      ? "error"
      : hasNextPage
        ? "idle"
        : "end";

  return {
    articles,
    status: query.data !== undefined ? "ready" : query.isError ? "error" : "loading",
    contextStatus,
    pageStatus,
    retryFirstPage: () => void query.refetch(),
    loadMore,
    retryNextPage,
    reloadAfterChange,
  } as const;
}
