import { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import type { SessionErrorCode, SessionNotice, SessionState } from "../types";

type SignInState = Extract<
  SessionState,
  { status: "signed_out" } | { status: "signing_in" } | { status: "sign_in_error" }
>;

const RESIDUAL_ACCESS = "Access issued before signing out can remain valid for up to 15 minutes.";

const ERROR_MESSAGES: Partial<Record<SessionErrorCode, string>> = {
  // One message for every rejected login, so the screen never confirms an account exists.
  invalid_credentials: "The username or password is incorrect.",
  sign_in_unavailable: "Sign-in is unavailable right now. Check your connection and try again.",
  credential_write_failed: "This device could not store your session securely. Try again.",
  credential_delete_failed: "This device could not clear its previous session. Try again.",
};

function noticeMessage(state: Extract<SessionState, { status: "signed_out" }>): string | null {
  const notice: SessionNotice | undefined = state.notice;
  if (notice?.code === "credential_delete_failed") {
    return "You are signed out, but this device could not delete its stored session.";
  }
  if (notice?.code === "remote_logout_failed") {
    return (
      "You are signed out on this device, but the server could not confirm the session was " +
      "revoked. The removed session credential stays valid until it expires (up to 14 days). " +
      RESIDUAL_ACCESS
    );
  }
  if (state.reason === "logout") return `You are signed out. ${RESIDUAL_ACCESS}`;
  if (state.reason === "expired") return "Your session has ended. Sign in again.";
  return null;
}

export function SignInScreen({
  state,
  onSubmit,
  onRetryCleanup,
}: {
  state: SignInState;
  onSubmit: (username: string, password: string) => Promise<void>;
  onRetryCleanup: () => Promise<unknown>;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const busy = state.status === "signing_in";
  const canSubmit = !busy && username.trim().length > 0 && password.length > 0;
  const error = state.status === "sign_in_error" ? ERROR_MESSAGES[state.error.code] : undefined;
  const notice = state.status === "signed_out" ? noticeMessage(state) : null;
  const cleanupFailed =
    state.status === "signed_out" && state.notice?.code === "credential_delete_failed";

  async function submit() {
    if (!canSubmit) return;
    const submitted = password;
    // The password lives only in this submission; the field never keeps it afterwards.
    setPassword("");
    await onSubmit(username.trim(), submitted);
  }

  return (
    <View style={styles.container}>
      <Text accessibilityRole="header" style={styles.title}>
        Sign in to Pulso
      </Text>
      {notice ? (
        <Text accessibilityLiveRegion="polite" style={styles.notice}>
          {notice}
        </Text>
      ) : null}
      {cleanupFailed ? (
        <Pressable
          accessibilityRole="button"
          onPress={() => void onRetryCleanup()}
          style={styles.secondaryButton}
        >
          <Text style={styles.secondaryButtonText}>Retry removing stored session</Text>
        </Pressable>
      ) : null}
      <TextInput
        accessibilityLabel="Username"
        autoCapitalize="none"
        autoComplete="username"
        autoCorrect={false}
        editable={!busy}
        onChangeText={setUsername}
        placeholder="Username"
        style={styles.input}
        textContentType="username"
        value={username}
      />
      <TextInput
        accessibilityLabel="Password"
        autoCapitalize="none"
        autoComplete="current-password"
        autoCorrect={false}
        editable={!busy}
        onChangeText={setPassword}
        onSubmitEditing={() => void submit()}
        placeholder="Password"
        secureTextEntry
        style={styles.input}
        textContentType="password"
        value={password}
      />
      {error ? (
        <Text accessibilityLiveRegion="assertive" accessibilityRole="alert" style={styles.error}>
          {error}
        </Text>
      ) : null}
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ disabled: !canSubmit, busy }}
        disabled={!canSubmit}
        onPress={() => void submit()}
        style={[styles.button, !canSubmit && styles.buttonDisabled]}
      >
        <Text style={styles.buttonText}>{busy ? "Signing in…" : "Sign in"}</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, justifyContent: "center", gap: 12, padding: 24 },
  title: { fontSize: 24, fontWeight: "600", marginBottom: 8 },
  notice: { fontSize: 14, color: "#444444" },
  input: {
    borderWidth: 1,
    borderColor: "#999999",
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 16,
    minHeight: 44,
  },
  error: { fontSize: 14, color: "#B00020" },
  button: {
    alignItems: "center",
    backgroundColor: "#208AEF",
    borderRadius: 8,
    minHeight: 44,
    justifyContent: "center",
  },
  buttonDisabled: { opacity: 0.5 },
  buttonText: { color: "#FFFFFF", fontSize: 16, fontWeight: "600" },
  secondaryButton: { alignItems: "center", minHeight: 44, justifyContent: "center" },
  secondaryButtonText: { color: "#208AEF", fontSize: 16 },
});
