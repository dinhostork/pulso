import { useRouter } from "expo-router";

import { Screen } from "@/components/Screen";
import { ErrorState } from "@/components/StatusState";

import { FEED_HREF } from "./routes";

/** Unknown paths get a recoverable state, not a render error or a redirect loop. */
export function NotFoundScreen() {
  const router = useRouter();
  return (
    <Screen edges={["top", "bottom", "left", "right"]}>
      <ErrorState
        message="This page does not exist in Pulso."
        primary={{ label: "Go to Feed", onPress: () => router.replace(FEED_HREF) }}
        title="Page not found"
      />
    </Screen>
  );
}
