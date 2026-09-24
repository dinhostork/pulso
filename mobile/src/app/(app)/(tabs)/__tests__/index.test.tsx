import { QueryClient } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

import FeedRoute from "@/app/(app)/(tabs)/index";
import { createSessionRuntime } from "@/session/runtime";
import { SessionProvider } from "@/session/SessionProvider";
import { createWebMemoryRefreshTokenStore } from "@/session/storage";

describe("Feed route", () => {
  it("renders the signed-in route target without Story content and offers sign-out", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    const { controller } = createSessionRuntime({
      baseUrl: "https://api.example.com",
      queryClient,
      refreshTokenStore: createWebMemoryRefreshTokenStore(),
      fetch: jest.fn(async (url: string | URL | Request) =>
        String(url).endsWith("/me")
          ? new Response(JSON.stringify({ id: 1, username: "reader" }))
          : String(url).endsWith("/login")
            ? new Response(JSON.stringify({ access: "a", refresh: "r" }))
            : new Response(null, { status: 205 }),
      ) as typeof fetch,
    });
    await controller.signIn("reader", "secret");

    await render(
      <SessionProvider controller={controller}>
        <FeedRoute />
      </SessionProvider>,
    );

    expect(screen.getByRole("header", { name: "Feed" })).toBeTruthy();
    expect(screen.getByText("The Story feed is not available in this build yet.")).toBeTruthy();
    expect(screen.getByText("Signed in as reader")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(controller.snapshot().status).toBe("signed_out"));
    queryClient.clear();
  });
});
