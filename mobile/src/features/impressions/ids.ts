import * as Crypto from "expo-crypto";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/**
 * A version 4 UUID from the platform's cryptographically secure generator
 * (`expo-crypto`). There is deliberately no `Math.random` fallback: if the
 * secure generator is unavailable this throws and no event is produced.
 */
export function secureUuid(): string {
  const value: unknown = Crypto.randomUUID();
  const normalized = typeof value === "string" ? value.toLowerCase() : "";
  if (!UUID_V4.test(normalized)) throw new Error("Secure UUID generation is unavailable");
  return normalized;
}
