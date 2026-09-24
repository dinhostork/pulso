import { act, fireEvent, screen, waitFor } from "expo-router/testing-library";

import { readingProduct, renderReadingApp, TEST_USERS, testSession } from "@/test-utils/readingApp";

import { createWebMemoryRefreshTokenStore } from "../storage";

async function signIn(username: string) {
  await fireEvent.changeText(await screen.findByLabelText("Username"), username);
  await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS[username].password);
  await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => jest.useRealTimers());

describe("protected session routing", () => {
  it("sends a signed-out user to sign-in and a valid login to the Feed", async () => {
    const app = await renderReadingApp(testSession());
    expect(await screen.findByText("Sign in to Pulso")).toBeTruthy();
    expect(app.pathname()).toBe("/sign-in");

    await signIn("reader");

    expect(await screen.findByText("Signed in as reader")).toBeTruthy();
    expect(app.pathname()).toBe("/");
  });

  it("returns to a validated deep link after sign-in", async () => {
    const app = await renderReadingApp(testSession(undefined, readingProduct), "/stories/7");
    await signIn("reader");
    expect(await screen.findByText("Story 7 headline")).toBeTruthy();
    expect(app.pathname()).toBe("/stories/7");
  });

  it("restores a stored session without showing sign-in", async () => {
    const store = createWebMemoryRefreshTokenStore();
    await store.write("refresh-reader");
    await renderReadingApp(testSession(store));
    expect(await screen.findByText("Signed in as reader")).toBeTruthy();
    expect(screen.queryByText("Sign in to Pulso")).toBeNull();
  });

  it("returns to sign-in when the stored refresh token's account no longer exists", async () => {
    // The scripted auth fake answers an unknown account's refresh with 401, as the
    // backend does for a deleted account (`no_active_account`).
    const store = createWebMemoryRefreshTokenStore();
    await store.write("refresh-deleted-account");
    const runtime = testSession(store);
    const app = await renderReadingApp(runtime);

    expect(await screen.findByText("Your session has ended. Sign in again.")).toBeTruthy();
    expect(app.pathname()).toBe("/sign-in");
    expect(screen.queryByText(/could not reach the server/)).toBeNull();
    expect(await store.read()).toBeNull();
    expect(runtime.productCalls).toEqual([]);
  });

  it("does not carry account A's screen or identity into account B", async () => {
    const runtime = testSession(undefined, readingProduct);
    const { controller } = runtime;
    const app = await renderReadingApp(runtime, "/stories/7");
    await signIn("reader");
    await screen.findByText("Story 7 headline");

    await act(() => controller.logout());
    expect(await screen.findByText(/You are signed out/)).toBeTruthy();
    await signIn("other");

    expect(await screen.findByText("Signed in as other")).toBeTruthy();
    expect(app.pathname()).toBe("/");
    await waitFor(() => expect(screen.queryByText("Story 7 headline")).toBeNull());
  });
});
