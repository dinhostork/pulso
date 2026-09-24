import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react-native";
import type { ReactNode } from "react";

import { createMobileApi } from "@/api/client";
import { MobileApiProvider } from "@/api/MobileApiProvider";
import { createTransport } from "@/api/transport";
import { queryKeys } from "@/server-state/query";
import { deferred, feedPage, storyCard } from "@/test-utils/stories";

import { useFeed } from "../useFeed";

function harness(reply: (cursor: string | null) => Response | Promise<Response>) {
  const cursors: (string | null)[] = [];
  const fetch = jest.fn(async (url: string | URL | Request) => {
    const cursor = new URL(String(url)).searchParams.get("cursor");
    cursors.push(cursor);
    return reply(cursor);
  });
  const api = createMobileApi(
    createTransport({
      baseUrl: "https://api.example.com",
      fetch: fetch as typeof globalThis.fetch,
      credentials: { accessToken: () => "token", sessionEpoch: () => 1 },
    }),
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: Infinity, retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MobileApiProvider api={api}>{children}</MobileApiProvider>
    </QueryClientProvider>
  );
  return { cursors, queryClient, wrapper };
}

const page = (body: unknown) => new Response(JSON.stringify(body));

describe("useFeed", () => {
  it("starts one page request for simultaneous load-more calls", async () => {
    const gate = deferred<void>();
    const h = harness(async (cursor) => {
      if (cursor === null) return page(feedPage([storyCard("3")], "c1"));
      await gate.promise;
      return page(feedPage([storyCard("1")]));
    });
    const { result } = await renderHook(() => useFeed("1"), { wrapper: h.wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      result.current.loadMore();
      result.current.loadMore();
      result.current.loadMore();
    });
    expect(h.cursors).toEqual([null, "c1"]);

    await act(async () => gate.resolve());
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(h.cursors).toEqual([null, "c1"]);
  });

  it("counts only successful explicit refreshes, not automatic refetches", async () => {
    let first = 0;
    const h = harness((cursor) => {
      if (cursor !== null) return page(feedPage([]));
      first += 1;
      return first === 3
        ? new Response(JSON.stringify({ code: "server_error", detail: "x" }), { status: 503 })
        : page(feedPage([storyCard(String(first))]));
    });
    const { result } = await renderHook(() => useFeed("1"), { wrapper: h.wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.refreshGeneration).toBe(0);

    // An automatic refetch (here: invalidation) keeps the generation.
    await act(() => h.queryClient.invalidateQueries({ queryKey: queryKeys.feed("1") }));
    await waitFor(() => expect(result.current.items[0].story.id).toBe("2"));
    expect(result.current.refreshGeneration).toBe(0);

    // A failed explicit refresh keeps it too.
    await act(async () => {
      expect(await result.current.refresh()).toBe(false);
    });
    expect(result.current.refreshStatus).toBe("failed");
    expect(result.current.refreshGeneration).toBe(0);

    await act(async () => {
      expect(await result.current.refresh()).toBe(true);
    });
    expect(result.current.refreshStatus).toBe("idle");
    expect(result.current.refreshGeneration).toBe(1);
    expect(result.current.items.map((item) => item.story.id)).toEqual(["4"]);
  });

  it("keeps only the latest of overlapping refreshes", async () => {
    const slow = deferred<void>();
    let first = 0;
    const h = harness(async () => {
      first += 1;
      if (first === 2) {
        await slow.promise;
        return page(feedPage([storyCard("20")]));
      }
      return page(feedPage([storyCard(String(first * 10))]));
    });
    const { result } = await renderHook(() => useFeed("1"), { wrapper: h.wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let older!: Promise<boolean>;
    await act(async () => {
      older = result.current.refresh();
    });
    await act(async () => {
      expect(await result.current.refresh()).toBe(true);
    });
    await act(async () => {
      slow.resolve();
      expect(await older).toBe(false);
    });
    expect(result.current.items.map((item) => item.story.id)).toEqual(["30"]);
    expect(result.current.refreshGeneration).toBe(1);
  });
});
