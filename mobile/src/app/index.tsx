import { StyleSheet, Text, View } from "react-native";

import { apiBaseUrl } from "@/config/env";

export default function Index() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>Pulso</Text>
      <Text style={styles.subtitle}>Mobile application shell</Text>
      <Text style={styles.meta}>API base URL: {apiBaseUrl}</Text>
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
});
