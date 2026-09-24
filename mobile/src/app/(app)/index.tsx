import { Pressable, StyleSheet, Text, View } from "react-native";

import { useSession } from "@/session/SessionProvider";

export default function Index() {
  const { state, controller } = useSession();
  return (
    <View style={styles.container}>
      <Text accessibilityRole="header" style={styles.title}>
        Pulso
      </Text>
      <Text style={styles.subtitle}>Feed</Text>
      {state.status === "authenticated" ? (
        <Text style={styles.meta}>Signed in as {state.account.username}</Text>
      ) : null}
      <Pressable
        accessibilityRole="button"
        onPress={() => void controller.logout()}
        style={styles.button}
      >
        <Text style={styles.buttonText}>Sign out</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: 24,
  },
  title: {
    fontSize: 28,
    fontWeight: "600",
  },
  subtitle: {
    fontSize: 16,
    color: "#666666",
  },
  meta: {
    marginTop: 24,
    fontSize: 12,
    color: "#999999",
  },
  button: { marginTop: 24, minHeight: 44, justifyContent: "center", paddingHorizontal: 16 },
  buttonText: { color: "#208AEF", fontSize: 16 },
});
