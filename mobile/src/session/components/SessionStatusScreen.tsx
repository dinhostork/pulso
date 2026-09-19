import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import type { SessionErrorCode } from "../types";

const RESTORE_MESSAGES: Partial<Record<SessionErrorCode, string>> = {
  restore_unavailable: "Pulso could not reach the server to restore your session.",
  restore_failed: "Your session could not be restored.",
  credential_read_failed: "This device could not read its stored session.",
};

export function SessionProgressScreen({ label }: { label: string }) {
  return (
    <View style={styles.container}>
      <ActivityIndicator accessibilityLabel={label} size="large" />
      <Text style={styles.message}>{label}</Text>
    </View>
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
    <View style={styles.container}>
      <Text accessibilityRole="alert" style={styles.message}>
        {RESTORE_MESSAGES[code] ?? "Your session could not be restored."}
      </Text>
      <Pressable accessibilityRole="button" onPress={onRetry} style={styles.button}>
        <Text style={styles.buttonText}>Try again</Text>
      </Pressable>
      <Pressable accessibilityRole="button" onPress={onSignOut} style={styles.secondaryButton}>
        <Text style={styles.secondaryButtonText}>Sign out</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 16, padding: 24 },
  message: { fontSize: 16, textAlign: "center" },
  button: {
    alignItems: "center",
    alignSelf: "stretch",
    backgroundColor: "#208AEF",
    borderRadius: 8,
    minHeight: 44,
    justifyContent: "center",
  },
  buttonText: { color: "#FFFFFF", fontSize: 16, fontWeight: "600" },
  secondaryButton: { alignItems: "center", minHeight: 44, justifyContent: "center" },
  secondaryButtonText: { color: "#208AEF", fontSize: 16 },
});
