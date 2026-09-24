import { useRef, useState } from "react";
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, TextInput } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { MIN_TOUCH_TARGET, radius, spacing, typography, useTheme } from "@/theme";

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
  const passwordInput = useRef<TextInput>(null);
  const { colors } = useTheme();
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
    <SafeAreaView style={[styles.fill, { backgroundColor: colors.background }]}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        style={styles.fill}
      >
        {/* Scrolls under the keyboard and at large text so every control stays reachable. */}
        <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
          <AppText variant="title">Sign in to Pulso</AppText>
          {notice ? <AppText accessibilityLiveRegion="polite">{notice}</AppText> : null}
          {cleanupFailed ? (
            <Button
              label="Retry removing stored session"
              onPress={() => void onRetryCleanup()}
              variant="secondary"
            />
          ) : null}
          <TextInput
            accessibilityLabel="Username"
            autoCapitalize="none"
            autoComplete="username"
            autoCorrect={false}
            editable={!busy}
            onChangeText={setUsername}
            onSubmitEditing={() => passwordInput.current?.focus()}
            placeholder="Username"
            placeholderTextColor={colors.textMuted}
            returnKeyType="next"
            submitBehavior="submit"
            style={[styles.input, { borderColor: colors.border, color: colors.text }]}
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
            placeholderTextColor={colors.textMuted}
            ref={passwordInput}
            returnKeyType="go"
            secureTextEntry
            style={[styles.input, { borderColor: colors.border, color: colors.text }]}
            textContentType="password"
            value={password}
          />
          {error ? (
            <AppText accessibilityLiveRegion="assertive" accessibilityRole="alert" tone="danger">
              {error}
            </AppText>
          ) : null}
          <Button
            busy={busy}
            disabled={!canSubmit}
            label={busy ? "Signing in…" : "Sign in"}
            onPress={() => void submit()}
            stretch
          />
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  container: { flexGrow: 1, justifyContent: "center", gap: spacing.md, padding: spacing.lg },
  input: {
    borderWidth: 1,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    minHeight: MIN_TOUCH_TARGET,
    ...typography.body,
  },
});
