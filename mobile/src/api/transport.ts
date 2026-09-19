import { apiBaseUrl } from "@/config/env";

import { ApiError, DecodeError, type ApiFieldErrors } from "./errors";

const DEFAULT_TIMEOUT_MS = 15_000;
const APPROVED_PATH = /^\/api\/[A-Za-z0-9_~./?=&%+-]*$/;

export interface CredentialHooks {
  accessToken(): string | null;
  sessionEpoch(): number;
  refreshAfterUnauthorized?(capturedEpoch: number): Promise<string | null>;
}

export interface TransportRequest<T> {
  operation: string;
  path: string;
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  authenticated?: boolean;
  decode?: (value: unknown) => T;
}

export interface Transport {
  request<T>(request: TransportRequest<T>): Promise<T>;
}

export interface TransportOptions {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
  credentials?: CredentialHooks;
  allowInsecureHttp?: boolean;
}

function normalizedOrigin(baseUrl: string, allowInsecureHttp: boolean): string {
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch (cause) {
    throw new ApiError("configuration", "configure_api_origin", { cause });
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.pathname !== "/" && parsed.pathname !== "") ||
    (parsed.protocol === "http:" && !allowInsecureHttp)
  ) {
    throw new ApiError("configuration", "configure_api_origin");
  }
  return parsed.origin;
}

export function buildApiUrl(baseUrl: string, path: string, allowInsecureHttp = false): string {
  if (
    !APPROVED_PATH.test(path) ||
    path.startsWith("//") ||
    path.includes("\\") ||
    path.includes("..") ||
    /%2e/i.test(path)
  ) {
    throw new ApiError("configuration", "build_api_url");
  }
  return `${normalizedOrigin(baseUrl, allowInsecureHttp)}${path}`;
}

function retryAfter(response: Response): number | undefined {
  const raw = response.headers.get("Retry-After");
  if (raw === null || !/^\d+$/.test(raw)) return undefined;
  return Number(raw);
}

function fieldErrors(value: unknown): ApiFieldErrors | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const result: Record<string, string[]> = {};
  for (const [key, messages] of Object.entries(value)) {
    if (!Array.isArray(messages) || !messages.every((message) => typeof message === "string")) {
      return undefined;
    }
    result[key] = messages.slice(0, 10);
  }
  return result;
}

function normalizedHttpError(response: Response, value: unknown, operation: string): ApiError {
  const body =
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  return new ApiError("http", operation, {
    status: response.status,
    code: typeof body.code === "string" ? body.code : undefined,
    detail: typeof body.detail === "string" ? body.detail : undefined,
    fields: fieldErrors(body.fields),
    retryAfterSeconds: retryAfter(response),
  });
}

function composeAbort(caller: AbortSignal | undefined, timeoutMs: number) {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(caller?.reason);
  caller?.addEventListener("abort", abortFromCaller, { once: true });
  if (caller?.aborted) abortFromCaller();
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    cleanup: () => {
      clearTimeout(timeout);
      caller?.removeEventListener("abort", abortFromCaller);
    },
  };
}

async function responseBody(response: Response, operation: string): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch (cause) {
    if (!response.ok) return undefined;
    throw new ApiError("malformed_json", operation, { status: response.status, cause });
  }
}

export function createTransport(options: TransportOptions = {}): Transport {
  const fetchImplementation = options.fetch ?? globalThis.fetch;
  const origin = options.baseUrl ?? apiBaseUrl;
  const allowInsecureHttp = options.allowInsecureHttp ?? __DEV__;

  async function request<T>(definition: TransportRequest<T>, replayed = false): Promise<T> {
    const method = definition.method ?? "GET";
    const authenticated = definition.authenticated ?? true;
    const capturedEpoch = options.credentials?.sessionEpoch() ?? 0;
    const token = authenticated ? options.credentials?.accessToken() : null;
    const headers: Record<string, string> = { Accept: "application/json" };
    if (definition.body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = `Bearer ${token}`;
    const url = buildApiUrl(origin, definition.path, allowInsecureHttp);
    const abort = composeAbort(definition.signal, definition.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    let response: Response;
    try {
      response = await fetchImplementation(url, {
        method,
        headers,
        body: definition.body === undefined ? undefined : JSON.stringify(definition.body),
        signal: abort.signal,
      });
    } catch (cause) {
      if (abort.timedOut()) throw new ApiError("timeout", definition.operation, { cause });
      if (definition.signal?.aborted)
        throw new ApiError("aborted", definition.operation, { cause });
      throw new ApiError("network", definition.operation, { cause });
    } finally {
      abort.cleanup();
    }

    if (
      response.status === 401 &&
      authenticated &&
      !replayed &&
      options.credentials?.refreshAfterUnauthorized
    ) {
      const refreshed = await options.credentials.refreshAfterUnauthorized(capturedEpoch);
      if (refreshed && options.credentials.sessionEpoch() === capturedEpoch) {
        return request(definition, true);
      }
    }

    const body = await responseBody(response, definition.operation);
    if (!response.ok) throw normalizedHttpError(response, body, definition.operation);
    if (!definition.decode) return body as T;
    try {
      return definition.decode(body);
    } catch (cause) {
      if (cause instanceof ApiError) throw cause;
      throw new ApiError("malformed_dto", definition.operation, {
        cause: cause instanceof DecodeError ? cause : undefined,
      });
    }
  }

  return { request };
}
