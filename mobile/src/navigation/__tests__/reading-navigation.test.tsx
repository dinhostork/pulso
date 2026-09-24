import * as Linking from "expo-linking";
import { router } from "expo-router";
import { act, fireEvent, screen, waitFor } from "expo-router/testing-library";
import { BackHandler } from "react-native";

import { openPublisherUrl } from "@/navigation/external";
import { storyHref } from "@/navigation/routes";
import { createWebMemoryRefreshTokenStore } from "@/session/storage";
import { renderReadingApp, TEST_USERS, testSession } from "@/test-utils/readingApp";

// expo-router itself uses expo-linking, so only the outbound call is replaced.
jest.mock("expo-linking", () => ({
  ...jest.requireActual("expo-linking"),
  openURL: jest.fn(async () => true),
}));

/** Restore a stored session so the app opens straight into protected routes. */
async function signedIn(initialUrl = "/") {
  const store = createWebMemoryRefreshTokenStore();
  await store.write("refresh-reader");
  const controller = testSession(store);
  const app = await renderReadingApp(controller, initialUrl);
  await waitFor(() => expect(controller.snapshot().status).toBe("authenticated"));
  return { app, controller };
}

/** Android delivers the system back press to the most recent listener first. */
function captureHardwareBack() {
  type Listener = Parameters<typeof BackHandler.addEventListener>[1];
  const listeners: Listener[] = [];
  jest.spyOn(BackHandler, "addEventListener").mockImplementation((_event, listener) => {
    listeners.push(listener);
    return {
      remove: () => {
        listeners.splice(listeners.indexOf(listener), 1);
      },
    };
  });
  return async () => {
    let handled = false;
    await act(async () => {
      handled = [...listeners].reverse().some((listener) => listener({} as never) === true);
    });
    return handled;
  };
}

afterEach(() => jest.restoreAllMocks());

describe("reading navigation", () => {
  it("offers exactly the Feed and Saved tabs and no later-phase destinations", async () => {
    await signedIn();
    expect(await screen.findByRole("header", { name: "Feed" })).toBeTruthy();

    // Tabs are announced as "tab" on Android/web and as buttons on iOS.
    const tabs = screen.getAllByTestId(/^tab-/);
    expect(tabs.map((tab) => tab.props.testID)).toEqual(["tab-feed", "tab-saved"]);
    expect(screen.getByTestId("tab-feed")).toBeSelected();
    for (const later of [/pulse/i, /opinion/i, /profile/i, /explore/i, /search/i, /audio/i]) {
      expect(screen.queryByText(later)).toBeNull();
    }
    expect(screen.queryByRole("button", { name: /share/i })).toBeNull();
  });

  it("switches to Saved with sign-out still reachable, and back returns to Feed", async () => {
    const pressBack = captureHardwareBack();
    const { app, controller } = await signedIn();

    await fireEvent.press(await screen.findByTestId("tab-saved"));
    expect(await screen.findByRole("header", { name: "Saved" })).toBeTruthy();
    expect(app.pathname()).toBe("/saved");
    expect(screen.getByTestId("tab-saved")).toBeSelected();

    expect(await pressBack()).toBe(true);
    expect(await screen.findByRole("header", { name: "Feed" })).toBeTruthy();
    expect(app.pathname()).toBe("/");

    await fireEvent.press(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(controller.snapshot().status).toBe("signed_out"));
    expect(await screen.findByText("Sign in to Pulso")).toBeTruthy();
  });

  it("pushes Story and source screens above the tabs and pops back through them", async () => {
    const pressBack = captureHardwareBack();
    const { app } = await signedIn();
    await screen.findByRole("header", { name: "Feed" });

    await act(() => router.push(storyHref("12")));
    await fireEvent.press(await screen.findByRole("button", { name: "View sources" }));
    expect(
      await screen.findByText("The source list is not available in this build yet."),
    ).toBeTruthy();
    expect(app.pathname()).toBe("/stories/12/sources");

    expect(await pressBack()).toBe(true);
    await waitFor(() => expect(app.pathname()).toBe("/stories/12"));
    expect(await pressBack()).toBe(true);
    await waitFor(() => expect(app.pathname()).toBe("/"));
  });

  it("returns a cold Story deep link to Feed on back", async () => {
    const pressBack = captureHardwareBack();
    const { app } = await signedIn("/stories/7/sources");
    expect(
      await screen.findByText("The source list is not available in this build yet."),
    ).toBeTruthy();
    expect(router.canGoBack()).toBe(true);

    expect(await pressBack()).toBe(true);

    await waitFor(() => expect(app.pathname()).toBe("/"));
    expect(await screen.findByRole("header", { name: "Feed" })).toBeTruthy();
  });

  it("resumes a pending Story route after sign-in and still backs out to Feed", async () => {
    const app = await renderReadingApp(testSession(), "/stories/7");
    await fireEvent.changeText(await screen.findByLabelText("Username"), "reader");
    await fireEvent.changeText(screen.getByLabelText("Password"), TEST_USERS.reader.password);
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Story details")).toBeTruthy();
    expect(app.pathname()).toBe("/stories/7");

    await act(() => router.back());

    await waitFor(() => expect(app.pathname()).toBe("/"));
  });

  it.each(["/stories/abc", "/stories/0", "/stories/99999999999999999999/sources"])(
    "shows a recoverable state for the invalid Story route %s",
    async (path) => {
      const { app } = await signedIn(path);
      expect(await screen.findByText("This Story link is not valid.")).toBeTruthy();
      expect(app.pathname()).toBe(path);

      await fireEvent.press(screen.getByRole("button", { name: "Go to Feed" }));

      await waitFor(() => expect(app.pathname()).toBe("/"));
    },
  );

  it("does not remember an invalid or external return route while signed out", async () => {
    const controller = testSession();
    const app = await renderReadingApp(controller, "/stories/abc");
    await screen.findByText("Sign in to Pulso");
    expect(controller.consumeReturnRoute()).toBeNull();
    expect(app.pathname()).toBe("/sign-in");
  });

  it("gives unknown paths a recoverable not-found state", async () => {
    const { app } = await signedIn("/pulse");
    expect(await screen.findByText("This page does not exist in Pulso.")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Go to Feed" }));
    await waitFor(() => expect(app.pathname()).toBe("/"));
  });

  it("leaves the in-app route untouched when a publisher page is opened", async () => {
    const { app } = await signedIn("/stories/7/sources");
    await screen.findByText("The source list is not available in this build yet.");

    await act(async () => {
      expect(await openPublisherUrl("https://publisher.example/story")).toBe(true);
    });

    expect(Linking.openURL).toHaveBeenCalledWith("https://publisher.example/story");
    expect(app.pathname()).toBe("/stories/7/sources");
    expect(router.canGoBack()).toBe(true);
  });
});
