/**
 * Wording for factual Story metadata. Every label names what the value is: a
 * Story's creation time is never its publication time, and a source count is
 * never "verified" or "independent".
 */

/** "1 source", "2 sources"; zero uses the plural. */
export function countLabel(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** A wire timestamp in the reader's locale and time zone. */
export function formatTimestamp(iso: string): string {
  return DATE_TIME.format(new Date(iso));
}

/**
 * The Story's publication window as reported by its member Articles. Story
 * creation time is not publication time, so a missing window stays missing.
 */
export function storyPublicationLabel(story: {
  first_published_at: string | null;
  last_published_at: string | null;
}): string | null {
  const latest = story.last_published_at ?? story.first_published_at;
  return latest === null ? null : `Latest publication ${formatTimestamp(latest)}`;
}
