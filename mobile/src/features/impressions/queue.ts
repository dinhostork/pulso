import { ApiError } from "@/api/errors";
import type { FeedImpressionEvent, FeedImpressionResponse } from "@/api/types";
import type { SessionChange } from "@/session/types";

import { DELIVERY_POLICY, type DeliveryPolicy } from "./policy";

export interface ImpressionQueueDeps {
  /** One POST /api/feed-impressions; it must not retry on its own. */
  send(events: FeedImpressionEvent[], signal: AbortSignal): Promise<FeedImpressionResponse>;
  now(): number;
  policy?: DeliveryPolicy;
}

/** Content-free delivery counters: no event, session or Story identifiers. */
export interface ImpressionDiagnostics {
  queued: number;
  acknowledged: number;
  duplicate: number;
  rejected: Record<string, number>;
  dropped: {
    overflow: number;
    expired: number;
    retry_exhausted: number;
    terminal: number;
    stale_session: number;
  };
  requests: number;
}

interface Entry {
  /** Frozen at enqueue: every retry sends exactly this payload. */
  readonly payload: Readonly<FeedImpressionEvent>;
  readonly enqueuedAt: number;
  /** After the first attempt, only `notBefore` (backoff/Retry-After) delays a resend. */
  attempted: boolean;
  retries: number;
  notBefore: number;
}

function emptyDiagnostics(): ImpressionDiagnostics {
  return {
    queued: 0,
    acknowledged: 0,
    duplicate: 0,
    rejected: {},
    dropped: { overflow: 0, expired: 0, retry_exhausted: 0, terminal: 0, stale_session: 0 },
    requests: 0,
  };
}

function isRetryable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.kind === "network" || error.kind === "timeout") return true;
  return error.kind === "http" && (error.status === 401 || (error.status ?? 0) >= 500);
}

/**
 * The bounded in-memory FeedImpression delivery queue. It alone owns
 * impression retry: the transport performs only its single 401 refresh/replay,
 * and no query-library mutation is involved. It belongs to one session epoch —
 * a session change clears events, timers and the in-flight request, and a late
 * response from the previous account is ignored. Nothing is persisted, so
 * process death loses queued events by design.
 */
export class ImpressionQueue {
  private entries: Entry[] = [];
  private epoch = 0;
  private active = false;
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inFlight: AbortController | null = null;
  private counters = emptyDiagnostics();
  private readonly policy: DeliveryPolicy;

  constructor(private readonly deps: ImpressionQueueDeps) {
    this.policy = deps.policy ?? DELIVERY_POLICY;
  }

  /** Account boundary: called for every session change published by the session controller. */
  onSessionChange(change: SessionChange): void {
    this.generation += 1;
    this.inFlight?.abort();
    this.inFlight = null;
    this.clearTimer();
    this.entries = [];
    this.epoch = change.epoch;
    this.active = change.accountId !== null;
  }

  /** The epoch a producer must present when enqueueing. */
  currentEpoch(): number {
    return this.epoch;
  }

  diagnostics(): ImpressionDiagnostics {
    return {
      ...this.counters,
      rejected: { ...this.counters.rejected },
      dropped: { ...this.counters.dropped },
    };
  }

  size(): number {
    return this.entries.length;
  }

  /** Accepts an event from `epoch`'s producer; returns false when it is dropped. */
  enqueue(event: FeedImpressionEvent, epoch: number): boolean {
    if (!this.active || epoch !== this.epoch) {
      this.counters.dropped.stale_session += 1;
      return false;
    }
    if (this.entries.length >= this.policy.maxQueued) {
      this.counters.dropped.overflow += 1;
      return false;
    }
    const now = this.deps.now();
    this.entries.push({
      payload: Object.freeze({ ...event }),
      enqueuedAt: now,
      attempted: false,
      retries: 0,
      notBefore: now,
    });
    this.counters.queued += 1;
    this.schedule();
    return true;
  }

