import type { CardVisibility } from "@/features/feed/visibility";

import type { ExposurePolicy } from "./policy";

/** A Story card that met the exposure policy; `occurredAt` is the qualifying instant (epoch ms). */
export interface QualifiedExposure {
  storyId: string;
  position: number;
  occurredAt: number;
}

/**
 * The exposure qualification state machine. It is pure: callers pass the
 * current time into every transition and schedule their own timer at
 * `nextDeadline()`, so it has no clock, timer, identifier or network.
 *
 * A card is a candidate while its visible share is at or above the policy
 * share and the gate (route focused and app in the foreground) is open. It
 * qualifies once it has been a candidate continuously for the dwell time.
 * Dropping below the share, disappearing from the report (unmounted or
 * recycled), a gate close or a new session resets its timer. A Story
 * qualifies at most once per session.
 */
export class ExposureQualifier {
  private gateOpen = false;
  private visible: readonly CardVisibility[] = [];
  private readonly candidates = new Map<string, { since: number; position: number }>();
  private readonly exposed = new Set<string>();

  constructor(private readonly policy: ExposurePolicy) {}

  /**
   * Starts a new feed session: no candidate or earlier exposure carries over,
   * and cards visible right now start a fresh dwell from `now`.
   */
  reset(now: number): void {
    this.candidates.clear();
    this.exposed.clear();
    this.sync(now);
  }

  setGate(open: boolean, now: number): QualifiedExposure[] {
    const qualified = this.settle(now);
    this.gateOpen = open;
    if (open) this.sync(now);
    else this.candidates.clear();
    return qualified;
  }

  /** The latest visibility report; cards absent from it are not visible. */
  observe(cards: readonly CardVisibility[], now: number): QualifiedExposure[] {
    const qualified = this.settle(now);
    this.visible = cards;
    this.sync(now);
    return qualified;
  }

  tick(now: number): QualifiedExposure[] {
    return this.settle(now);
  }

  /** When the earliest candidate qualifies if nothing changes, or null. */
  nextDeadline(): number | null {
    let earliest: number | null = null;
    for (const { since } of this.candidates.values()) {
      const deadline = since + this.policy.dwellMs;
      if (earliest === null || deadline < earliest) earliest = deadline;
    }
    return earliest;
  }

  /** Candidates that stayed eligible until `now` for the whole dwell time qualify. */
  private settle(now: number): QualifiedExposure[] {
    const qualified: QualifiedExposure[] = [];
    for (const [storyId, candidate] of this.candidates) {
      if (now - candidate.since < this.policy.dwellMs) continue;
      this.candidates.delete(storyId);
      this.exposed.add(storyId);
      qualified.push({
        storyId,
        position: candidate.position,
        occurredAt: candidate.since + this.policy.dwellMs,
      });
    }
    return qualified.sort((a, b) => a.position - b.position);
  }

  private sync(now: number): void {
    if (!this.gateOpen) return;
    const eligible = new Map<string, number>();
    for (const card of this.visible) {
      if (card.share >= this.policy.visibleShare && !this.exposed.has(card.storyId)) {
        eligible.set(card.storyId, card.position);
      }
    }
    for (const storyId of [...this.candidates.keys()]) {
      if (!eligible.has(storyId)) this.candidates.delete(storyId);
    }
    for (const [storyId, position] of eligible) {
      const candidate = this.candidates.get(storyId);
      if (candidate) candidate.position = position;
      else this.candidates.set(storyId, { since: now, position });
    }
  }
}
