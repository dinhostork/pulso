import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, Text } from "react-native";

import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { Screen } from "@/components/Screen";
import { SourceRow } from "@/components/SourceRow";
import { EmptyState, ErrorState, LoadingState, StatusLabel } from "@/components/StatusState";
import { MIN_TOUCH_TARGET, palettes } from "@/theme";

function flat(element: { props: { style?: unknown } }): Record<string, unknown> {
  return (StyleSheet.flatten(element.props.style as never) ?? {}) as Record<string, unknown>;
}

describe("AppText", () => {
  it("exposes titles as headers and keeps dynamic text scaling", async () => {
    await render(
      <>
        <AppText variant="title">Feed</AppText>
        <AppText>Body</AppText>
      </>,
    );
    expect(screen.getByRole("header", { name: "Feed" })).toBeTruthy();
    const body = screen.getByText("Body");
    expect(body.props.accessibilityRole).toBeUndefined();
    expect(body.props.allowFontScaling).not.toBe(false);
    expect(body.props.numberOfLines).toBeUndefined();
  });

  it("renders publication strings as plain text", async () => {
    await render(<AppText>{"<b>Breaking</b> &amp; <script>x</script>"}</AppText>);
    expect(screen.getByText("<b>Breaking</b> &amp; <script>x</script>")).toBeTruthy();
  });
});

describe("Button", () => {
  it("is a labelled 44×44 target that wraps instead of fixing its height", async () => {
    const onPress = jest.fn();
    await render(<Button hint="Opens the publisher" label="Open publication" onPress={onPress} />);
    const button = screen.getByRole("button", { name: "Open publication" });
    expect(button.props.accessibilityHint).toBe("Opens the publisher");
    const style = flat(button);
    expect(style.minHeight).toBe(MIN_TOUCH_TARGET);
    expect(style.minWidth).toBe(MIN_TOUCH_TARGET);
    expect(style.height).toBeUndefined();
    expect(screen.getByText("Open publication").props.numberOfLines).toBeUndefined();

    await fireEvent.press(button);
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it("reports disabled and busy states and ignores presses", async () => {
    const onPress = jest.fn();
    await render(
      <>
        <Button disabled label="Disabled" onPress={onPress} />
        <Button busy label="Saving" onPress={onPress} />
      </>,
    );
    expect(screen.getByRole("button", { name: "Disabled" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Saving" })).toBeBusy();
    await fireEvent.press(screen.getByRole("button", { name: "Saving" }));
    expect(onPress).not.toHaveBeenCalled();
  });

  it("draws a visible focus ring for keyboard and web focus", async () => {
    await render(<Button label="Retry" onPress={jest.fn()} variant="secondary" />);
    const button = screen.getByRole("button", { name: "Retry" });
    expect(flat(button).borderWidth).toBe(1);
    await fireEvent(button, "focus");
    expect(flat(screen.getByRole("button", { name: "Retry" }))).toMatchObject({
      borderWidth: 2,
      borderColor: palettes.light.focus,
    });
    await fireEvent(button, "blur");
    expect(flat(screen.getByRole("button", { name: "Retry" })).borderWidth).toBe(1);
  });

  it("distinguishes link buttons by underline, not color alone", async () => {
    await render(<Button label="Open" onPress={jest.fn()} variant="link" />);
    expect(flat(screen.getByText("Open")).textDecorationLine).toBe("underline");
  });
});

describe("status states", () => {
  it("labels loading for screen readers and sighted users", async () => {
    await render(<LoadingState label="Loading Stories" />);
    expect(screen.getByLabelText("Loading Stories")).toBeTruthy();
    expect(screen.getByText("Loading Stories")).toBeTruthy();
  });

  it("announces errors as alerts and offers retry and exit actions in order", async () => {
    const retry = jest.fn();
    const leave = jest.fn();
    await render(
      <ErrorState
        message="The Story could not be loaded."
        primary={{ label: "Try again", onPress: retry }}
        secondary={{ label: "Go to Feed", onPress: leave }}
        title="Something went wrong"
      />,
    );
    expect(screen.getByRole("header", { name: "Something went wrong" })).toBeTruthy();
    expect(screen.getByRole("alert")).toHaveTextContent("The Story could not be loaded.");
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((button) => button.props.accessibilityLabel)).toEqual([
      "Try again",
      "Go to Feed",
    ]);
    await fireEvent.press(buttons[0]);
    await fireEvent.press(buttons[1]);
    expect(retry).toHaveBeenCalledTimes(1);
    expect(leave).toHaveBeenCalledTimes(1);
  });

  it("keeps empty results distinct from errors", async () => {
    await render(<EmptyState message="Pull to refresh later." title="No Stories yet" />);
    expect(screen.getByRole("header", { name: "No Stories yet" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("states status in words", async () => {
    await render(<StatusLabel label="Updating" tone="attention" />);
    expect(screen.getByLabelText("Status: Updating")).toHaveTextContent("Updating");
  });
});

describe("SourceRow", () => {
  it("keeps the publication attributable with an explained external action", async () => {
    const onOpen = jest.fn();
    await render(
      <SourceRow
        detail="Published 19 Sep 2026"
        onOpen={onOpen}
        publisher="Harbor Times"
        title="Storm closes port"
      />,
    );
    expect(screen.getByText("Harbor Times")).toBeTruthy();
    expect(screen.getByText("Storm closes port")).toBeTruthy();
    const open = screen.getByRole("button", { name: "Open publication" });
    expect(open.props.accessibilityHint).toBe("Opens Harbor Times outside Pulso");
    await fireEvent.press(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("shows an unavailable link without dropping the publication", async () => {
    await render(<SourceRow publisher="Harbor Times" title="Storm closes port" />);
    expect(screen.getByText("Storm closes port")).toBeTruthy();
    expect(screen.getByText("Link unavailable")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("Screen", () => {
  it("orders the title before its action and wraps the header at large text", async () => {
    await render(
      <Screen headerAction={<Button label="Sign out" onPress={jest.fn()} />} title="Saved">
        <Text>Body</Text>
      </Screen>,
    );
    expect(screen.getByRole("header", { name: "Saved" })).toBeTruthy();
    expect(flat(screen.getByTestId("screen-header"))).toMatchObject({
      flexDirection: "row",
      flexWrap: "wrap",
    });
    const order = JSON.stringify(screen.toJSON());
    expect(order.indexOf("Saved")).toBeLessThan(order.indexOf("Sign out"));
    expect(order.indexOf("Sign out")).toBeLessThan(order.indexOf("Body"));
  });
});
