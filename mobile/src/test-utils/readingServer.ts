/**
 * Test-only stateful fake of the Reading API behind the scripted transport:
 * Feed, Story detail/sources, per-account Bookmarks with keyset-like Saved
 * pages, and FeedImpression intake. Payloads come from the wire builders, so
 * they pass the real decoders. Production code never imports this file.
 */
import type { FeedImpressionEvent } from "@/api/types";

import { json, type ProductHandler, type ProductRequest } from "./readingApp";
import {
  deferred,
  feedPage,
  sourceArticle,
  sourcesPage,
  storyDetail,
  type Deferred,
} from "./stories";

type Card = ReturnType<typeof storyDetail>;

/** The StoryCard subset of a detail payload, as Feed and Saved pages carry it. */
const CARD_FIELDS = [
  "id",
  "language",
  "created_at",
  "first_published_at",
  "last_published_at",
  "content_state",
  "synthesis_id",
  "synthesized_at",
  "title",
  "elements",
  "topics",
  "article_count",
  "source_count",
  "viewer",
] as const;

/**
 * What the next matching request does. `commit_then_lose`: the server applies
 * the write, then the response never arrives (a lost/timed-out response).
 */
export type Fault =
  | { kind: "status"; status: number; code?: string; commit?: boolean }
  | { kind: "network"; commit: boolean }
  | { kind: "hold"; gate: Deferred<void>; snapshot?: boolean };

interface SavedRow {
  storyId: string;
  savedAt: string;
  seq: number;
}

export interface ReadingServerOptions {
  /** Feed Stories in server order; each is also readable as a detail. */
  stories?: Card[];
  /** Stories absent from the Feed whose detail returns 410 (archived/empty). */
  unavailable?: string[];
  feedPageSize?: number;
  savedPageSize?: number;
  /** Bookmarks that already exist, per username, newest first. */
  saved?: Record<string, string[]>;
  /** A Story's source page; by default one generated current source. */
  sources?: Record<string, unknown>;
}

