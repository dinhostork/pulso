import { Tabs } from "expo-router";
import { StyleSheet, useWindowDimensions, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { spacing, typography, useTheme } from "@/theme";

/** Indicator bar plus vertical padding around the label, before text scaling. */
const TAB_CHROME = 4 + spacing.xs + spacing.sm * 2;
const DEFAULT_TAB_BAR_HEIGHT = 49;

/**
 * The default tab bar height is fixed; this one grows with dynamic text so the
 * labels are never clipped, and never shrinks below the platform size.
 */
export function tabBarHeight(fontScale: number, bottomInset: number): number {
  const labelHeight = Math.ceil(typography.caption.lineHeight * Math.max(1, fontScale));
  return Math.max(DEFAULT_TAB_BAR_HEIGHT, TAB_CHROME + labelHeight) + bottomInset;
}

/**
 * Phase 3 has exactly two destinations. Later-phase areas (Pulse, Opinions,
 * profile, explore, search) get no tab until they exist.
 */
export function ReadingTabs() {
  const { colors } = useTheme();
  const { fontScale } = useWindowDimensions();
  const insets = useSafeAreaInsets();
  return (
    <Tabs
      backBehavior="firstRoute"
      screenOptions={{
        headerShown: false,
        animation: "none",
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textMuted,
        tabBarLabelStyle: typography.caption,
        tabBarStyle: {
          backgroundColor: colors.surface,
          borderTopColor: colors.border,
          height: tabBarHeight(fontScale, insets.bottom),
        },
        // The selected tab also shows a bar, so selection is not color-only.
        tabBarIcon: ({ focused, color }) => (
          <View style={[styles.indicator, { backgroundColor: focused ? color : "transparent" }]} />
        ),
      }}
    >
      <Tabs.Screen name="index" options={{ title: "Feed", tabBarButtonTestID: "tab-feed" }} />
      <Tabs.Screen name="saved" options={{ title: "Saved", tabBarButtonTestID: "tab-saved" }} />
    </Tabs>
  );
}

const styles = StyleSheet.create({
  indicator: { width: 24, height: 4, borderRadius: 2 },
});
