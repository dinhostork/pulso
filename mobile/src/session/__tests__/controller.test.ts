import { QueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/server-state/query";

import { createSessionRuntime } from "../runtime";
import type { RefreshTokenStore } from "../storage";
import type { SessionChange } from "../types";

const BASE = "https://api.example.com";
const EMPTY_FEED = { results: [], next_cursor: null, ordering: "story_created_desc_v1" };

type Reply = Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

class MemoryStore implements RefreshTokenStore {
  value: string | null = null;
  failRead = false;
  failWrite = false;
  failClear = false;
  writes: string[] = [];

  async read() {
    if (this.failRead) throw new Error("keychain unavailable");
    return this.value;
  }
  async write(refreshToken: string) {
    if (this.failWrite) throw new Error("keychain unavailable");
    this.writes.push(refreshToken);
    this.value = refreshToken;
  }
  async clear() {
    if (this.failClear) throw new Error("keychain unavailable");
    this.value = null;
  }
}

const ACCOUNTS: Record<string, { id: number; username: string; password: string }> = {
  reader: { id: 1, username: "reader", password: "secret-a" },
  other: { id: 2, username: "other", password: "secret-b" },
};

/**
 * A scripted fake of the ADR-0009 endpoints plus one protected product read.
 * Each route may be overridden to hold or fail a single response.
 */
function harness() {
  const store = new MemoryStore();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const validAccess = new Map<string, number>();
  const validRefresh = new Map<string, number>();
  const calls: { path: string; body: Record<string, string> | undefined; auth?: string }[] = [];
  let issued = 0;
  const overrides: Partial<Record<string, (() => Reply)[]>> = {};

  const routes: Record<string, (body: Record<string, string>, auth?: string) => Reply> = {
    "/api/auth/login": (body) => {
      const account = ACCOUNTS[body.username];
      if (!account || account.password !== body.password) {
        return json({ non_field_errors: ["Invalid credentials."] }, 400);
      }
      issued += 1;
      const access = `access-${account.id}-${issued}`;
      const refresh = `refresh-${account.id}-${issued}`;
      validAccess.set(access, account.id);
      validRefresh.set(refresh, account.id);
      return json({ access, refresh });
    },
    "/api/auth/refresh": (body) => {
      const owner = validRefresh.get(body.refresh);
      if (owner === undefined) return json({ detail: "Token is blacklisted" }, 401);
      issued += 1;
      const access = `access-${owner}-${issued}`;
      validAccess.set(access, owner);
      return json({ access });
    },
    "/api/auth/logout": (body) => {
      validRefresh.delete(body.refresh);
      return new Response(null, { status: 205 });
    },
    "/api/auth/me": (_body, auth) => {
      const owner = auth ? validAccess.get(auth) : undefined;
      if (owner === undefined) return json({ detail: "expired" }, 401);
      const account = Object.values(ACCOUNTS).find((row) => row.id === owner)!;
      return json({ id: account.id, username: account.username });
    },
    "/api/feed": (_body, auth) =>
      auth && validAccess.has(auth) ? json(EMPTY_FEED) : json({ detail: "expired" }, 401),
  };

  const fetch = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, string>) : undefined;
    const header = (init?.headers as Record<string, string> | undefined)?.Authorization;
    const auth = header?.replace(/^Bearer /, "");
    calls.push({ path, body, auth });
    const override = overrides[path]?.shift();
    if (override) return override();
    return routes[path](body ?? {}, auth);
  });

  const runtime = createSessionRuntime({
    baseUrl: BASE,
    fetch: fetch as typeof globalThis.fetch,
    queryClient,
    refreshTokenStore: store,
  });

  return {
    ...runtime,
    store,
    queryClient,
    calls,
    validAccess,
    validRefresh,
    /** Queue a one-shot response for the next request to `path`. */
    next(path: string, reply: () => Reply) {
      (overrides[path] ??= []).push(reply);
    },
    /** Expire every access token so product requests start returning 401. */
    expireAccess() {
      validAccess.clear();
    },
    count(path: string) {
      return calls.filter((call) => call.path === path).length;
    },
    /** Hold the next `path` response until the gate opens, then reply (default: real handler). */
    hold(path: string, reply?: () => Response) {
      const gate = deferred<void>();
      this.next(path, async () => {
        const call = calls.at(-1)!;
        await gate.promise;
        return reply ? reply() : routes[path](call.body ?? {}, call.auth);
      });
      return gate;
    },
  };
}

