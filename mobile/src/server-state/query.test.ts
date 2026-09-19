import { ApiError } from "@/api/errors";

import {
  MAX_FEED_PAGES,
  capFeedPages,
  clearAccountServerState,
  createQueryClient,
  idempotentWriteRetry,
  nextCursor,
  queryKeys,
  readRetryDelay,
  readRetry,
} from "./query";

describe("account-scoped server state", () => {
  it("never shares viewer-decorated data between accounts", async () => {
    const client = createQueryClient();
    client.setQueryData(queryKeys.story("1", "42"), { viewer: { bookmarked: true } });
    client.setQueryData(queryKeys.story("2", "42"), { viewer: { bookmarked: false } });
    expect(client.getQueryData(queryKeys.story("1", "42"))).not.toEqual(
      client.getQueryData(queryKeys.story("2", "42")),
    );
    await clearAccountServerState(client, "1");
    expect(client.getQueryData(queryKeys.story("1", "42"))).toBeUndefined();
    expect(client.getQueryData(queryKeys.story("2", "42"))).toBeDefined();
    client.clear();
  });

  it("uses bounded, non-overlapping retry ownership", () => {
    const network = new ApiError("network", "feed.read");
    const server = new ApiError("http", "feed.read", { status: 503 });
    const validation = new ApiError("http", "feed.read", { status: 400 });
    expect(readRetry(0, network)).toBe(true);
    expect(readRetry(1, server)).toBe(true);
    expect(readRetry(2, network)).toBe(false);
    expect(readRetry(0, validation)).toBe(false);
    expect(idempotentWriteRetry(0, network)).toBe(true);
    expect(idempotentWriteRetry(1, network)).toBe(false);
    expect(
      readRetryDelay(0, new ApiError("http", "feed.read", { status: 429, retryAfterSeconds: 12 })),
    ).toBe(12_000);
  });

  it("caps in-memory feed pages at the documented session bound", () => {
    const pages = Array.from({ length: MAX_FEED_PAGES + 2 }, (_, index) => index);
    const capped = capFeedPages({ pages, pageParams: pages.map(String) });
    expect(capped.pages).toHaveLength(MAX_FEED_PAGES);
    expect(capped.pageParams).toHaveLength(MAX_FEED_PAGES);
    expect(nextCursor({ results: [], next_cursor: "next" })).toBe("next");
    expect(nextCursor({ results: [], next_cursor: null })).toBeUndefined();
  });

  it("requires stable decimal account IDs rather than credentials in keys", () => {
    expect(queryKeys.feed("9007199254740993")).toEqual(["account", "9007199254740993", "feed"]);
    expect(() => queryKeys.feed("access.token.value")).toThrow(TypeError);
    expect(() => queryKeys.story("1", "access.token.value")).toThrow(TypeError);
  });
});
