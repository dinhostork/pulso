import { QueryClient } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { act, fireEvent, renderRouter, screen, waitFor } from "expo-router/testing-library";
import { Text } from "react-native";

import ProtectedLayout from "@/app/(app)/_layout";
import Index from "@/app/(app)/index";
import SignInRoute from "@/app/sign-in";

import type { SessionController } from "../controller";
import { createSessionRuntime } from "../runtime";
import { SessionProvider } from "../SessionProvider";
import { createWebMemoryRefreshTokenStore, type RefreshTokenStore } from "../storage";

const USERS: Record<string, { id: number; password: string }> = {
  reader: { id: 1, password: "secret-a" },
  other: { id: 2, password: "secret-b" },
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function runtime(store: RefreshTokenStore = createWebMemoryRefreshTokenStore()) {
  const fetch = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    const auth = (init?.headers as Record<string, string>).Authorization ?? "";
    if (path === "/api/auth/login") {
      const user = USERS[body.username];
      if (!user || user.password !== body.password) return json({ detail: "invalid" }, 400);
      return json({ access: `access-${body.username}`, refresh: `refresh-${body.username}` });
    }
    if (path === "/api/auth/refresh") {
      const username = String(body.refresh).replace("refresh-", "");
      return USERS[username] ? json({ access: `access-${username}` }) : json({}, 401);
    }
    if (path === "/api/auth/me") {
      const username = auth.replace("Bearer access-", "");
      return json({ id: USERS[username].id, username });
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

/**
 * With RNTL 14 `render` is async, so expo-router attaches `getPathname` to the
 * returned promise; keep that object instead of the awaited render result.
 */
async function renderApp(controller: SessionController, initialUrl = "/") {
  const rendered = renderRouter(
    {
      _layout: () => (
        <SessionProvider controller={controller}>
          <Stack screenOptions={{ headerShown: false }} />
        </SessionProvider>
      ),
      "(app)/_layout": ProtectedLayout,
      "(app)/index": Index,
      "(app)/stories/[id]": () => <Text>Story screen</Text>,
      "sign-in": SignInRoute,
    },
    { initialUrl },
  );
  await rendered;
  return { pathname: () => rendered.getPathname() };
}

async function signIn(username: string) {
  await fireEvent.changeText(await screen.findByLabelText("Username"), username);
  await fireEvent.changeText(screen.getByLabelText("Password"), USERS[username].password);
  await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => jest.useRealTimers());

describe("protected session routing", () => {
  it("sends a signed-out user to sign-in and a valid login to the Feed", async () => {
    const app = await renderApp(runtime());
    expect(await screen.findByText("Sign in to Pulso")).toBeTruthy();
    expect(app.pathname()).toBe("/sign-in");

    await signIn("reader");

    expect(await screen.findByText("Signed in as reader")).toBeTruthy();
    expect(app.pathname()).toBe("/");
  });

  it("returns to a validated deep link after sign-in", async () => {
    const app = await renderApp(runtime(), "/stories/7");
    await signIn("reader");
    expect(await screen.findByText("Story screen")).toBeTruthy();
    expect(app.pathname()).toBe("/stories/7");
  });

  it("restores a stored session without showing sign-in", async () => {
    const store = createWebMemoryRefreshTokenStore();
    await store.write("refresh-reader");
    await renderApp(runtime(store));
    expect(await screen.findByText("Signed in as reader")).toBeTruthy();
    expect(screen.queryByText("Sign in to Pulso")).toBeNull();
  });

  it("does not carry account A's screen or identity into account B", async () => {
    const controller = runtime();
    const app = await renderApp(controller, "/stories/7");
    await signIn("reader");
    await screen.findByText("Story screen");

    await act(() => controller.logout());
    expect(await screen.findByText(/You are signed out/)).toBeTruthy();
    await signIn("other");

    expect(await screen.findByText("Signed in as other")).toBeTruthy();
    expect(app.pathname()).toBe("/");
    await waitFor(() => expect(screen.queryByText("Story screen")).toBeNull());
  });
});
