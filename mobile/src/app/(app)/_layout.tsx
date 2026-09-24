import { Redirect, usePathname } from "expo-router";
import { useEffect } from "react";

import { ReadingStack } from "@/navigation/ReadingStack";
import {
  RestoreErrorScreen,
  SessionProgressScreen,
} from "@/session/components/SessionStatusScreen";
import { useSession } from "@/session/SessionProvider";

function SignInRedirect({ returnRoute }: { returnRoute: string | null }) {
  const { controller } = useSession();
  useEffect(() => {
    if (returnRoute !== null) controller.rememberReturnRoute(returnRoute);
  }, [controller, returnRoute]);
  return <Redirect href="/sign-in" />;
}

/** A cold Story/source deep link still has the tabs beneath it, so back returns to Feed. */
export const unstable_settings = { initialRouteName: "(tabs)" };

/** Product screens render only for an identified account; every other state stays outside. */
export default function ProtectedLayout() {
  const { state, controller } = useSession();
  const pathname = usePathname();
  switch (state.status) {
    case "authenticated":
      return <ReadingStack />;
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
      // A deliberate sign-out never carries its screen over to the next account.
      return (
        <SignInRedirect
          returnRoute={state.status === "signed_out" && state.reason === "logout" ? null : pathname}
        />
      );
  }
}