async function signedIn(username = "reader") {
  const h = harness();
  await h.controller.signIn(username, ACCOUNTS[username].password);
  expect(h.controller.snapshot()).toMatchObject({ status: "authenticated" });
  return h;
}

afterEach(() => jest.useRealTimers());

describe("cold restore", () => {
  it("restores through refresh then /me with only the refresh token stored", async () => {
    const h = harness();
    h.store.value = "refresh-1-0";
    h.validRefresh.set("refresh-1-0", 1);

    await h.controller.bootstrap();

    expect(h.calls.map((call) => call.path)).toEqual(["/api/auth/refresh", "/api/auth/me"]);
    expect(h.calls[1].auth).toMatch(/^access-1-/);
    expect(h.controller.snapshot()).toEqual({
      status: "authenticated",
      account: { id: "1", username: "reader" },
    });
    expect(h.store.writes).toEqual([]);
  });

  it("requires sign-in without any network call when no refresh token is stored", async () => {
    const h = harness();
    await h.controller.bootstrap();
    expect(h.controller.snapshot()).toEqual({ status: "signed_out", reason: "initial" });
    expect(h.calls).toEqual([]);
  });

  it.each([
    ["offline", () => Promise.reject(new TypeError("Network request failed"))],
    ["a 5xx", () => json({ detail: "unavailable" }, 503)],
  ])("keeps the refresh token and stays retryable when %s", async (_label, reply) => {
    const h = harness();
    h.store.value = "refresh-1-0";
    h.validRefresh.set("refresh-1-0", 1);
    h.next("/api/auth/refresh", reply);

    await h.controller.bootstrap();

    expect(h.controller.snapshot()).toEqual({
      status: "restore_error",
      error: { code: "restore_unavailable", retryable: true },
    });
    expect(h.store.value).toBe("refresh-1-0");

    await h.controller.bootstrap();
    expect(h.controller.snapshot()).toMatchObject({ status: "authenticated" });
  });

  it("signs out and clears the stored credential for an invalid or blacklisted refresh", async () => {
    const h = harness();
    h.store.value = "refresh-blacklisted";

    await h.controller.bootstrap();

    expect(h.controller.snapshot()).toEqual({ status: "signed_out", reason: "expired" });
    expect(h.store.value).toBeNull();
    expect(h.count("/api/auth/me")).toBe(0);
  });

  it("reports an unreadable secure store as a retryable state, not a spinner or fallback", async () => {
    const h = harness();
    h.store.failRead = true;

    await h.controller.bootstrap();

    expect(h.controller.snapshot()).toEqual({
      status: "restore_error",
      error: { code: "credential_read_failed", retryable: true },
    });
    expect(h.calls).toEqual([]);
  });
});

describe("sign-in", () => {
  it("stores only the refresh token and never retains the password", async () => {
    const h = await signedIn();
    expect(h.store.value).toMatch(/^refresh-1-/);
    expect(h.controller.accessToken()).toMatch(/^access-1-/);
    const retained = Object.values(h.controller).filter((value) => typeof value === "string");
    expect(retained).not.toContain(ACCOUNTS.reader.password);
    expect(h.store.writes).not.toContain(ACCOUNTS.reader.password);
  });

  it("reports unknown users and wrong passwords with one generic code", async () => {
    const h = harness();
    await h.controller.signIn("reader", "wrong");
    const wrongPassword = h.controller.snapshot();
    await h.controller.signIn("nobody", "wrong");
    expect(h.controller.snapshot()).toEqual(wrongPassword);
    expect(wrongPassword).toEqual({
      status: "sign_in_error",
      error: { code: "invalid_credentials", retryable: true },
    });
    expect(h.store.value).toBeNull();
  });

  it("separates an unreachable server from invalid credentials", async () => {
    const h = harness();
    h.next("/api/auth/login", () => json({ detail: "down" }, 502));
    await h.controller.signIn("reader", "secret-a");
    expect(h.controller.snapshot()).toMatchObject({ error: { code: "sign_in_unavailable" } });
  });

  it("does not authenticate when the secure store cannot persist the session", async () => {
    const h = harness();
    h.store.failWrite = true;
    await h.controller.signIn("reader", "secret-a");
    expect(h.controller.snapshot()).toEqual({
      status: "sign_in_error",
      error: { code: "credential_write_failed", retryable: true },
    });
    expect(h.controller.accessToken()).toBeNull();
  });
});

