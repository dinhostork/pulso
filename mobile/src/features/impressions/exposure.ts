import type { CardVisibility } from "@/features/feed/visibility";

import { secureUuid } from "./ids";
import { EXPOSURE_POLICY, POLICY_VERSION, type ExposurePolicy } from "./policy";
import { ExposureQualifier, type QualifiedExposure } from "./qualifier";
import type { ImpressionQueue } from "./queue";

export interface FeedExposureDeps {
  queue: ImpressionQueue;
  now?: () => number;
  uuid?: () => string;
  policy?: ExposurePolicy;
}

/**
 * The HOME_FEED adapter around the pure qualifier: it owns the feed session
 * ID, the one dwell timer and the hand-off of qualified exposures to the
 * delivery queue. Rendering never waits on it, and any failure here (for
 * example no secure UUID source) drops the exposure instead of reaching the
 * Feed.
 */
export class FeedExposureController {
  private readonly qualifier: ExposureQualifier;
  private readonly now: () => number;
  private readonly uuid: () => string;
  private sessionId: string | null = null;
  private epoch = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly deps: FeedExposureDeps) {
    this.qualifier = new ExposureQualifier(deps.policy ?? EXPOSURE_POLICY);
    this.now = deps.now ?? (() => Date.now());
    this.uuid = deps.uuid ?? secureUuid;
  }

  /** The current feed session, for tests and diagnostics; never logged. */
  feedSessionId(): string | null {
    return this.sessionId;
  }

  /**
   * Starts a feed session: on Feed initialization for an account and after a
   * successful explicit refresh. Exposures of the previous session are
   * forgotten, so a Story may qualify again.
   */
  startSession(): void {
    // Exposures that matured under the previous session keep its ID.
    this.deliver(this.qualifier.tick(this.now()));
    this.qualifier.reset(this.now());
    this.epoch = this.deps.queue.currentEpoch();
    try {
      this.sessionId = this.uuid();
    } catch {
      this.sessionId = null;
    }
    this.schedule();
  }

  /** Open only while the Feed route is focused and the app is in the foreground. */
  setGate(open: boolean): void {
    this.deliver(this.qualifier.setGate(open, this.now()));
  }

  readonly observe = (cards: CardVisibility[]): void => {
    this.deliver(this.qualifier.observe(cards, this.now()));
  };

  /** Unmount: cancels the timer and ends the session. */
  dispose(): void {
    this.clearTimer();
    this.qualifier.setGate(false, this.now());
    this.sessionId = null;
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(): void {
    this.clearTimer();
    const deadline = this.qualifier.nextDeadline();
    if (deadline === null) return;
    this.timer = setTimeout(
      () => {
        this.timer = null;
        this.deliver(this.qualifier.tick(this.now()));
      },
      Math.max(0, deadline - this.now()),
    );
  }

  private deliver(exposures: QualifiedExposure[]): void {
    const sessionId = this.sessionId;
    for (const exposure of exposures) {
      if (sessionId === null) break;
      let eventId: string;
      try {
        eventId = this.uuid();
      } catch {
        continue;
      }
      this.deps.queue.enqueue(
        {
          event_id: eventId,
          story_id: exposure.storyId,
          feed_session_id: sessionId,
          position: exposure.position,
          surface: "HOME_FEED",
          policy_version: POLICY_VERSION,
          occurred_at: new Date(exposure.occurredAt).toISOString(),
        },
        this.epoch,
      );
    }
    this.schedule();
  }
}
