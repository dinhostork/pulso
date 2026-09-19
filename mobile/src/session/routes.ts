const PROTECTED_RETURN_ROUTE = /^\/stories\/[1-9]\d*(?:\/sources)?$/;

export function validatedPendingReturnRoute(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 256) return null;
  if (
    value.startsWith("//") ||
    value.includes(":") ||
    value.includes("\\") ||
    value.includes("..") ||
    /%2f|%5c|%2e/i.test(value)
  ) {
    return null;
  }
  return PROTECTED_RETURN_ROUTE.test(value) ? value : null;
}
