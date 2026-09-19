import { createMobileApi } from "../client";
import type { Transport, TransportRequest } from "../transport";

describe("typed API helpers", () => {
  it("constructs only relative product routes and forwards cancellation", async () => {
    const requests: TransportRequest<unknown>[] = [];
    const transport: Transport = {
      request: jest.fn(async <T>(request: TransportRequest<T>) => {
        requests.push(request as TransportRequest<unknown>);
        return undefined as T;
      }),
    };
    const api = createMobileApi(transport);
    const controller = new AbortController();

    await api.feed({ cursor: "signed:value", limit: 20, signal: controller.signal });
    await api.story("9007199254740993", controller.signal);
    await api.sources("42", { synthesisId: "77", signal: controller.signal });
    await api.bookmarks({ limit: 10, signal: controller.signal });
    await api.saveBookmark("42", controller.signal);
    await api.removeBookmark("42", controller.signal);
    await api.reportFeedImpressions([], controller.signal);

    expect(requests.map((request) => request.path)).toEqual([
      "/api/feed?cursor=signed%3Avalue&limit=20",
      "/api/stories/9007199254740993",
      "/api/stories/42/sources?synthesis_id=77",
      "/api/bookmarks?limit=10",
      "/api/bookmarks/42",
      "/api/bookmarks/42",
      "/api/feed-impressions",
    ]);
    expect(requests.every((request) => request.signal === controller.signal)).toBe(true);
    expect(requests[4].body).toBeUndefined();
  });

  it("rejects Number-like or foreign identifiers before transport", () => {
    const transport: Transport = { request: jest.fn() };
    const api = createMobileApi(transport);
    expect(() => api.story("9007199254740992.0")).toThrow(TypeError);
    expect(() => api.story("https://foreign.example/1")).toThrow(TypeError);
    expect(transport.request).not.toHaveBeenCalled();
  });
});
