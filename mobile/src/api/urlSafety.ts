/**
 * The publisher-link rule shared by the SourceArticle decoder and the external
 * navigation seam: an absolute HTTP(S) URL with a host, no credentials, no
 * control characters, and no literal loopback, private, link-local or
 * unspecified address. No DNS lookup is made; a hostname is taken as given.
 */
export function isSafePublisherUrl(value: string): boolean {
  if (/[\u0000-\u001f\u007f\s\\]/.test(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password
  ) {
    return false;
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return !(
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "::" ||
    host === "::1" ||
    host.startsWith("::ffff:") ||
    /^f[cd][0-9a-f]{2}:/.test(host) ||
    host.startsWith("fe80:") ||
    /^(0\.|127\.|10\.|169\.254\.|192\.168\.)/.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host)
  );
}
