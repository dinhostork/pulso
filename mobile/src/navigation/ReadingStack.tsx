import { Stack } from "expo-router";

import { useReducedMotion, useTheme } from "@/theme";

/**
 * The authenticated stack: the Feed/Saved tabs with Story and source screens
 * pushed above them. Story screens show a native header whose back action
 * (and Android's system back) pops to the tabs; iOS keeps its edge swipe.
 */
export function ReadingStack() {
  const { colors } = useTheme();
  const reduceMotion = useReducedMotion();
  return (
    <Stack
      screenOptions={{
        headerShown: false,
        animation: reduceMotion ? "none" : "default",
        contentStyle: { backgroundColor: colors.background },
        headerStyle: { backgroundColor: colors.surface },
        headerTintColor: colors.accent,
        headerTitleStyle: { color: colors.text },
        headerBackButtonDisplayMode: "minimal",
      }}
    >
      <Stack.Screen name="(tabs)" />
      <Stack.Screen
        name="stories/[storyId]/index"
        options={{ headerShown: true, title: "Story" }}
      />
      <Stack.Screen
        name="stories/[storyId]/sources"
        options={{ headerShown: true, title: "Sources" }}
      />
    </Stack>
  );
}
