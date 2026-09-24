/**
 * Test-only helpers for rendering the real route modules with
 * `expo-router/testing-library`. This file lives outside `src/app` (so it is
 * never a route) and outside `__tests__` (so Jest does not run it as a suite).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { renderRouter } from "expo-router/testing-library";
import type { ComponentType } from "react";

import { MobileApiProvider } from "@/api/MobileApiProvider";

import TabsLayout from "@/app/(app)/(tabs)/_layout";
import FeedRoute from "@/app/(app)/(tabs)/index";
import SavedRoute from "@/app/(app)/(tabs)/saved";
import ProtectedLayout, { unstable_settings } from "@/app/(app)/_layout";
import StoryRoute from "@/app/(app)/stories/[storyId]/index";
import StorySourcesRoute from "@/app/(app)/stories/[storyId]/sources";
import NotFoundRoute from "@/app/+not-found";
import SignInRoute from "@/app/sign-in";
import { createSessionRuntime, type SessionRuntime } from "@/session/runtime";
import { SessionProvider } from "@/session/SessionProvider";
import { createWebMemoryRefreshTokenStore, type RefreshTokenStore } from "@/session/storage";

export const TEST_USERS: Record<string, { id: number; password: string }> = {
  reader: { id: 1, password: "secret-a" },
  other: { id: 2, password: "secret-b" },
};

export function json(body: unknown, status = 200, headers?: Record<string, string>) {
  return new Response(JSON.stringify(body), { status, headers });
}

export const EMPTY_FEED = { ordering: "story_created_desc_v1", results: [], next_cursor: null };

/** A product request as the scripted server sees it, after bearer authentication. */
export interface ProductRequest {
  path: string;
  query: URLSearchParams;
  method: string;
  body: unknown;
  /** The signed-in username the bearer token belongs to, or null. */
  user: string | null;
  signal?: AbortSignal | null;
}

export type ProductHandler = (request: ProductRequest) => Response | Promise<Response>;

/** Default product API: an empty Feed and an unknown Story, never fixture content. */
const defaultProduct: ProductHandler = ({ path }) =>
  path === "/api/feed"
    ? json(EMPTY_FEED)
    : json({ code: "story_not_found", detail: "The Story was not found." }, 404);

export interface TestRuntime extends SessionRuntime {
  fetch: jest.Mock;
  /** Every product request, in order (auth endpoints excluded). */
  productCalls: ProductRequest[];
}

/**
 * A session runtime backed by a scripted fake of the auth endpoints and an
 * optional product handler. Queries use production defaults except for read
 * retries (covered by server-state tests), so failures surface immediately.
 */
export function testSession(
  store: RefreshTokenStore = createWebMemoryRefreshTokenStore(),
  product: ProductHandler = defaultProduct,
): TestRuntime {
  const productCalls: ProductRequest[] = [];
  const fetch = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const parsed = new URL(String(url));
    const path = parsed.pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    const auth = (init?.headers as Record<string, string>).Authorization ?? "";
    if (path === "/api/auth/login") {
      const user = TEST_USERS[body.username];
      if (!user || user.password !== body.password) return json({ detail: "invalid" }, 400);
      return json({ access: `access-${body.username}`, refresh: `refresh-${body.username}` });
    }
    if (path === "/api/auth/refresh") {
      const username = String(body.refresh).replace("refresh-", "");
      return TEST_USERS[username] ? json({ access: `access-${username}` }) : json({}, 401);
    }
    if (path === "/api/auth/me") {
      const username = auth.replace("Bearer access-", "");
      return json({ id: TEST_USERS[username].id, username });
    }
    if (path === "/api/auth/logout") return new Response(null, { status: 205 });
    const username = auth.replace("Bearer access-", "");
    const request: ProductRequest = {
      path,
      query: parsed.searchParams,
      method: init?.method ?? "GET",
      body: init?.body ? body : undefined,
      user: TEST_USERS[username] ? username : null,
      signal: init?.signal,
    };
    productCalls.push(request);
    return product(request);
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: Infinity, retry: false }, mutations: { retry: false } },
  });
  const runtime = createSessionRuntime({
    baseUrl: "https://api.example.com",
    fetch: fetch as typeof globalThis.fetch,
    queryClient,
    refreshTokenStore: store,
  });
  return { ...runtime, fetch, productCalls };
}

/** Restores a stored session for `username`, so the app opens straight into protected routes. */
export async function storedSession(username = "reader", product?: ProductHandler) {
  const store = createWebMemoryRefreshTokenStore();
  await store.write(`refresh-${username}`);
  return testSession(store, product);
}

/** The production route tree, with the root layout's global runtime replaced. */
export function readingRoutes(
  runtime: SessionRuntime,
  overrides: Record<string, ComponentType> = {},
) {
  return {
    _layout: () => (
      <QueryClientProvider client={runtime.queryClient}>
        <MobileApiProvider api={runtime.api}>
          <SessionProvider controller={runtime.controller}>
            <Stack screenOptions={{ headerShown: false }} />
          </SessionProvider>
        </MobileApiProvider>
      </QueryClientProvider>
    ),
    "+not-found": NotFoundRoute,
    "sign-in": SignInRoute,
    "(app)/_layout": { default: ProtectedLayout, unstable_settings },
    "(app)/(tabs)/_layout": TabsLayout,
    "(app)/(tabs)/index": FeedRoute,
    "(app)/(tabs)/saved": SavedRoute,
    "(app)/stories/[storyId]/index": StoryRoute,
    "(app)/stories/[storyId]/sources": StorySourcesRoute,
    ...overrides,
  };
}

/**
 * With RNTL 14 `render` is async, so expo-router attaches `getPathname` to the
 * returned promise; keep that object instead of the awaited render result.
 */
export async function renderReadingApp(
  runtime: SessionRuntime,
  initialUrl = "/",
  overrides?: Record<string, ComponentType>,
) {
  const rendered = renderRouter(readingRoutes(runtime, overrides), { initialUrl });
  await rendered;
  return { pathname: () => rendered.getPathname() };
}
