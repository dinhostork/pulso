import type { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";
import { clearAccountServerState } from "@/server-state/query";

import { SessionApi } from "./auth-api";
import { validatedPendingReturnRoute } from "./routes";
import { SerializedRefreshTokenStore, type RefreshTokenStore } from "./storage";
import type { LogoutResult, SessionChange, SessionErrorCode, SessionState } from "./types";

type StateListener = (state: SessionState) => void;
type SessionChangeListener = (change: SessionChange) => void;

interface SessionControllerOptions {
  api: SessionApi;
  queryClient: QueryClient;
  refreshTokenStore: RefreshTokenStore;
}

function isTerminalAuthenticationFailure(error: unknown): boolean {
  return (
    error instanceof ApiError && error.kind === "http" && [400, 401].includes(error.status ?? 0)
  );
}

function isConnectivityFailure(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.kind === "network" ||
      error.kind === "timeout" ||
      (error.kind === "http" && (error.status ?? 0) >= 500))
  );
}

function signInErrorCode(error: unknown): SessionErrorCode {
  if (error instanceof ApiError && error.kind === "http" && error.status === 400) {
    return "invalid_credentials";
  }
  return "sign_in_unavailable";
}

export class SessionController {
  private state: SessionState = { status: "cold" };
  private epoch = 0;
  private access: string | null = null;
  private refresh: string | null = null;
  private accountId: string | null = null;
  private pendingReturnRoute: string | null = null;
  private refreshFlight: { epoch: number; promise: Promise<string | null> } | null = null;
  private bootstrapFlight: Promise<void> | null = null;
  private readonly stateListeners = new Set<StateListener>();
  private readonly changeListeners = new Set<SessionChangeListener>();
  private readonly credentials: SerializedRefreshTokenStore;
  private readonly api: SessionApi;
  private readonly queryClient: QueryClient;

  constructor(options: SessionControllerOptions) {
    this.api = options.api;
    this.queryClient = options.queryClient;
    this.credentials = new SerializedRefreshTokenStore(options.refreshTokenStore);
  }

  snapshot(): SessionState {
    return this.state;
  }

  accessToken(): string | null {
    return this.access;
  }

  sessionEpoch(): number {
    return this.epoch;
  }

