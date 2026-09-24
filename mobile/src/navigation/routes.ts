import type { Href } from "expo-router";

/** Story IDs are positive decimal strings within PostgreSQL's bigint range. */
const STORY_ID = /^[1-9]\d{0,18}$/;
const MAX_STORY_ID = 9223372036854775807n;

export const FEED_HREF = "/" as Href;
export const SAVED_HREF = "/saved" as Href;

/** Route params arrive as strings (or arrays for repeated keys); anything else is invalid. */
export function parseStoryId(value: unknown): string | null {
  if (typeof value !== "string" || !STORY_ID.test(value)) return null;
  return BigInt(value) <= MAX_STORY_ID ? value : null;
}

/** Routes carry Story IDs only, never tokens or publisher URLs. */
export function storyHref(storyId: string): Href {
  return `/stories/${storyId}` as Href;
}

export function storySourcesHref(storyId: string): Href {
  return `/stories/${storyId}/sources` as Href;
}
