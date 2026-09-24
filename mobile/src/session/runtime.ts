import type { QueryClient } from "@tanstack/react-query";

import { createMobileApi, type MobileApi } from "@/api/client";
import { createTransport } from "@/api/transport";
import { queryClient as sharedQueryClient } from "@/server-state/query";

import { SessionApi } from "./auth-api";
import { SessionController } from "./controller";
import { createRefreshTokenStore, type RefreshTokenStore } from "./storage";

export interface SessionRuntime {
  controller: SessionController;
  api: MobileApi;
}

export interface SessionRuntimeOptions {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
  allowInsecureHttp?: boolean;
  queryClient?: QueryClient;
  refreshTokenStore?: RefreshTokenStore;
}

/**
 * Wires one transport to one session controller: product and auth requests
 * share the same access token, epoch guard and single-flight refresh.
 */
export function createSessionRuntime(options: SessionRuntimeOptions = {}): SessionRuntime {
  let controller: SessionController | null = null;
  const transport = createTransport({
    baseUrl: options.baseUrl,
    fetch: options.fetch,
    allowInsecureHttp: options.allowInsecureHttp,
    credentials: {
      accessToken: () => controller?.accessToken() ?? null,
      sessionEpoch: () => controller?.sessionEpoch() ?? 0,
      refreshAfterUnauthorized: (epoch, rejectedToken) =>
        controller?.refreshAfterUnauthorized(epoch, rejectedToken) ?? Promise.resolve(null),
    },
  });
  controller = new SessionController({
    api: new SessionApi(transport),
    queryClient: options.queryClient ?? sharedQueryClient,
    refreshTokenStore: options.refreshTokenStore ?? createRefreshTokenStore(),
  });
  return { controller, api: createMobileApi(transport) };
}

export const sessionRuntime = createSessionRuntime();