  subscribe(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  subscribeSessionChanges(listener: SessionChangeListener): () => void {
    this.changeListeners.add(listener);
    return () => this.changeListeners.delete(listener);
  }

  rememberReturnRoute(candidate: unknown): boolean {
    const route = validatedPendingReturnRoute(candidate);
    this.pendingReturnRoute = route;
    return route !== null;
  }

  consumeReturnRoute(): string | null {
    const route = this.pendingReturnRoute;
    this.pendingReturnRoute = null;
    return route;
  }

  private publish(state: SessionState): void {
    this.state = state;
    for (const listener of this.stateListeners) listener(state);
  }

  private publishSessionChange(): void {
    const change = { epoch: this.epoch, accountId: this.accountId };
    for (const listener of this.changeListeners) listener(change);
  }

  private beginBoundary(state: SessionState): { epoch: number; clearing: Promise<void> } {
    const previousAccountId = this.accountId;
    this.epoch += 1;
    this.access = null;
    this.refresh = null;
    this.accountId = null;
    this.refreshFlight = null;
    this.publish(state);
    this.publishSessionChange();
    return {
      epoch: this.epoch,
      clearing: previousAccountId
        ? clearAccountServerState(this.queryClient, previousAccountId)
        : Promise.resolve(),
    };
  }

  private current(epoch: number): boolean {
    return this.epoch === epoch;
  }

  private authenticate(epoch: number, account: { id: string; username: string }): void {
    if (!this.current(epoch)) return;
    this.accountId = account.id;
    this.publish({ status: "authenticated", account });
    this.publishSessionChange();
  }

  bootstrap(): Promise<void> {
    if (this.bootstrapFlight) return this.bootstrapFlight;
    const operation = this.performBootstrap().finally(() => {
      if (this.bootstrapFlight === operation) this.bootstrapFlight = null;
    });
    this.bootstrapFlight = operation;
    return operation;
  }

  private async performBootstrap(): Promise<void> {
    const boundary = this.beginBoundary({ status: "restoring" });
    await boundary.clearing;
    let refresh: string | null;
    try {
      refresh = await this.credentials.read();
    } catch {
      if (this.current(boundary.epoch)) {
        this.publish({
          status: "restore_error",
          error: { code: "credential_read_failed", retryable: true },
        });
      }
      return;
    }
    if (!this.current(boundary.epoch)) return;
    if (!refresh) {
      this.publish({ status: "signed_out", reason: "initial" });
      return;
    }
    this.refresh = refresh;
    let access: string;
    try {
      access = await this.api.refresh(refresh);
    } catch (error) {
      if (!this.current(boundary.epoch)) return;
      if (isTerminalAuthenticationFailure(error)) {
        await this.expire(boundary.epoch);
      } else {
        this.publish({
          status: "restore_error",
          error: {
            code: isConnectivityFailure(error) ? "restore_unavailable" : "restore_failed",
            retryable: true,
          },
        });
      }
      return;
    }
    if (!this.current(boundary.epoch)) return;
    this.access = access;
    try {
      const account = await this.api.me();
      this.authenticate(boundary.epoch, account);
    } catch (error) {
      if (!this.current(boundary.epoch)) return;
      this.access = null;
      if (isTerminalAuthenticationFailure(error)) {
        await this.expire(boundary.epoch);
      } else {
        this.publish({
          status: "restore_error",
          error: {
            code: isConnectivityFailure(error) ? "restore_unavailable" : "restore_failed",
            retryable: true,
          },
        });
      }
    }
  }

  async signIn(username: string, password: string, signal?: AbortSignal): Promise<void> {
    const boundary = this.beginBoundary({ status: "signing_in" });
    await boundary.clearing;
    try {
      await this.credentials.clear();
    } catch {
      if (this.current(boundary.epoch)) {
        this.publish({
          status: "sign_in_error",
          error: { code: "credential_delete_failed", retryable: true },
        });
      }
      return;
    }
    if (!this.current(boundary.epoch)) return;
    let tokens: { access: string; refresh: string };
    try {
      tokens = await this.api.login(username, password, signal);
    } catch (error) {
      if (!this.current(boundary.epoch)) return;
      if (error instanceof ApiError && error.kind === "aborted") {
        this.publish({ status: "signed_out", reason: "initial" });
      } else {
        this.publish({
          status: "sign_in_error",
          error: { code: signInErrorCode(error), retryable: true },
        });
      }
      return;
    }
    if (!this.current(boundary.epoch)) {
      this.revokeAbandoned(tokens.refresh);
      return;
    }
    try {
      await this.credentials.write(tokens.refresh);
    } catch {
      if (this.current(boundary.epoch)) {
        this.publish({
          status: "sign_in_error",
          error: { code: "credential_write_failed", retryable: true },
        });
      }
      return;
    }
    if (!this.current(boundary.epoch)) {
      // The boundary that superseded this sign-in owns the stored credential.
      this.revokeAbandoned(tokens.refresh);
      return;
    }
    this.access = tokens.access;
    this.refresh = tokens.refresh;
    try {
      const account = await this.api.me(signal);
      this.authenticate(boundary.epoch, account);
    } catch (error) {
      if (!this.current(boundary.epoch)) return;
      this.access = null;
      if (isTerminalAuthenticationFailure(error)) {
        await this.expire(boundary.epoch);
      } else {
        this.publish({
          status: "restore_error",
          error: {
            code: isConnectivityFailure(error) ? "restore_unavailable" : "restore_failed",
            retryable: true,
          },
        });
      }
    }
  }

  refreshAfterUnauthorized(
    capturedEpoch: number,
    rejectedToken: string | null = null,
  ): Promise<string | null> {
    if (
      capturedEpoch !== this.epoch ||
      this.state.status !== "authenticated" ||
      this.refresh === null
    ) {
      return Promise.resolve(null);
    }
    if (this.refreshFlight?.epoch === capturedEpoch) return this.refreshFlight.promise;
    // A 401 for a token that an earlier refresh already replaced needs no new refresh.
    if (this.access !== null && this.access !== rejectedToken) return Promise.resolve(this.access);
    const operation = this.performRefresh(capturedEpoch).finally(() => {
      if (this.refreshFlight?.promise === operation) this.refreshFlight = null;
    });
    this.refreshFlight = { epoch: capturedEpoch, promise: operation };
    return operation;
  }

  private async performRefresh(capturedEpoch: number): Promise<string | null> {
    const refresh = this.refresh;
    if (refresh === null) return null;
    try {
      const access = await this.api.refresh(refresh);
      if (!this.current(capturedEpoch)) return null;
      this.access = access;
      return access;
    } catch (error) {
      if (this.current(capturedEpoch) && isTerminalAuthenticationFailure(error)) {
        await this.expire(capturedEpoch);
      }
      return null;
    }
  }

  /** Best-effort revocation of a token pair obtained by a superseded sign-in; never retained. */
  private revokeAbandoned(refresh: string): void {
    this.api.logout(refresh).catch(() => undefined);
  }

  private async expire(expectedEpoch: number): Promise<void> {
    if (!this.current(expectedEpoch)) return;
    const boundary = this.beginBoundary({ status: "signed_out", reason: "expired" });
    const [, clearResult] = await Promise.allSettled([boundary.clearing, this.credentials.clear()]);
    if (!this.current(boundary.epoch)) return;
    if (clearResult.status === "rejected") {
      this.publish({
        status: "signed_out",
        reason: "expired",
        notice: { code: "credential_delete_failed", retryable: true },
      });
    }
  }

  async logout(): Promise<LogoutResult> {
    const refresh = this.refresh;
    this.pendingReturnRoute = null;
    const boundary = this.beginBoundary({ status: "logging_out" });
    const remote = refresh ? this.api.logout(refresh) : Promise.resolve();
    const [remoteResult, clearResult] = await Promise.allSettled([
      remote,
      Promise.all([boundary.clearing, this.credentials.clear()]),
    ]);
    const result = {
      remoteRevoked: remoteResult.status === "fulfilled",
      credentialCleared: clearResult.status === "fulfilled",
    };
    if (!this.current(boundary.epoch)) return result;
    const notice = !result.credentialCleared
      ? { code: "credential_delete_failed" as const, retryable: true }
      : !result.remoteRevoked
        ? { code: "remote_logout_failed" as const, retryable: false }
        : undefined;
    this.publish({ status: "signed_out", reason: "logout", notice });
    return result;
  }

  async retryCredentialCleanup(): Promise<boolean> {
    const boundary = this.beginBoundary({ status: "logging_out" });
    await boundary.clearing;
    try {
      await this.credentials.clear();
    } catch {
      if (this.current(boundary.epoch)) {
        this.publish({
          status: "signed_out",
          reason: "logout",
          notice: { code: "credential_delete_failed", retryable: true },
        });
      }
      return false;
    }
    if (this.current(boundary.epoch)) {
      this.publish({ status: "signed_out", reason: "logout" });
    }
    return true;
  }
}
