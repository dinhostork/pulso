import { DecodeError } from "./errors";
import type {
  BookmarkResult,
  ContentState,
  Entity,
  FeedImpressionResponse,
  FeedPage,
  Page,
  SavedEntry,
  SourceArticle,
  StoryCard,
  StoryDetail,
  StoryElement,
  Topic,
} from "./types";

type JsonObject = Record<string, unknown>;
type Decoder<T> = (value: unknown, path: string) => T;

const DECIMAL_ID = /^[1-9]\d*$/;
const CONTENT_STATES = new Set<ContentState>(["CURRENT", "UPDATING", "PREPARING"]);
const ELEMENT_KINDS = new Set(["TITLE", "SUMMARY", "CONTEXT"] as const);
const ENTITY_KINDS = new Set(["PERSON", "ORGANIZATION", "PLACE", "OTHER"] as const);

function object(value: unknown, path: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DecodeError(path, "expected an object");
  }
  return value as JsonObject;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string") throw new DecodeError(path, "expected a string");
  return value;
}

function nonemptyString(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!decoded) throw new DecodeError(path, "must not be empty");
  return decoded;
}

function id(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!DECIMAL_ID.test(decoded)) throw new DecodeError(path, "expected a decimal-string ID");
  return decoded;
}

function nullable<T>(value: unknown, path: string, decode: Decoder<T>): T | null {
  return value === null ? null : decode(value, path);
}

function integer(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new DecodeError(path, "expected a nonnegative safe integer");
  }
  return value;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(path, "expected a boolean");
  return value;
}

function timestamp(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!/^\d{4}-\d{2}-\d{2}T/.test(decoded) || !Number.isFinite(Date.parse(decoded))) {
    throw new DecodeError(path, "expected an ISO-8601 timestamp");
  }
  return decoded;
}

function array<T>(value: unknown, path: string, decode: Decoder<T>, maximum = 50): T[] {
  if (!Array.isArray(value)) throw new DecodeError(path, "expected an array");
  if (value.length > maximum) throw new DecodeError(path, `exceeds maximum length ${maximum}`);
  return value.map((item, index) => decode(item, `${path}[${index}]`));
}

function enumValue<T extends string>(value: unknown, path: string, allowed: ReadonlySet<T>): T {
  const decoded = string(value, path) as T;
  if (!allowed.has(decoded)) throw new DecodeError(path, "has an unsupported value");
  return decoded;
}

function relativeApiPath(value: unknown, path: string): string {
  const decoded = string(value, path);
  if (!decoded.startsWith("/api/") || decoded.startsWith("//") || /[\u0000-\u001f]/.test(decoded)) {
    throw new DecodeError(path, "expected an approved relative API path");
  }
  return decoded;
}

function externalHttpUrl(value: unknown, path: string): string {
  const decoded = string(value, path);
  let parsed: URL;
  try {
    parsed = new URL(decoded);
  } catch {
    throw new DecodeError(path, "expected a valid HTTP(S) URL");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password
  ) {
    throw new DecodeError(path, "expected a credential-free HTTP(S) URL");
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    host === "localhost" ||
    host === "::" ||
    host === "::1" ||
    /^f[cd][0-9a-f]{2}:/.test(host) ||
    host.startsWith("fe80:") ||
    /^(127\.|10\.|169\.254\.|192\.168\.)/.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host)
  ) {
    throw new DecodeError(path, "literal local/private destinations are not allowed");
  }
  return decoded;
}

function decodeElement(value: unknown, path = "element"): StoryElement {
  const row = object(value, path);
  const decoded = {
    id: id(row.id, `${path}.id`),
    kind: enumValue(row.kind, `${path}.kind`, ELEMENT_KINDS),
    position: integer(row.position, `${path}.position`),
    text: nonemptyString(row.text, `${path}.text`),
    article_ids: array(row.article_ids, `${path}.article_ids`, id, 50),
  };
  if (decoded.article_ids.length === 0) {
    throw new DecodeError(`${path}.article_ids`, "must contain citation provenance");
  }
  return decoded;
}

