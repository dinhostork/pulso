import { useRouter, type Href } from "expo-router";
import { useEffect } from "react";

import {
  RestoreErrorScreen,
  SessionProgressScreen,
} from "@/session/components/SessionStatusScreen";
import { SignInScreen } from "@/session/components/SignInScreen";
import { useSession } from "@/session/SessionProvider";

export default function SignInRoute() {
  const { state, controller } = useSession();
  const router = useRouter();
  const authenticated = state.status === "authenticated";

  useEffect(() => {
    if (!authenticated) return;
    // Only a validated in-app path can have been remembered (see session/routes.ts).
    router.replace((controller.consumeReturnRoute() ?? "/") as Href);
  }, [authenticated, controller, router]);

  switch (state.status) {
    case "authenticated":
    case "cold":
    case "restoring":
      return <SessionProgressScreen label="Restoring your session" />;
    case "logging_out":
      return <SessionProgressScreen label="Signing out" />;
    case "restore_error":
      return (
        <RestoreErrorScreen
          code={state.error.code}
          onRetry={() => void controller.bootstrap()}
          onSignOut={() => void controller.logout()}
        />
      );
    default:
      return (
        <SignInScreen
          state={state}
          onSubmit={(username, password) => controller.signIn(username, password)}
          onRetryCleanup={() => controller.retryCredentialCleanup()}
        />
      );
  }
}
