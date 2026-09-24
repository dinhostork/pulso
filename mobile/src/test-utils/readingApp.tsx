/**
 * Test-only helpers for rendering the real route modules with
 * `expo-router/testing-library`. This file lives outside `src/app` (so it is
 * never a route) and outside `__tests__` (so Jest does not run it as a suite).
 */
import { QueryClient } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { renderRouter } from "expo-router/testing-library";

import TabsLayout from "@/app/(app)/(tabs)/_layout";
import FeedRoute from "@/app/(app)/(tabs)/index";
import SavedRoute from "@/app/(app)/(tabs)/saved";
import ProtectedLayout, { unstable_settings } from "@/app/(app)/_layout";
import StoryRoute from "@/app/(app)/stories/[storyId]/index";
import StorySourcesRoute from "@/app/(app)/stories/[storyId]/sources";
import NotFoundRoute from "@/app/+not-found";
import SignInRoute from "@/app/sign-in";
import type { SessionController } from "@/session/controller";
import { createSessionRuntime } from "@/session/runtime";
import { SessionProvider } from "@/session/SessionProvider";
import { createWebMemoryRefreshTokenStore, type RefreshTokenStore } from "@/session/storage";

export const TEST_USERS: Record<string, { id: number; password: string }> = {
  reader: { id: 1, password: "secret-a" },
  other: { id: 2, password: "secret-b" },
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

/** A session controller backed by a scripted fake of the auth endpoints. */
export function testSession(
  store: RefreshTokenStore = createWebMemoryRefreshTokenStore(),
): SessionController {
  const fetch = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
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
    return new Response(null, { status: 205 });
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
  return createSessionRuntime({
    baseUrl: "https://api.example.com",
    fetch: fetch as typeof globalThis.fetch,
    queryClient,
    refreshTokenStore: store,
  }).controller;
}

/** The production route tree, with the root layout's global runtime replaced. */
export function readingRoutes(controller: SessionController) {
  return {
    _layout: () => (
      <SessionProvider controller={controller}>
        <Stack screenOptions={{ headerShown: false }} />
      </SessionProvider>
    ),
    "+not-found": NotFoundRoute,
    "sign-in": SignInRoute,
    "(app)/_layout": { default: ProtectedLayout, unstable_settings },
    "(app)/(tabs)/_layout": TabsLayout,
    "(app)/(tabs)/index": FeedRoute,
    "(app)/(tabs)/saved": SavedRoute,
    "(app)/stories/[storyId]/index": StoryRoute,
    "(app)/stories/[storyId]/sources": StorySourcesRoute,
  };
}

/**
 * With RNTL 14 `render` is async, so expo-router attaches `getPathname` to the
 * returned promise; keep that object instead of the awaited render result.
 */
export async function renderReadingApp(controller: SessionController, initialUrl = "/") {
  const rendered = renderRouter(readingRoutes(controller), { initialUrl });
  await rendered;
  return { pathname: () => rendered.getPathname() };
}