describe("refresh and replay", () => {
  it("shares one refresh across three parallel expired requests and replays each once", async () => {
    const h = await signedIn();
    h.expireAccess();
    const gate = h.hold("/api/auth/refresh");

    const requests = [h.api.feed(), h.api.feed(), h.api.feed()];
    await new Promise((resolve) => setTimeout(resolve, 0));
    gate.resolve();

    await expect(Promise.all(requests)).resolves.toHaveLength(3);
    expect(h.count("/api/auth/refresh")).toBe(1);
    expect(h.count("/api/feed")).toBe(6);
  });

  it("reuses an already refreshed token for a late 401 instead of refreshing again", async () => {
    const h = await signedIn();
    const stale = h.controller.accessToken();
    h.expireAccess();
    await h.api.feed();
    expect(h.count("/api/auth/refresh")).toBe(1);

    const current = h.controller.accessToken();
    await expect(
      h.controller.refreshAfterUnauthorized(h.controller.sessionEpoch(), stale),
    ).resolves.toBe(current);
    expect(h.count("/api/auth/refresh")).toBe(1);
  });

  it("does not replay twice when the replay is also rejected", async () => {
    const h = await signedIn();
    h.expireAccess();
    h.next("/api/feed", () => json({ detail: "expired" }, 401));
    h.next("/api/feed", () => json({ detail: "still rejected" }, 401));

    await expect(h.api.feed()).rejects.toMatchObject({ kind: "http", status: 401 });
    expect(h.count("/api/feed")).toBe(2);
    expect(h.count("/api/auth/refresh")).toBe(1);
    expect(h.controller.snapshot()).toMatchObject({ status: "authenticated" });
  });

  it("signs out after a terminal refresh rejection and clears account state", async () => {
    const h = await signedIn();
    h.queryClient.setQueryData(queryKeys.feed("1"), EMPTY_FEED);
    h.expireAccess();
    h.validRefresh.clear();

    await expect(h.api.feed()).rejects.toMatchObject({ kind: "stale_session" });
    expect(h.controller.snapshot()).toEqual({ status: "signed_out", reason: "expired" });
    expect(h.store.value).toBeNull();
    expect(h.queryClient.getQueryData(queryKeys.feed("1"))).toBeUndefined();
  });

  it("keeps the session when refresh fails transiently", async () => {
    const h = await signedIn();
    h.expireAccess();
    h.next("/api/auth/refresh", () => json({ detail: "down" }, 503));

    await expect(h.api.feed()).rejects.toMatchObject({ kind: "http", status: 401 });
    expect(h.controller.snapshot()).toMatchObject({ status: "authenticated" });
    expect(h.store.value).toMatch(/^refresh-1-/);
  });
});

describe("logout", () => {
  it("revokes remotely, clears credentials and removes the account's cache", async () => {
    const h = await signedIn();
    const refresh = h.store.value;
    h.queryClient.setQueryData(queryKeys.feed("1"), EMPTY_FEED);

    await expect(h.controller.logout()).resolves.toEqual({
      remoteRevoked: true,
      credentialCleared: true,
    });

    expect(h.calls.at(-1)).toMatchObject({ path: "/api/auth/logout", body: { refresh } });
    expect(h.controller.snapshot()).toEqual({ status: "signed_out", reason: "logout" });
    expect(h.store.value).toBeNull();
    expect(h.controller.accessToken()).toBeNull();
    expect(h.queryClient.getQueryData(queryKeys.feed("1"))).toBeUndefined();
  });

  it("still clears everything locally and reports it when remote revocation fails", async () => {
    const h = await signedIn();
    h.next("/api/auth/logout", () => Promise.reject(new TypeError("Network request failed")));

    await expect(h.controller.logout()).resolves.toEqual({
      remoteRevoked: false,
      credentialCleared: true,
    });
    expect(h.controller.snapshot()).toEqual({
      status: "signed_out",
      reason: "logout",
      notice: { code: "remote_logout_failed", retryable: false },
    });
    expect(h.store.value).toBeNull();
  });

  it("surfaces a secure-store delete failure and lets cleanup be retried", async () => {
    const h = await signedIn();
    h.store.failClear = true;

    await expect(h.controller.logout()).resolves.toMatchObject({ credentialCleared: false });
    expect(h.controller.snapshot()).toMatchObject({
      notice: { code: "credential_delete_failed", retryable: true },
    });

    h.store.failClear = false;
    await expect(h.controller.retryCredentialCleanup()).resolves.toBe(true);
    expect(h.store.value).toBeNull();
    expect(h.controller.snapshot()).toEqual({ status: "signed_out", reason: "logout" });
  });

  it("discards a refresh that completes after logout", async () => {
    const h = await signedIn();
    h.expireAccess();
    const gate = h.hold("/api/auth/refresh", () => json({ access: "access-1-late" }));
    const request = h.api.feed();
    await new Promise((resolve) => setTimeout(resolve, 0));

    await h.controller.logout();
    gate.resolve();

    await expect(request).rejects.toMatchObject({ kind: "stale_session" });
    expect(h.controller.accessToken()).toBeNull();
    expect(h.controller.snapshot()).toMatchObject({ status: "signed_out", reason: "logout" });
    expect(h.count("/api/feed")).toBe(1);
  });
});

