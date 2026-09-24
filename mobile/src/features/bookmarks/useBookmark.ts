import {
  useMutation,
  useMutationState,
  useQueryClient,
  type MutationState,
  type QueryClient,
} from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import type { MobileApi } from "@/api/client";
import { ApiError } from "@/api/errors";
import { useMobileApi } from "@/api/MobileApiProvider";
import { queryKeys } from "@/server-state/query";
import { useSession } from "@/session/SessionProvider";

import { applyConfirmedBookmark } from "./bookmarkCache";

/** A set operation, never a toggle: PUT and DELETE are both idempotent. */
export type BookmarkIntent = "save" | "remove";

/**
 * Why a write did not reach its intended state.
 * `unconfirmed`: the response was lost or broken and a fresh read could not
 * tell whether the server applied it. `not_applied`: the server rejected it
 * or a fresh read shows it did not apply. `rate_limited`, `unavailable` and
 * `not_found` are definitive server answers. `session_changed`: the account
 * changed mid-write, so nothing is reported to the next account.
 */
export type BookmarkFailure =
  "unconfirmed" | "not_applied" | "rate_limited" | "unavailable" | "not_found" | "session_changed";

export class BookmarkWriteError extends Error {
  constructor(
    readonly intent: BookmarkIntent,
    readonly failure: BookmarkFailure,
    options?: { cause?: unknown },
  ) {
    super(`bookmark ${intent} ${failure}`, options);
    this.name = "BookmarkWriteError";
  }

  /** Whether repeating the same idempotent operation can help. */
  get retryable(): boolean {
    return this.failure !== "unavailable" && this.failure !== "not_found";
  }
}

export interface BookmarkOutcome {
  bookmarked: boolean;
  /** True when a lost/broken response was resolved by re-reading the server. */
  reconciled: boolean;
}

/**
 * The response did not prove the write's outcome either way: the server may
 * have committed it before the connection, the gateway or the body failed.
 */
function isAmbiguous(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return (
    error.kind === "network" ||
    error.kind === "timeout" ||
    error.kind === "malformed_json" ||
    error.kind === "malformed_dto" ||
    (error.kind === "http" && (error.status ?? 0) >= 500)
  );
}

function definitiveFailure(error: unknown): BookmarkFailure {
  if (error instanceof ApiError) {
    if (error.kind === "stale_session") return "session_changed";
    if (error.status === 429) return "rate_limited";
    if (error.status === 410) return "unavailable";
    if (error.status === 404) return "not_found";
  }
  return "not_applied";
}

interface WriteContext {
  api: MobileApi;
  client: QueryClient;
  accountId: string;
  storyId: string;
  /** True while the session that started the write is still the current one. */
  sameSession: () => boolean;
}

/**
 * Reads this account's server state for the Story again. Null when the read
 * fails or the Story can no longer be read (a tombstone): the Saved list is
 * then refetched so it shows whatever the server holds.
 */
async function serverBookmarkState(context: WriteContext): Promise<boolean | null> {
  const { api, client, accountId, storyId } = context;
  try {
    const detail = await client.fetchQuery({
      queryKey: queryKeys.story(accountId, storyId),
      queryFn: ({ signal }) => api.story(storyId, signal),
      staleTime: 0,
    });
    return detail.viewer.bookmarked;
  } catch {
    if (context.sameSession()) {
      void client.invalidateQueries({ queryKey: queryKeys.bookmarks(accountId), exact: true });
    }
    return null;
  }
}

/**
 * One Bookmark write and, if its response is ambiguous, its reconciliation.
 * Success is only ever a server answer: the write's own 2xx, or a fresh read
 * showing the intended state. Nothing is retried automatically; the reader
 * repeats the same PUT/DELETE explicitly, which is safe because both are
 * idempotent.
 */
