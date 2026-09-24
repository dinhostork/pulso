/**
 * The Feed's visibility instrumentation seam. It turns list geometry into
 * per-card visible shares and nothing else: no timers, no identifiers, no
 * delivery. Fetching, receiving a page, mounting or rendering a card never
 * produces a report by itself; only a laid-out card inside a measured viewport
 * has a nonzero share. Exposure qualification consumes these reports (#51).
 */

/** The scrolled window, in list-content coordinates. */
export interface Viewport {
  offset: number;
  height: number;
}

/** A laid-out card, in list-content coordinates. */
export interface CardLayout {
  top: number;
  height: number;
}

export interface CardVisibility {
  storyId: string;
  /** Zero-based absolute position in the rendered Feed order, not the viewport index. */
  position: number;
  /** 0–1; see `visibleShare`. */
  share: number;
}

/**
 * Visible height divided by the smaller of the card and viewport heights. A
 * card taller than the screen (long text, large accessibility type) counts as
 * fully visible when it fills the viewport, so tall cards are not excluded by
 * geometry alone.
 */
export function visibleShare(card: CardLayout, viewport: Viewport): number {
  if (card.height <= 0 || viewport.height <= 0) return 0;
  const top = Math.max(card.top, viewport.offset);
  const bottom = Math.min(card.top + card.height, viewport.offset + viewport.height);
  if (bottom <= top) return 0;
  return Math.min(1, (bottom - top) / Math.min(card.height, viewport.height));
}

/**
 * Collects card layouts and the viewport and reports every card with a
 * nonzero share whenever the geometry changes. A card that unmounts (or is
 * recycled for another Story) is removed and stops being reported.
 */
export class FeedVisibilityTracker {
  private viewport: Viewport | null = null;
  private readonly cards = new Map<string, { position: number; layout: CardLayout }>();

  constructor(private readonly listener: (cards: CardVisibility[]) => void) {}

  setViewport(viewport: Viewport): void {
    this.viewport = viewport;
    this.emit();
  }

  setCard(storyId: string, position: number, layout: CardLayout): void {
    this.cards.set(storyId, { position, layout });
    this.emit();
  }

  setPosition(storyId: string, position: number): void {
    const card = this.cards.get(storyId);
    if (!card || card.position === position) return;
    card.position = position;
    this.emit();
  }

  removeCard(storyId: string): void {
    if (this.cards.delete(storyId)) this.emit();
  }

  snapshot(): CardVisibility[] {
    const viewport = this.viewport;
    if (viewport === null) return [];
    const visible: CardVisibility[] = [];
    for (const [storyId, card] of this.cards) {
      const share = visibleShare(card.layout, viewport);
      if (share > 0) visible.push({ storyId, position: card.position, share });
    }
    return visible.sort((a, b) => a.position - b.position);
  }

  private emit(): void {
    this.listener(this.snapshot());
  }
}
