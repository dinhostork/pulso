export interface SessionAccount {
  id: string;
  username: string;
}

export type SessionErrorCode =
  | "invalid_credentials"
  | "sign_in_unavailable"
  | "credential_read_failed"
  | "credential_write_failed"
  | "credential_delete_failed"
  | "restore_unavailable"
  | "restore_failed"
  | "remote_logout_failed";

export interface SessionNotice {
  code: SessionErrorCode;
  retryable: boolean;
}

export type SessionState =
  | { status: "cold" }
  | { status: "restoring" }
  | { status: "signed_out"; reason: "initial" | "logout" | "expired"; notice?: SessionNotice }
  | { status: "signing_in" }
  | { status: "authenticated"; account: SessionAccount }
  | { status: "restore_error"; error: SessionNotice }
  | { status: "sign_in_error"; error: SessionNotice }
  | { status: "logging_out" };

export interface SessionChange {
  epoch: number;
  accountId: string | null;
}

export interface LogoutResult {
  remoteRevoked: boolean;
  credentialCleared: boolean;
}
