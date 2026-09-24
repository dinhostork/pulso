import { QueryClientProvider } from "@tanstack/react-query";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { MobileApiProvider } from "@/api/MobileApiProvider";
import { sessionRuntime } from "@/session/runtime";
import { SessionProvider } from "@/session/SessionProvider";

export default function RootLayout() {
  return (
    <QueryClientProvider client={sessionRuntime.queryClient}>
      <MobileApiProvider api={sessionRuntime.api}>
        <SessionProvider controller={sessionRuntime.controller}>
          <SafeAreaProvider>
            <StatusBar style="auto" />
            <Stack screenOptions={{ headerShown: false }} />
          </SafeAreaProvider>
        </SessionProvider>
      </MobileApiProvider>
    </QueryClientProvider>
  );
}