describe("account isolation", () => {
  it("ignores a login that completes after logout and revokes its tokens", async () => {
    const h = harness();
    const gate = h.hold("/api/auth/login");
    const signIn = h.controller.signIn("reader", "secret-a");
    await new Promise((resolve) => setTimeout(resolve, 0));

    await h.controller.logout();
    gate.resolve();
    await signIn;

    expect(h.controller.snapshot()).toMatchObject({ status: "signed_out", reason: "logout" });
    expect(h.controller.accessToken()).toBeNull();
    expect(h.store.value).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(h.calls.at(-1)?.path).toBe("/api/auth/logout");
    expect(h.validRefresh.size).toBe(0);
  });

  it("keeps account B when A's refresh completes after logout and B's login", async () => {
    const h = await signedIn("reader");
    h.expireAccess();
    const gate = h.hold("/api/auth/refresh", () => json({ access: "access-1-late" }));
    const lateRequest = h.api.feed();
    await new Promise((resolve) => setTimeout(resolve, 0));

    await h.controller.logout();
    await h.controller.signIn("other", "secret-b");
    const accessB = h.controller.accessToken();
    gate.resolve();

    await expect(lateRequest).rejects.toMatchObject({ kind: "stale_session" });
    expect(h.controller.accessToken()).toBe(accessB);
    expect(h.controller.snapshot()).toEqual({
      status: "authenticated",
      account: { id: "2", username: "other" },
    });
  });

  it("rejects A's late query response and never repopulates A's cache under B", async () => {
    const h = await signedIn("reader");
    const directGate = h.hold("/api/feed");
    const direct = h.api.feed();
    const queryGate = h.hold("/api/feed");
    const cached = h.queryClient
      .fetchQuery({
        queryKey: queryKeys.feed("1"),
        queryFn: ({ signal }) => h.api.feed({ signal }),
      })
      .catch((error: unknown) => error);
    await new Promise((resolve) => setTimeout(resolve, 0));

    await h.controller.logout();
    await h.controller.signIn("other", "secret-b");
    h.queryClient.setQueryData(queryKeys.feed("2"), EMPTY_FEED);
    directGate.resolve();
    queryGate.resolve();

    await expect(direct).rejects.toMatchObject({ kind: "stale_session" });
    await cached;
    expect(h.queryClient.getQueryData(queryKeys.feed("1"))).toBeUndefined();
    expect(h.queryClient.getQueryData(queryKeys.feed("2"))).toEqual(EMPTY_FEED);
  });

  it("publishes account changes for exposure delivery without credentials", async () => {
    const h = harness();
    const changes: SessionChange[] = [];
    h.controller.subscribeSessionChanges((change) => changes.push(change));

    await h.controller.signIn("reader", "secret-a");
    await h.controller.logout();
    await h.controller.signIn("other", "secret-b");

    expect(changes.map((change) => change.accountId)).toEqual([null, "1", null, null, "2"]);
    const epochs = changes.map((change) => change.epoch);
    expect(epochs).toEqual([...epochs].sort((a, b) => a - b));
    expect(JSON.stringify(changes)).not.toMatch(/access-|refresh-|secret/);
  });
});
