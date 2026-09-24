import { ActivityIndicator, StyleSheet, View } from "react-native";

import { radius, spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

export interface StateAction {
  label: string;
  onPress: () => void;
  hint?: string;
}

function Actions({ primary, secondary }: { primary?: StateAction; secondary?: StateAction }) {
  if (!primary && !secondary) return null;
  return (
    <View style={styles.actions}>
      {primary ? <Button {...primary} stretch /> : null}
      {secondary ? <Button {...secondary} stretch variant="secondary" /> : null}
    </View>
  );
}

/** Progress with a visible and announced label; never an unlabeled spinner. */
export function LoadingState({ label }: { label: string }) {
  const { colors } = useTheme();
  return (
    <View accessibilityLiveRegion="polite" style={styles.container}>
      <ActivityIndicator accessibilityLabel={label} color={colors.accent} size="large" />
      <AppText style={styles.centered} tone="muted">
        {label}
      </AppText>
    </View>
  );
}

/**
 * A failure explained in words and announced as an alert, with a way forward.
 * `primary` is usually retry; `secondary` leaves the failing screen.
 */
export function ErrorState({
  title,
  message,
  primary,
  secondary,
}: {
  title?: string;
  message: string;
  primary?: StateAction;
  secondary?: StateAction;
}) {
  return (
    <View style={styles.container}>
      {title ? (
        <AppText style={styles.centered} variant="heading">
          {title}
        </AppText>
      ) : null}
      <AppText
        accessibilityLiveRegion="assertive"
        accessibilityRole="alert"
        style={styles.centered}
      >
        {message}
      </AppText>
      <Actions primary={primary} secondary={secondary} />
    </View>
  );
}

/** A successful response with nothing to show; distinct from an error. */
export function EmptyState({
  title,
  message,
  action,
}: {
  title: string;
  message?: string;
  action?: StateAction;
}) {
  return (
    <View style={styles.container}>
      <AppText style={styles.centered} variant="heading">
        {title}
      </AppText>
      {message ? (
        <AppText style={styles.centered} tone="muted">
          {message}
        </AppText>
      ) : null}
      <Actions primary={action} />
    </View>
  );
}

export type StatusTone = "neutral" | "attention";

/**
 * A short text status such as "Updating" or "Unavailable". The words carry the
 * meaning; the outline only groups them, so status never depends on color.
 */
export function StatusLabel({ label, tone = "neutral" }: { label: string; tone?: StatusTone }) {
  const { colors } = useTheme();
  return (
    <View
      accessibilityLabel={`Status: ${label}`}
      accessible
      style={[styles.status, { borderColor: tone === "attention" ? colors.danger : colors.border }]}
    >
      <AppText tone={tone === "attention" ? "danger" : "muted"} variant="caption">
        {label}
      </AppText>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    paddingVertical: spacing.lg,
  },
  centered: { textAlign: "center" },
  actions: { alignSelf: "stretch", gap: spacing.sm },
  status: {
    alignSelf: "flex-start",
    borderRadius: radius.sm,
    borderWidth: 1,
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
  },
});