function decodeTopic(value: unknown, path = "topic"): Topic {
  const row = object(value, path);
  return {
    slug: nonemptyString(row.slug, `${path}.slug`),
    label: nonemptyString(row.label, `${path}.label`),
  };
}

function decodeEntity(value: unknown, path = "entity"): Entity {
  const row = object(value, path);
  return {
    kind: enumValue(row.kind, `${path}.kind`, ENTITY_KINDS),
    display_name: nonemptyString(row.display_name, `${path}.display_name`),
  };
}

export function decodeSourceArticle(value: unknown, path = "source_article"): SourceArticle {
  const row = object(value, path);
  const source = object(row.source, `${path}.source`);
  return {
    id: id(row.id, `${path}.id`),
    title: nonemptyString(row.title, `${path}.title`),
    canonical_url: externalHttpUrl(row.canonical_url, `${path}.canonical_url`),
    source: {
      id: id(source.id, `${path}.source.id`),
      name: nonemptyString(source.name, `${path}.source.name`),
      slug: nonemptyString(source.slug, `${path}.source.slug`),
    },
    published_at: nullable(row.published_at, `${path}.published_at`, timestamp),
    first_seen_at: timestamp(row.first_seen_at, `${path}.first_seen_at`),
    byline: nullable(row.byline, `${path}.byline`, string),
    duplicate_of_id: nullable(row.duplicate_of_id, `${path}.duplicate_of_id`, id),
    is_current_member: boolean(row.is_current_member, `${path}.is_current_member`),
  };
}

function storyBase(value: unknown, path: string): StoryCard {
  const row = object(value, path);
  const viewer = object(row.viewer, `${path}.viewer`);
  const decoded: StoryCard = {
    id: id(row.id, `${path}.id`),
    language: nonemptyString(row.language, `${path}.language`),
    created_at: timestamp(row.created_at, `${path}.created_at`),
    first_published_at: nullable(row.first_published_at, `${path}.first_published_at`, timestamp),
    last_published_at: nullable(row.last_published_at, `${path}.last_published_at`, timestamp),
    content_state: enumValue(row.content_state, `${path}.content_state`, CONTENT_STATES),
    synthesis_id: nullable(row.synthesis_id, `${path}.synthesis_id`, id),
    synthesized_at: nullable(row.synthesized_at, `${path}.synthesized_at`, timestamp),
    title: nonemptyString(row.title, `${path}.title`),
    elements: array(row.elements, `${path}.elements`, decodeElement, 20),
    topics: array(row.topics, `${path}.topics`, decodeTopic, 8),
    article_count: integer(row.article_count, `${path}.article_count`),
    source_count: integer(row.source_count, `${path}.source_count`),
    viewer: { bookmarked: boolean(viewer.bookmarked, `${path}.viewer.bookmarked`) },
  };
  const order = { TITLE: 0, SUMMARY: 1, CONTEXT: 2 } as const;
  let previousOrder = -1;
  const positions = new Map<string, number>();
  for (const element of decoded.elements) {
    const currentOrder = order[element.kind];
    const expectedPosition = positions.get(element.kind) ?? 0;
    if (currentOrder < previousOrder || element.position !== expectedPosition) {
      throw new DecodeError(`${path}.elements`, "are not in deterministic kind/position order");
    }
    previousOrder = currentOrder;
    positions.set(element.kind, expectedPosition + 1);
  }
  if (decoded.content_state === "PREPARING") {
    if (
      decoded.synthesis_id !== null ||
      decoded.synthesized_at !== null ||
      decoded.elements.length !== 0
    ) {
      throw new DecodeError(path, "PREPARING must not contain synthesis data");
    }
  } else {
    const titles = decoded.elements.filter((element) => element.kind === "TITLE");
    if (
      decoded.synthesis_id === null ||
      decoded.synthesized_at === null ||
      titles.length !== 1 ||
      titles[0].text !== decoded.title
    ) {
      throw new DecodeError(path, "published content requires one matching cited title");
    }
  }
  return decoded;
}

export function decodeStoryCard(value: unknown, path = "story"): StoryCard {
  return storyBase(value, path);
}

