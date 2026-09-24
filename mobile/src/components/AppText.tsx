import { Text, type TextProps } from "react-native";

import { typography, useTheme, type TypographyVariant } from "@/theme";

export type TextTone = "default" | "muted" | "accent" | "danger";

export interface AppTextProps extends TextProps {
  variant?: TypographyVariant;
  tone?: TextTone;
}

const TONE_COLOR = {
  default: "text",
  muted: "textMuted",
  accent: "accent",
  danger: "danger",
} as const;

/**
 * Plain-text rendering with scalable type. Publication strings are passed as
 * children and never interpreted as markup. Titles and headings are exposed
 * to screen readers as headers.
 */
export function AppText({ variant = "body", tone = "default", style, ...props }: AppTextProps) {
  const { colors } = useTheme();
  const header = variant === "title" || variant === "heading";
  return (
    <Text
      accessibilityRole={header ? "header" : undefined}
      {...props}
      style={[typography[variant], { color: colors[TONE_COLOR[tone]] }, style]}
    />
  );
}
