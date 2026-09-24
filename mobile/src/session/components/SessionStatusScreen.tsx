import type { ReactNode } from "react";
import { SafeAreaView } from "react-native-safe-area-context";

import { ErrorState, LoadingState } from "@/components/StatusState";
import { spacing, useTheme } from "@/theme";

import type { SessionErrorCode } from "../types";

const RESTORE_MESSAGES: Partial<Record<SessionErrorCode, string>> = {
  restore_unavailable: "Pulso could not reach the server to restore your session.",
  restore_failed: "Your session could not be restored.",
  credential_read_failed: "This device could not read its stored session.",
};

function StatusFrame({ children }: { children: ReactNode }) {
  const { colors } = useTheme();
  return (
    <SafeAreaView style={{ flex: 1, padding: spacing.lg, backgroundColor: colors.background }}>
      {children}
    </SafeAreaView>
  );
}

export function SessionProgressScreen({ label }: { label: string }) {
  return (
    <StatusFrame>
      <LoadingState label={label} />
    </StatusFrame>
  );
}

export function RestoreErrorScreen({
  code,
  onRetry,
  onSignOut,
}: {
  code: SessionErrorCode;
  onRetry: () => void;
  onSignOut: () => void;
}) {
  return (
    <StatusFrame>
      <ErrorState
        message={RESTORE_MESSAGES[code] ?? "Your session could not be restored."}
        primary={{ label: "Try again", onPress: onRetry }}
        secondary={{ label: "Sign out", onPress: onSignOut }}
      />
    </StatusFrame>
  );
}