export async function writeBookmark(
  context: WriteContext,
  intent: BookmarkIntent,
): Promise<BookmarkOutcome> {
  const { api, storyId } = context;
  try {
    if (intent === "save") await api.saveBookmark(storyId);
    else await api.removeBookmark(storyId);
    return { bookmarked: intent === "save", reconciled: false };
  } catch (error) {
    if (!context.sameSession()) {
      throw new BookmarkWriteError(intent, "session_changed", { cause: error });
    }
    if (!isAmbiguous(error)) {
      throw new BookmarkWriteError(intent, definitiveFailure(error), { cause: error });
    }
    const confirmed = await serverBookmarkState(context);
    if (!context.sameSession()) {
      throw new BookmarkWriteError(intent, "session_changed", { cause: error });
    }
    if (confirmed === (intent === "save")) return { bookmarked: confirmed, reconciled: true };
    throw new BookmarkWriteError(intent, confirmed === null ? "unconfirmed" : "not_applied", {
      cause: error,
    });
  }
}

export interface BookmarkControl {
  /** A write for this Story is in flight from any screen; further writes are refused. */
  pending: boolean;
  pendingIntent: BookmarkIntent | null;
  /** The latest attempt's failure, shared by every screen showing this Story. */
  failure: BookmarkWriteError | null;
  save(): void;
  remove(): void;
  /** Repeats the failed attempt's own intent (not a toggle of the shown state). */
  retry(): void;
}

type WriteState = MutationState<BookmarkOutcome, BookmarkWriteError, BookmarkIntent>;

/**
 * The single Bookmark mutation used by Feed, Story detail and Saved. Writes
 * are keyed per account and Story: a second write for the same Story is
 * refused while one is pending (and the mutation scope queues any that slip
 * through), while other Stories stay writable. On success every surface's
 * cache receives the server-confirmed state; after an account change nothing
 * is written to any cache.
 */
export function useBookmark(accountId: string, storyId: string): BookmarkControl {
  const api = useMobileApi();
  const client = useQueryClient();
  const { controller } = useSession();
  const mutationKey = useMemo(
    () => queryKeys.bookmarkWrite(accountId, storyId),
    [accountId, storyId],
  );

  const { mutate } = useMutation<BookmarkOutcome, BookmarkWriteError, BookmarkIntent, number>({
    mutationKey,
    scope: { id: mutationKey.join(":") },
    // Never paused offline and resumed later, possibly under another account.
    networkMode: "always",
    retry: false,
    // The session epoch the write belongs to; it is checked again before any cache update.
    onMutate: () => controller.sessionEpoch(),
    mutationFn: (intent) => {
      const epoch = controller.sessionEpoch();
      return writeBookmark(
        { api, client, accountId, storyId, sameSession: () => controller.sessionEpoch() === epoch },
        intent,
      );
    },
    onSuccess: (outcome, _intent, epoch) => {
      if (controller.sessionEpoch() !== epoch) return;
      applyConfirmedBookmark(client, accountId, storyId, outcome.bookmarked);
    },
    onError: (error, _intent, epoch) => {
      if (controller.sessionEpoch() !== epoch) return;
      // The Story left the readable set: show that server state instead of a stale Story.
      if (error.failure === "unavailable" || error.failure === "not_found") {
        void client.invalidateQueries({
          queryKey: queryKeys.story(accountId, storyId),
          exact: true,
        });
        void client.invalidateQueries({ queryKey: queryKeys.bookmarks(accountId), exact: true });
      }
    },
  });

  const attempts = useMutationState<WriteState>({
    filters: { mutationKey, exact: true },
    select: (mutation) => mutation.state as WriteState,
  });
  const latest = attempts.at(-1);
  const pendingAttempt = attempts.find((attempt) => attempt.status === "pending");

  const write = useCallback(
    (intent: BookmarkIntent) => {
      // Synchronous guard: two taps in one frame see the first write as pending.
      if (client.isMutating({ mutationKey, exact: true }) > 0) return;
      mutate(intent);
    },
    [client, mutate, mutationKey],
  );

  const failure = latest?.status === "error" ? latest.error : null;
  return {
    pending: pendingAttempt !== undefined,
    pendingIntent: pendingAttempt?.variables ?? null,
    failure,
    save: () => write("save"),
    remove: () => write("remove"),
    retry: () => {
      if (failure) write(failure.intent);
    },
  };
}
