import { createContext, useContext, useEffect, useSyncExternalStore, type ReactNode } from "react";

import type { SessionController } from "./controller";
import type { SessionState } from "./types";

const SessionContext = createContext<SessionController | null>(null);

export function SessionProvider({
  controller,
  children,
}: {
  controller: SessionController;
  children: ReactNode;
}) {
  useEffect(() => {
    if (controller.snapshot().status === "cold") void controller.bootstrap();
  }, [controller]);
  return <SessionContext.Provider value={controller}>{children}</SessionContext.Provider>;
}

export function useSession(): { state: SessionState; controller: SessionController } {
  const controller = useContext(SessionContext);
  if (controller === null) throw new Error("useSession must be used inside SessionProvider");
  const state = useSyncExternalStore(
    (onChange) => controller.subscribe(onChange),
    () => controller.snapshot(),
  );
  return { state, controller };
}

/**
 * The signed-in account's decimal ID, which scopes every viewer-decorated
 * query key. Null outside an authenticated session: protected screens are
 * unmounted then, but a screen that is still leaving renders nothing.
 */
export function useAccountId(): string | null {
  const { state } = useSession();
  return state.status === "authenticated" ? state.account.id : null;
}
