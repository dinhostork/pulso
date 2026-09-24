import { fireEvent, screen, waitFor } from "expo-router/testing-library";

import { json, renderReadingApp, storedSession } from "@/test-utils/readingApp";
import { feedPage, storyCard } from "@/test-utils/stories";

describe("Feed route", () => {
  it("renders the signed-in Feed from the real feed endpoint and offers sign-out", async () => {
    const runtime = await storedSession("reader", ({ path }) =>
      path === "/api/feed"
        ? json(feedPage([storyCard("7", { title: "Transit plan published" })]))
        : json({}, 404),
    );
    await renderReadingApp(runtime);

    expect(await screen.findByRole("header", { name: "Feed" })).toBeTruthy();
    expect(await screen.findByText("Transit plan published")).toBeTruthy();
    expect(screen.getByText("Signed in as reader")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(runtime.controller.snapshot().status).toBe("signed_out"));
  });
});
