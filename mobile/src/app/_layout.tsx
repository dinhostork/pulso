import { QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { queryClient } from "@/server-state/query";
import { sessionRuntime } from "@/session/runtime";
import { SessionProvider } from "@/session/SessionProvider";

export default function RootLayout() {
  return (
    <QueryClientProvider client={queryClient}>
      <SessionProvider controller={sessionRuntime.controller}>
        <SafeAreaProvider>
          <StatusBar style="auto" />
          <Stack screenOptions={{ headerShown: false }} />
        </SafeAreaProvider>
      </SessionProvider>
    </QueryClientProvider>
  );
}
