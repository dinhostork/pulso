import {
  decodeBookmarkResult,
  decodeFeedImpressionResponse,
  decodeFeedPage,
  decodeSavedPage,
  decodeSourcePage,
  decodeStoryDetail,
} from "./decoders";
import type { Transport } from "./transport";
import type {
  BookmarkResult,
  FeedImpressionEvent,
  FeedImpressionResponse,
  FeedPage,
  Page,
  SavedEntry,
  SourceArticle,
  StoryDetail,
} from "./types";

function positiveId(value: string, name: string): string {
  if (!/^[1-9]\d*$/.test(value)) throw new TypeError(`${name} must be a decimal-string ID`);
  return value;
}

function pagePath(
  path: string,
  input: { cursor?: string; limit?: number; synthesisId?: string } = {},
) {
  const parameters = new URLSearchParams();
  if (input.cursor) parameters.set("cursor", input.cursor);
  if (input.limit !== undefined) parameters.set("limit", String(input.limit));
  if (input.synthesisId)
    parameters.set("synthesis_id", positiveId(input.synthesisId, "synthesisId"));
  const query = parameters.toString();
  return query ? `${path}?${query}` : path;
}

export function createMobileApi(transport: Transport) {
  return {
    feed(input: { cursor?: string; limit?: number; signal?: AbortSignal } = {}): Promise<FeedPage> {
      return transport.request({
        operation: "feed.read",
        path: pagePath("/api/feed", input),
        signal: input.signal,
        decode: decodeFeedPage,
      });
    },
    story(storyId: string, signal?: AbortSignal): Promise<StoryDetail> {
      return transport.request({
        operation: "story.read",
        path: `/api/stories/${positiveId(storyId, "storyId")}`,
        signal,
        decode: decodeStoryDetail,
      });
    },
    sources(
      storyId: string,
      input: { cursor?: string; limit?: number; synthesisId?: string; signal?: AbortSignal } = {},
    ): Promise<Page<SourceArticle>> {
      return transport.request({
        operation: "story.sources.read",
        path: pagePath(`/api/stories/${positiveId(storyId, "storyId")}/sources`, input),
        signal: input.signal,
        decode: decodeSourcePage,
      });
    },
    bookmarks(
      input: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
    ): Promise<Page<SavedEntry>> {
      return transport.request({
        operation: "bookmarks.read",
        path: pagePath("/api/bookmarks", input),
        signal: input.signal,
        decode: decodeSavedPage,
      });
    },
    saveBookmark(storyId: string, signal?: AbortSignal): Promise<BookmarkResult> {
      return transport.request({
        operation: "bookmark.save",
        path: `/api/bookmarks/${positiveId(storyId, "storyId")}`,
        method: "PUT",
        signal,
        decode: decodeBookmarkResult,
      });
    },
    removeBookmark(storyId: string, signal?: AbortSignal): Promise<void> {
      return transport.request({
        operation: "bookmark.remove",
        path: `/api/bookmarks/${positiveId(storyId, "storyId")}`,
        method: "DELETE",
        signal,
      });
    },
    reportFeedImpressions(
      events: FeedImpressionEvent[],
      signal?: AbortSignal,
    ): Promise<FeedImpressionResponse> {
      return transport.request({
        operation: "feed_impressions.deliver",
        path: "/api/feed-impressions",
        method: "POST",
        body: { events },
        signal,
        decode: decodeFeedImpressionResponse,
      });
    },
  };
}

export type MobileApi = ReturnType<typeof createMobileApi>;
