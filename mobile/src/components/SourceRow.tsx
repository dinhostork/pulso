import { StyleSheet, View } from "react-native";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";
import { Button } from "./Button";

/**
 * One attributable publication. Callers format the strings (#49 decides the
 * "Published" vs "First seen" wording); the row only lays them out as plain
 * text. Without `onOpen` the publication stays attributed and the link is
 * shown as unavailable instead of failing the whole list.
 */
export function SourceRow({
  publisher,
  title,
  detail,
  onOpen,
}: {
  publisher: string;
  title: string;
  detail?: string | null;
  onOpen?: () => void;
}) {
  const { colors } = useTheme();
  return (
    <View style={[styles.row, { borderColor: colors.border }]}>
      <AppText tone="muted" variant="caption">
        {publisher}
      </AppText>
      <AppText variant="label">{title}</AppText>
      {detail ? (
        <AppText tone="muted" variant="caption">
          {detail}
        </AppText>
      ) : null}
      {onOpen ? (
        <Button
          hint={`Opens ${publisher} outside Pulso`}
          label="Open publication"
          onPress={onOpen}
          variant="link"
        />
      ) : (
        <AppText tone="muted" variant="caption">
          Link unavailable
        </AppText>
      )}
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
