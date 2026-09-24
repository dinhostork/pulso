import type { MobileApi } from "@/api/client";
import { decodeStoryDetail } from "@/api/decoders";
import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/server-state/query";
import { storyDetail } from "@/test-utils/stories";

import { BookmarkWriteError, writeBookmark, type BookmarkIntent } from "../useBookmark";

function setup(write: () => Promise<unknown>, read: () => Promise<unknown>) {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity } });
  const api = {
    saveBookmark: jest.fn(write),
    removeBookmark: jest.fn(write),
    story: jest.fn(read),
  } as unknown as MobileApi;
  let sameSession = true;
  const context = {
    api,
    client,
    accountId: "1",
    storyId: "42",
    sameSession: () => sameSession,
  };
  return {
    api,
    client,
    run: (intent: BookmarkIntent) => writeBookmark(context, intent),
    endSession: () => {
      sameSession = false;
    },
  };
}

const detail = (bookmarked: boolean) => async () =>
  decodeStoryDetail(storyDetail("42", { bookmarked }));

async function failure(promise: Promise<unknown>): Promise<BookmarkWriteError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof BookmarkWriteError) return error;
    throw error;
  }
  throw new Error("expected a BookmarkWriteError");
}

describe("writeBookmark", () => {
  it("reports the server's own success without reading again", async () => {
    const { api, run } = setup(async () => undefined, detail(false));
    await expect(run("save")).resolves.toEqual({ bookmarked: true, reconciled: false });
    await expect(run("remove")).resolves.toEqual({ bookmarked: false, reconciled: false });
    expect(api.story).not.toHaveBeenCalled();
  });

  it.each([
    ["timeout", new ApiError("timeout", "bookmark.save")],
    ["network", new ApiError("network", "bookmark.save")],
    ["5xx", new ApiError("http", "bookmark.save", { status: 502 })],
    ["malformed body", new ApiError("malformed_dto", "bookmark.save", { status: 200 })],
  ])("reconciles an ambiguous %s by reading the server", async (_name, error) => {
    const committed = setup(() => Promise.reject(error), detail(true));
    await expect(committed.run("save")).resolves.toEqual({ bookmarked: true, reconciled: true });

    const notCommitted = setup(() => Promise.reject(error), detail(false));
    const result = await failure(notCommitted.run("save"));
    expect([result.failure, result.retryable]).toEqual(["not_applied", true]);

    const unreadable = setup(
      () => Promise.reject(error),
      () => Promise.reject(new ApiError("network", "story.read")),
    );
    const unknown = await failure(unreadable.run("remove"));
    expect([unknown.intent, unknown.failure, unknown.retryable]).toEqual([
      "remove",
      "unconfirmed",
      true,
    ]);
  });

  it.each([
    [400, "not_applied", true],
    [404, "not_found", false],
    [410, "unavailable", false],
    [429, "rate_limited", true],
  ])("treats HTTP %i as a definitive answer without reading", async (status, kind, retryable) => {
    const { api, run } = setup(
      () => Promise.reject(new ApiError("http", "bookmark.save", { status })),
      detail(true),
    );
    const result = await failure(run("save"));
    expect([result.failure, result.retryable]).toEqual([kind, retryable]);
    expect(api.story).not.toHaveBeenCalled();
  });

  it("reports nothing to the next account after a session change", async () => {
    const stale = setup(
      () => Promise.reject(new ApiError("stale_session", "bookmark.save")),
      detail(true),
    );
    stale.endSession();
    expect((await failure(stale.run("save"))).failure).toBe("session_changed");
    expect(stale.api.story).not.toHaveBeenCalled();

    let finishRead!: () => void;
    const midCheck = setup(
      () => Promise.reject(new ApiError("network", "bookmark.save")),
      () =>
        new Promise((resolve) => {
          finishRead = () => resolve(decodeStoryDetail(storyDetail("42", { bookmarked: true })));
        }),
    );
    const pending = midCheck.run("save");
    await Promise.resolve();
    await Promise.resolve();
    midCheck.endSession();
    finishRead();
    expect((await failure(pending)).failure).toBe("session_changed");
  });
});