export function readingServer(options: ReadingServerOptions = {}) {
  const stories = new Map((options.stories ?? []).map((story) => [story.id, story]));
  const feedOrder = (options.stories ?? [])
    .filter((story) => story.content_state === "CURRENT")
    .map((story) => story.id);
  const unavailable = new Set(options.unavailable ?? []);
  const feedPageSize = options.feedPageSize ?? 20;
  const savedPageSize = options.savedPageSize ?? 20;
  const bookmarks = new Map<string, SavedRow[]>();
  let seq = 0;
  const savedAt = () => new Date(Date.UTC(2026, 8, 20, 10, 0, seq)).toISOString();

  for (const [user, ids] of Object.entries(options.saved ?? {})) {
    for (const storyId of [...ids].reverse()) {
      seq += 1;
      rows(user).unshift({ storyId, savedAt: savedAt(), seq });
    }
  }

  function rows(user: string): SavedRow[] {
    if (!bookmarks.has(user)) bookmarks.set(user, []);
    return bookmarks.get(user)!;
  }

  const faults: { method: string; path: RegExp; fault: Fault }[] = [];
  const impressions: { user: string | null; events: FeedImpressionEvent[] }[] = [];
  /** Bookmark writes that reached the server, with the account their token named. */
  const writes: { method: string; storyId: string; user: string | null }[] = [];

  function decorated(story: Card, user: string | null) {
    const bookmarked = user !== null && rows(user).some((row) => row.storyId === story.id);
    return { ...story, viewer: { bookmarked } };
  }

  function card(story: Card, user: string | null) {
    const full = decorated(story, user);
    return Object.fromEntries(CARD_FIELDS.map((field) => [field, full[field]]));
  }

  function page<T>(items: T[], cursor: string | null, size: number) {
    const start = cursor === null ? 0 : Number(cursor.replace("c", ""));
    const results = items.slice(start, start + size);
    const next = start + size < items.length ? `c${start + size}` : null;
    return { results, next };
  }

  function apply(request: ProductRequest): Response {
    const { method, path, user } = request;
    const cursor = request.query.get("cursor");
    if (method === "GET" && path === "/api/feed") {
      const ids = page(feedOrder, cursor, feedPageSize);
      return json(
        feedPage(
          ids.results.map((id) => card(stories.get(id)!, user)),
          ids.next,
        ),
      );
    }
    if (method === "GET" && path === "/api/bookmarks") {
      const ordered = [...rows(user!)].sort((a, b) => b.seq - a.seq);
      const slice = page(ordered, cursor, savedPageSize);
      return json({
        results: slice.results.map((row) => {
          const story = stories.get(row.storyId);
          return story && !unavailable.has(row.storyId)
            ? {
                story_id: row.storyId,
                saved_at: row.savedAt,
                availability: "AVAILABLE",
                story: card(story, user),
              }
            : { story_id: row.storyId, saved_at: row.savedAt, availability: "UNAVAILABLE" };
        }),
        next_cursor: slice.next,
      });
    }
    const bookmark = /^\/api\/bookmarks\/(\d+)$/.exec(path);
    if (bookmark) {
      const storyId = bookmark[1];
      const list = rows(user!);
      const existing = list.find((row) => row.storyId === storyId);
      if (method === "DELETE") {
        if (existing) list.splice(list.indexOf(existing), 1);
        return new Response(null, { status: 204 });
      }
      if (existing) {
        return json({ story_id: storyId, bookmarked: true, saved_at: existing.savedAt });
      }
      if (!stories.has(storyId) && !unavailable.has(storyId)) {
        return json({ code: "story_not_found", detail: "The Story was not found." }, 404);
      }
      if (unavailable.has(storyId)) {
        return json(
          { code: "story_unavailable", detail: "This Story is no longer available." },
          410,
        );
      }
      seq += 1;
      const row = { storyId, savedAt: savedAt(), seq };
      list.push(row);
      return json({ story_id: storyId, bookmarked: true, saved_at: row.savedAt });
    }
    const detail = /^\/api\/stories\/(\d+)(\/sources)?$/.exec(path);
    if (detail) {
      const [, storyId, sources] = detail;
      if (unavailable.has(storyId)) {
        return json(
          { code: "story_unavailable", detail: "This Story is no longer available." },
          410,
        );
      }
      const story = stories.get(storyId);
      if (!story) return json({ code: "story_not_found", detail: "The Story was not found." }, 404);
      if (!sources) return json(decorated(story, user));
      return json(options.sources?.[storyId] ?? sourcesPage([sourceArticle(`${storyId}01`)]));
    }
    if (method === "POST" && path === "/api/feed-impressions") {
      const events = (request.body as { events: FeedImpressionEvent[] }).events;
      impressions.push({ user, events });
      return json({
        results: events.map((event) => ({
          event_id: event.event_id,
          outcome: "accepted",
          code: null,
        })),
      });
    }
    return json({ code: "not_found", detail: "Not found." }, 404);
  }

  const handler: ProductHandler = async (request) => {
    const bookmarkWrite = /^\/api\/bookmarks\/(\d+)$/.exec(request.path);
    if (bookmarkWrite) {
      writes.push({ method: request.method, storyId: bookmarkWrite[1], user: request.user });
    }
    const index = faults.findIndex(
      (entry) => entry.method === request.method && entry.path.test(request.path),
    );
    if (index === -1) return apply(request);
    const [{ fault }] = faults.splice(index, 1);
    if (fault.kind === "hold") {
      // `snapshot`: answer from the state at arrival, delivered later (a slow response).
      const early = fault.snapshot ? apply(request) : null;
      await fault.gate.promise;
      return early ?? apply(request);
    }
    if (fault.kind === "network") {
      if (fault.commit) apply(request);
      throw new TypeError("Network request failed");
    }
    if (fault.commit) apply(request);
    return json({ code: fault.code ?? "server_error", detail: "Failed." }, fault.status);
  };

  return {
    handler,
    writes,
    impressions,
    /** The next `method` request whose path matches `path` suffers `fault`. */
    fail(method: string, path: RegExp, fault: Fault) {
      faults.push({ method, path, fault });
    },
    /**
     * The next matching request waits until the returned gate opens. With
     * `snapshot`, its response reflects the server state when it arrived.
     */
    hold(method: string, path: RegExp, options: { snapshot?: boolean } = {}): Deferred<void> {
      const gate = deferred<void>();
      faults.push({ method, path, fault: { kind: "hold", gate, snapshot: options.snapshot } });
      return gate;
    },
    /** A write from another device of the same account, outside this app. */
    async elsewhere(method: "PUT" | "DELETE", storyId: string, user = "reader") {
      await handler({
        method,
        path: `/api/bookmarks/${storyId}`,
        query: new URLSearchParams(),
        body: undefined,
        user,
      });
    },
    savedIds(user: string): string[] {
      return [...rows(user)].sort((a, b) => b.seq - a.seq).map((row) => row.storyId);
    },
    archive(storyId: string) {
      unavailable.add(storyId);
      const index = feedOrder.indexOf(storyId);
      if (index !== -1) feedOrder.splice(index, 1);
    },
  };
}

export type ReadingServer = ReturnType<typeof readingServer>;
