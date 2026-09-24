import { StyleSheet, View } from "react-native";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

/**
 * One attributable publication. Callers format the strings (for example
 * "Published" vs "First seen"); the row only lays them out as plain text.
 * Without `onOpen` the publication stays attributed and the link is shown as
 * unavailable instead of failing the whole list. `notes` carry extra worded
 * facts (byline, membership); `openError` explains a failed hand-off.
 */
export function SourceRow({
  publisher,
  title,
  detail,
  notes = [],
  onOpen,
  openError,
  testID,
}: {
  publisher: string;
  title: string;
  detail?: string | null;
  notes?: readonly string[];
  onOpen?: () => void;
  openError?: string | null;
  testID?: string;
}) {
  const { colors } = useTheme();
  return (
    <View style={[styles.row, { borderColor: colors.border }]} testID={testID}>
      <AppText tone="muted" variant="caption">
        {publisher}
      </AppText>
      <AppText variant="label">{title}</AppText>
      {detail ? (
        <AppText tone="muted" variant="caption">
          {detail}
        </AppText>
      ) : null}
      {notes.map((note) => (
        <AppText key={note} tone="muted" variant="caption">
          {note}
        </AppText>
      ))}
      {onOpen ? (
        <Button
          hint={`Opens ${publisher} outside Pulso`}
          label="Open publication (external site)"
          onPress={onOpen}
          variant="link"
        />
      ) : (
        <AppText tone="muted" variant="caption">
          Link unavailable
        </AppText>
      )}
      {openError ? (
        <AppText accessibilityLiveRegion="polite" accessibilityRole="alert" tone="danger">
          {openError}
        </AppText>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    gap: spacing.xs,
    paddingVertical: spacing.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
});
