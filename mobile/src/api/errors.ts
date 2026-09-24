export type ApiErrorKind =
  | "configuration"
  | "timeout"
  | "aborted"
  | "network"
  | "http"
  | "malformed_json"
  | "malformed_dto"
  | "stale_session";

export type ApiFieldErrors = Readonly<Record<string, readonly string[]>>;

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly operation: string;
  readonly status?: number;
  readonly code?: string;
  readonly detail?: string;
  readonly fields?: ApiFieldErrors;
  readonly retryAfterSeconds?: number;

  constructor(
    kind: ApiErrorKind,
    operation: string,
    options: {
      status?: number;
      code?: string;
      detail?: string;
      fields?: ApiFieldErrors;
      retryAfterSeconds?: number;
      cause?: unknown;
    } = {},
  ) {
    super(`${operation} failed (${kind})`, { cause: options.cause });
    this.name = "ApiError";
    this.kind = kind;
    this.operation = operation;
    this.status = options.status;
    this.code = options.code;
    this.detail = options.detail;
    this.fields = options.fields;
    this.retryAfterSeconds = options.retryAfterSeconds;
  }
}

export class DecodeError extends Error {
  constructor(
    readonly path: string,
    message: string,
  ) {
    super(`${path}: ${message}`);
    this.name = "DecodeError";
  }
}
