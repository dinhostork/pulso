import type { Transport } from "@/api/transport";

import type { SessionAccount } from "./types";

interface TokenPair {
  access: string;
  refresh: string;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError("expected an object");
  }
  return value as Record<string, unknown>;
}

function credential(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) throw new TypeError("expected a token");
  return value;
}

function decodeTokenPair(value: unknown): TokenPair {
  const row = object(value);
  return { access: credential(row.access), refresh: credential(row.refresh) };
}

function decodeAccess(value: unknown): string {
  return credential(object(value).access);
}

function decodeAccount(value: unknown): SessionAccount {
  const row = object(value);
  const id =
    typeof row.id === "number" && Number.isSafeInteger(row.id) && row.id > 0
      ? String(row.id)
      : typeof row.id === "string" && /^[1-9]\d*$/.test(row.id)
        ? row.id
        : null;
  if (id === null || typeof row.username !== "string" || row.username.length === 0) {
    throw new TypeError("expected an account");
  }
  return { id, username: row.username };
}

export class SessionApi {
  constructor(private readonly transport: Transport) {}

  login(username: string, password: string, signal?: AbortSignal): Promise<TokenPair> {
    return this.transport.request({
      operation: "session.login",
      path: "/api/auth/login",
      method: "POST",
      body: { username, password },
      authenticated: false,
      signal,
      decode: decodeTokenPair,
    });
  }

  refresh(refresh: string): Promise<string> {
    return this.transport.request({
      operation: "session.refresh",
      path: "/api/auth/refresh",
      method: "POST",
      body: { refresh },
      authenticated: false,
      decode: decodeAccess,
    });
  }

  me(signal?: AbortSignal): Promise<SessionAccount> {
    return this.transport.request({
      operation: "session.me",
      path: "/api/auth/me",
      signal,
      decode: decodeAccount,
    });
  }

  logout(refresh: string): Promise<void> {
    return this.transport.request({
      operation: "session.logout",
      path: "/api/auth/logout",
      method: "POST",
      body: { refresh },
      authenticated: false,
    });
  }
}