  /** Best-effort immediate send (leaving the Feed or backgrounding); never waits. */
  flushNow(): void {
    if (this.inFlight === null) void this.flush();
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private dropExpired(now: number): void {
    const kept = this.entries.filter((entry) => now - entry.enqueuedAt < this.policy.ttlMs);
    this.counters.dropped.expired += this.entries.length - kept.length;
    this.entries = kept;
  }

  /** Next send: now for a full ready batch, else the oldest event's interval or retry time. */
  private schedule(): void {
    this.clearTimer();
    if (!this.active || this.inFlight !== null || this.entries.length === 0) return;
    const now = this.deps.now();
    const ready = this.entries.filter((entry) => entry.notBefore <= now).length;
    let at = Infinity;
    if (ready >= this.policy.batchSize) at = now;
    for (const entry of this.entries) {
      const due = entry.attempted
        ? entry.notBefore
        : Math.max(entry.notBefore, entry.enqueuedAt + this.policy.flushIntervalMs);
      at = Math.min(at, due, entry.enqueuedAt + this.policy.ttlMs);
    }
    this.timer = setTimeout(
      () => {
        this.timer = null;
        void this.flush();
      },
      Math.max(0, at - now),
    );
  }

  private async flush(): Promise<void> {
    if (!this.active || this.inFlight !== null) return;
    this.clearTimer();
    const now = this.deps.now();
    this.dropExpired(now);
    const batch = this.entries
      .filter((entry) => entry.notBefore <= now)
      .slice(0, this.policy.batchSize);
    if (batch.length === 0) {
      this.schedule();
      return;
    }
    for (const entry of batch) entry.attempted = true;
    const generation = this.generation;
    const controller = new AbortController();
    this.inFlight = controller;
    this.counters.requests += 1;
    try {
      const response = await this.deps.send(
        batch.map((entry) => entry.payload as FeedImpressionEvent),
        controller.signal,
      );
      if (generation !== this.generation) return;
      this.acknowledge(batch, response);
    } catch (error) {
      if (generation !== this.generation) return;
      this.fail(batch, error);
    } finally {
      if (generation === this.generation) {
        this.inFlight = null;
        this.schedule();
      }
    }
  }

  private remove(entries: Set<Entry>): void {
    this.entries = this.entries.filter((entry) => !entries.has(entry));
  }

  /** Per-event outcomes: only resolved events leave; an unmentioned one is retried with backoff. */
  private acknowledge(batch: Entry[], response: FeedImpressionResponse): void {
    const outcomes = new Map(response.results.map((result) => [result.event_id, result]));
    const resolved = new Set<Entry>();
    const unresolved: Entry[] = [];
    for (const entry of batch) {
      const result = outcomes.get(entry.payload.event_id);
      if (!result) {
        unresolved.push(entry);
        continue;
      }
      resolved.add(entry);
      if (result.outcome === "accepted") this.counters.acknowledged += 1;
      else if (result.outcome === "duplicate") this.counters.duplicate += 1;
      else {
        const code = result.code ?? "unknown";
        this.counters.rejected[code] = (this.counters.rejected[code] ?? 0) + 1;
      }
    }
    this.remove(resolved);
    this.retryLater(unresolved);
  }

  /** Counts one retry per event; past the cap the event is dropped, otherwise it backs off. */
  private retryLater(entries: Entry[]): void {
    const now = this.deps.now();
    const exhausted = new Set<Entry>();
    for (const entry of entries) {
      entry.retries += 1;
      if (entry.retries > this.policy.maxRetries) exhausted.add(entry);
      else entry.notBefore = now + this.policy.retryBaseMs * 2 ** (entry.retries - 1);
    }
    this.counters.dropped.retry_exhausted += exhausted.size;
    this.remove(exhausted);
  }

  private fail(batch: Entry[], error: unknown): void {
    const now = this.deps.now();
    if (error instanceof ApiError && (error.kind === "stale_session" || error.kind === "aborted")) {
      return;
    }
    if (error instanceof ApiError && error.kind === "http" && error.status === 429) {
      const wait =
        error.retryAfterSeconds !== undefined
          ? error.retryAfterSeconds * 1000
          : this.policy.defaultRetryAfterMs;
      // Waiting past the TTL simply lets the events expire; 429 never retries forever.
      for (const entry of batch) entry.notBefore = now + wait;
      return;
    }
    if (!isRetryable(error)) {
      this.counters.dropped.terminal += batch.length;
      this.remove(new Set(batch));
      return;
    }
    this.retryLater(batch);
  }
}
