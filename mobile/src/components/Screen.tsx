import type { ReactNode } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { SafeAreaView, type Edge } from "react-native-safe-area-context";

import { spacing, useTheme } from "@/theme";

import { AppText } from "./AppText";

/**
 * Safe-area aware screen layout. Content scrolls by default so every control
 * stays reachable with large text on small screens; the header row wraps its
 * action below the title instead of truncating either. List screens pass
 * `scroll={false}` and supply their own virtualized list.
 */
export function Screen({
  title,
  headerAction,
  edges = ["top", "left", "right"],
  scroll = true,
  children,
}: {
  title?: string;
  headerAction?: ReactNode;
  edges?: Edge[];
  scroll?: boolean;
  children: ReactNode;
}) {
  const { colors } = useTheme();
  const header =
    title || headerAction ? (
      <View style={styles.header} testID="screen-header">
        {title ? (
          <AppText style={styles.title} variant="title">
            {title}
          </AppText>
        ) : null}
        {headerAction}
      </View>
    ) : null;
  return (
    <SafeAreaView edges={edges} style={[styles.fill, { backgroundColor: colors.background }]}>
      {scroll ? (
        <ScrollView
          contentContainerStyle={styles.content}
          keyboardShouldPersistTaps="handled"
          style={styles.fill}
        >
          {header}
          {children}
        </ScrollView>
      ) : (
        <View style={[styles.fill, styles.content]}>
          {header}
          {children}
        </View>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { flexGrow: 1, gap: spacing.md, padding: spacing.md },
  header: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  title: { flexShrink: 1 },
});
