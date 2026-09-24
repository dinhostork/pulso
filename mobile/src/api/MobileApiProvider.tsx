import { createContext, useContext, type ReactNode } from "react";

import type { MobileApi } from "./client";

const MobileApiContext = createContext<MobileApi | null>(null);

/**
 * Hands screens the product API built on the session runtime's transport, so
 * every request shares that runtime's access token, epoch guard and refresh.
 */
export function MobileApiProvider({ api, children }: { api: MobileApi; children: ReactNode }) {
  return <MobileApiContext.Provider value={api}>{children}</MobileApiContext.Provider>;
}

export function useMobileApi(): MobileApi {
  const api = useContext(MobileApiContext);
  if (api === null) throw new Error("useMobileApi must be used inside MobileApiProvider");
  return api;
}
