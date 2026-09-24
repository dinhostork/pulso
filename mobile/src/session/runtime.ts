import type { QueryClient } from "@tanstack/react-query";

import { createMobileApi, type MobileApi } from "@/api/client";
import { createTransport } from "@/api/transport";
import { ImpressionQueue } from "@/features/impressions/queue";
import { queryClient as sharedQueryClient } from "@/server-state/query";

import { SessionApi } from "./auth-api";
import { SessionController } from "./controller";
import { createRefreshTokenStore, type RefreshTokenStore } from "./storage";

export interface SessionRuntime {
  controller: SessionController;
  api: MobileApi;
  /** The server-state cache the controller clears on every account boundary. */
  queryClient: QueryClient;
  /** FeedImpression delivery, cleared on every account boundary. */
  impressions: ImpressionQueue;
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
 * share the same access token, epoch guard and single-flight refresh. The
 * impression queue follows the controller's session changes, so it never
 * outlives the account that produced its events.
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
  const queryClient = options.queryClient ?? sharedQueryClient;
  controller = new SessionController({
    api: new SessionApi(transport),
    queryClient,
    refreshTokenStore: options.refreshTokenStore ?? createRefreshTokenStore(),
  });
  const api = createMobileApi(transport);
  const impressions = new ImpressionQueue({
    send: (events, signal) => api.reportFeedImpressions(events, signal),
    now: () => Date.now(),
  });
  controller.subscribeSessionChanges((change) => impressions.onSessionChange(change));
  return { controller, api, queryClient, impressions };
}

export const sessionRuntime = createSessionRuntime();
