import { StyleSheet, View } from "react-native";

import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { spacing } from "@/theme";

import { useSession } from "../SessionProvider";

/** The signed-in account and the always-reachable sign-out action for tab headers. */
export function AccountActions() {
  const { state, controller } = useSession();
  return (
    <View style={styles.container}>
      {state.status === "authenticated" ? (
        <AppText tone="muted" variant="caption">
          Signed in as {state.account.username}
        </AppText>
      ) : null}
      <Button label="Sign out" onPress={() => void controller.logout()} variant="secondary" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm },
});