export function decodeStoryDetail(value: unknown, path = "story"): StoryDetail {
  const row = object(value, path);
  const citations = object(row.citations, `${path}.citations`);
  const decodedCitations: Record<string, SourceArticle> = {};
  for (const [articleId, citation] of Object.entries(citations)) {
    if (!DECIMAL_ID.test(articleId)) throw new DecodeError(`${path}.citations`, "has a non-ID key");
    const decoded = decodeSourceArticle(citation, `${path}.citations.${articleId}`);
    if (decoded.id !== articleId)
      throw new DecodeError(`${path}.citations.${articleId}.id`, "does not match its key");
    decodedCitations[articleId] = decoded;
  }
  const base = storyBase(row, path);
  const citedIds = new Set(base.elements.flatMap((element) => element.article_ids));
  if (
    citedIds.size !== Object.keys(decodedCitations).length ||
    [...citedIds].some((articleId) => !(articleId in decodedCitations))
  ) {
    throw new DecodeError(`${path}.citations`, "must exactly cover returned element citations");
  }
  return {
    ...base,
    entities: array(row.entities, `${path}.entities`, decodeEntity, 20),
    current_article_count: integer(row.current_article_count, `${path}.current_article_count`),
    current_source_count: integer(row.current_source_count, `${path}.current_source_count`),
    citations: decodedCitations,
    sources_path: relativeApiPath(row.sources_path, `${path}.sources_path`),
  };
}

function page<T>(value: unknown, path: string, decode: Decoder<T>): Page<T> {
  const row = object(value, path);
  return {
    results: array(row.results, `${path}.results`, decode, 50),
    next_cursor: nullable(row.next_cursor, `${path}.next_cursor`, string),
  };
}

export function decodeFeedPage(value: unknown, path = "feed"): FeedPage {
  const row = object(value, path);
  const decoded = page(row, path, decodeStoryCard);
  if (row.ordering !== "story_created_desc_v1") {
    throw new DecodeError(`${path}.ordering`, "has an unsupported value");
  }
  return { ...decoded, ordering: "story_created_desc_v1" };
}

export function decodeSourcePage(value: unknown, path = "sources"): Page<SourceArticle> {
  return page(value, path, decodeSourceArticle);
}

export function decodeBookmarkResult(value: unknown, path = "bookmark"): BookmarkResult {
  const row = object(value, path);
  if (row.bookmarked !== true) throw new DecodeError(`${path}.bookmarked`, "expected true");
  return {
    story_id: id(row.story_id, `${path}.story_id`),
    bookmarked: true,
    saved_at: timestamp(row.saved_at, `${path}.saved_at`),
  };
}

function decodeSavedEntry(value: unknown, path = "saved_entry"): SavedEntry {
  const row = object(value, path);
  const base = {
    story_id: id(row.story_id, `${path}.story_id`),
    saved_at: timestamp(row.saved_at, `${path}.saved_at`),
  };
  if (row.availability === "UNAVAILABLE") return { ...base, availability: "UNAVAILABLE" };
  if (row.availability === "AVAILABLE") {
    const story = decodeStoryCard(row.story, `${path}.story`);
    if (story.id !== base.story_id) {
      throw new DecodeError(`${path}.story.id`, "does not match story_id");
    }
    return { ...base, availability: "AVAILABLE", story };
  }
  throw new DecodeError(`${path}.availability`, "has an unsupported value");
}

export function decodeSavedPage(value: unknown, path = "bookmarks"): Page<SavedEntry> {
  return page(value, path, decodeSavedEntry);
}

export function decodeFeedImpressionResponse(
  value: unknown,
  path = "feed_impressions",
): FeedImpressionResponse {
  const row = object(value, path);
  return {
    results: array(
      row.results,
      `${path}.results`,
      (item, itemPath) => {
        const result = object(item, itemPath);
        const outcome = enumValue(
          result.outcome,
          `${itemPath}.outcome`,
          new Set(["accepted", "duplicate", "rejected"] as const),
        );
        return {
          event_id: nonemptyString(result.event_id, `${itemPath}.event_id`),
          outcome,
          code: nullable(result.code, `${itemPath}.code`, string),
        };
      },
      20,
    ),
  };
}
