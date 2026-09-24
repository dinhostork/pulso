/**
 * FeedImpression client policy, version 1 (ADR-0012, issue #51).
 *
 * Qualification: a HOME_FEED Story card is exposed when at least half of it
 * is visible — measured against the smaller of card and viewport height, so a
 * card taller than the screen (long text, large accessibility type) qualifies
 * by filling the viewport — for one continuous second while the Feed route is
 * focused and the app is in the foreground. This mirrors the common
 * "50% for one continuous second" viewability convention, is long enough that
 * rapid scrolling and fast taps into a Story never qualify, and short enough
 * that a reader who pauses on a card does.
 *
 * Delivery: bounded, in memory only, and best effort. The batch bound equals
 * the server's 1–20 event limit (#43), and a maximal batch is about 5 KiB of
 * the server's 32 KiB body limit. One flush per 5 s is at most 12 requests a
 * minute, a fifth of the server's advisory 60/min throttle, leaving room for
 * leave/background flushes and retries. The 10-minute TTL keeps every retried
 * or Retry-After-delayed event far inside the server's 24-hour occurrence
 * window. Three retries (2 s, 4 s, 8 s backoff) ride out brief outages without
 * outliving the TTL. A full queue is five batches of small fixed-shape events,
 * so memory stays bounded and a backlog drains in five requests.
 *
 * Changing any qualification value changes what an event means and requires a
 * new `POLICY_VERSION`; delivery bounds can change without one.
 */
export const POLICY_VERSION = 1 as const;

export const EXPOSURE_POLICY = {
  /** Minimum visible share (0–1) of a card; exactly reaching it qualifies. */
  visibleShare: 0.5,
  /** Continuous milliseconds at or above the share; exactly reaching it qualifies. */
  dwellMs: 1000,
} as const;

export type ExposurePolicy = { visibleShare: number; dwellMs: number };

export const DELIVERY_POLICY = {
  /** Queued events, including those in flight; later events overflow. */
  maxQueued: 100,
  /** Events per request; equals the server's maximum batch. */
  batchSize: 20,
  /** Age after which an undelivered event is dropped. */
  ttlMs: 10 * 60 * 1000,
  /** A partial batch is sent this long after its oldest event was queued. */
  flushIntervalMs: 5000,
  /** Retries after the first attempt for network, timeout, 401 and 5xx failures. */
  maxRetries: 3,
  /** Backoff before retry n (1-based): base × 2^(n-1). */
  retryBaseMs: 2000,
  /** Wait after a 429 without a usable Retry-After header. */
  defaultRetryAfterMs: 30_000,
} as const;

export type DeliveryPolicy = { [K in keyof typeof DELIVERY_POLICY]: number };
