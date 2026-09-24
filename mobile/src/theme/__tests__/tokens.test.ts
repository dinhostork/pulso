import { renderHook, waitFor } from "@testing-library/react-native";
import { AccessibilityInfo } from "react-native";

import { palettes, useReducedMotion, type Palette } from "@/theme";

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255);
  const [r, g, b] = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const [light, dark] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

// WCAG 2.2 AA: 4.5:1 for text, 3:1 for component boundaries and focus indicators.
const TEXT_PAIRS: [keyof Palette, keyof Palette][] = [
  ["text", "background"],
  ["text", "surface"],
  ["textMuted", "background"],
  ["textMuted", "surface"],
  ["accent", "background"],
  ["accent", "surface"],
  ["danger", "background"],
  ["onAccent", "accent"],
];
const UI_PAIRS: [keyof Palette, keyof Palette][] = [
  ["border", "background"],
  ["border", "surface"],
  ["focus", "background"],
];

describe.each(Object.entries(palettes))("%s palette", (_scheme, palette) => {
  it.each(TEXT_PAIRS)("%s on %s meets 4.5:1", (foreground, background) => {
    expect(contrast(palette[foreground], palette[background])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(UI_PAIRS)("%s on %s meets 3:1", (foreground, background) => {
    expect(contrast(palette[foreground], palette[background])).toBeGreaterThanOrEqual(3);
  });
});

describe("useReducedMotion", () => {
  it("follows the platform setting and its changes", async () => {
    let notify: (value: boolean) => void = () => undefined;
    jest.spyOn(AccessibilityInfo, "isReduceMotionEnabled").mockResolvedValue(true);
    jest.spyOn(AccessibilityInfo, "addEventListener").mockImplementation((_event, handler) => {
      notify = handler as unknown as (value: boolean) => void;
      return { remove: jest.fn() } as never;
    });

    const { result } = await renderHook(() => useReducedMotion());

    await waitFor(() => expect(result.current).toBe(true));
    await waitFor(() => {
      notify(false);
      expect(result.current).toBe(false);
    });
  });
});
