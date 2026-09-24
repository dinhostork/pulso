import { useState } from "react";
import { Pressable, StyleSheet } from "react-native";

import { MIN_TOUCH_TARGET, radius, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

export type ButtonVariant = "primary" | "secondary" | "link";

export interface ButtonProps {
  label: string;
  onPress: () => void;
  variant?: ButtonVariant;
  /** Announced after the label, e.g. "Opens the publisher outside Pulso". */
  hint?: string;
  disabled?: boolean;
  busy?: boolean;
  /** Fill the parent's width instead of hugging the label. */
  stretch?: boolean;
  testID?: string;
}

/**
 * A 44×44 minimum target whose label wraps at large text sizes. Variants are
 * distinguished by shape (fill, outline, underline), not by color alone, and
 * keyboard/web focus draws a visible ring.
 */
export function Button({
  label,
  onPress,
  variant = "primary",
  hint,
  disabled = false,
  busy = false,
  stretch = false,
  testID,
}: ButtonProps) {
  const { colors } = useTheme();
  const [focused, setFocused] = useState(false);
  const inactive = disabled || busy;
  return (
    <Pressable
      accessibilityHint={hint}
      accessibilityLabel={label}
      accessibilityRole="button"
      accessibilityState={{ disabled: inactive, busy }}
      disabled={inactive}
      onBlur={() => setFocused(false)}
      onFocus={() => setFocused(true)}
      onPress={onPress}
      style={({ pressed }) => [
        styles.base,
        stretch ? styles.stretch : styles.hug,
        variant === "primary" && { backgroundColor: colors.accent },
        variant === "secondary" && { borderColor: colors.border, borderWidth: 1 },
        pressed && styles.pressed,
        inactive && styles.inactive,
        focused && { borderColor: colors.focus, borderWidth: 2 },
      ]}
      testID={testID}
    >
      <AppText
        style={[
          styles.label,
          variant === "primary" && { color: colors.onAccent },
          variant === "link" && styles.link,
        ]}
        tone="accent"
        variant="label"
      >
        {label}
      </AppText>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: MIN_TOUCH_TARGET,
    minWidth: MIN_TOUCH_TARGET,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radius.md,
  },
  hug: { alignSelf: "flex-start" },
  stretch: { alignSelf: "stretch" },
  pressed: { opacity: 0.7 },
  inactive: { opacity: 0.5 },
  label: { textAlign: "center" },
  link: { textDecorationLine: "underline" },
});
