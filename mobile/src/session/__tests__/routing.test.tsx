import { act, fireEvent, screen, waitFor } from "expo-router/testing-library";

import { renderReadingApp, TEST_USERS, testSession } from "@/test-utils/readingApp";

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
    const app = await renderReadingApp(testSession(), "/stories/7");
    await signIn("reader");
    expect(await screen.findByText("Story details")).toBeTruthy();
    expect(app.pathname()).toBe("/stories/7");
  });

  it("restores a stored session without showing sign-in", async () => {
    const store = createWebMemoryRefreshTokenStore();
    await store.write("refresh-reader");
    await renderReadingApp(testSession(store));
    expect(await screen.findByText("Signed in as reader")).toBeTruthy();
    expect(screen.queryByText("Sign in to Pulso")).toBeNull();
  });

  it("does not carry account A's screen or identity into account B", async () => {
    const controller = testSession();
    const app = await renderReadingApp(controller, "/stories/7");
    await signIn("reader");
    await screen.findByText("Story details");

    await act(() => controller.logout());
    expect(await screen.findByText(/You are signed out/)).toBeTruthy();
    await signIn("other");

    expect(await screen.findByText("Signed in as other")).toBeTruthy();
    expect(app.pathname()).toBe("/");
    await waitFor(() => expect(screen.queryByText("Story details")).toBeNull());
  });
});
