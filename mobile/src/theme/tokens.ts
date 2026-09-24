/**
 * The small semantic token set shared by reading screens (#46). Screens name
 * roles ("muted text", "accent"), never raw colors, so contrast is checked once
 * here (see theme/__tests__/tokens.test.ts) for both color schemes.
 */

export type ColorScheme = "light" | "dark";

export interface Palette {
  background: string;
  surface: string;
  text: string;
  textMuted: string;
  border: string;
  accent: string;
  onAccent: string;
  danger: string;
  focus: string;
}

export const palettes: Record<ColorScheme, Palette> = {
  light: {
    background: "#FFFFFF",
    surface: "#F4F5F7",
    text: "#16181D",
    textMuted: "#4F5561",
    border: "#6B717D",
    accent: "#0B5CC2",
    onAccent: "#FFFFFF",
    danger: "#B3261E",
    focus: "#0B5CC2",
  },
  dark: {
    background: "#111315",
    surface: "#1C1F23",
    text: "#ECEEF1",
    textMuted: "#B4BAC3",
    border: "#8A919C",
    accent: "#8AB8FF",
    onAccent: "#0A1B33",
    danger: "#FF8A80",
    focus: "#8AB8FF",
  },
};

export const spacing = { xs: 4, sm: 8, md: 16, lg: 24, xl: 32 } as const;

export const radius = { sm: 4, md: 8 } as const;

/** Minimum logical size of every touch target (44×44). */
export const MIN_TOUCH_TARGET = 44;

/**
 * Base sizes only. Text keeps `allowFontScaling`, so the platform's dynamic
 * text setting multiplies these; layouts must wrap rather than fix heights.
 */
export const typography = {
  title: { fontSize: 24, lineHeight: 30, fontWeight: "600" },
  heading: { fontSize: 18, lineHeight: 24, fontWeight: "600" },
  body: { fontSize: 16, lineHeight: 22, fontWeight: "400" },
  label: { fontSize: 16, lineHeight: 22, fontWeight: "600" },
  caption: { fontSize: 14, lineHeight: 20, fontWeight: "400" },
} as const;

export type TypographyVariant = keyof typeof typography;
