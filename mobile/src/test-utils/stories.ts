/**
 * Test-only builders for wire-shaped Story payloads (they pass the real
 * decoders). Production code never imports this file.
 */

export interface StoryOptions {
  title?: string;
  summary?: string | null;
  context?: string | null;
  topics?: string[];
  articles?: number;
  sources?: number;
  firstPublishedAt?: string | null;
  lastPublishedAt?: string | null;
  bookmarked?: boolean;
  /** Article IDs cited by every element; defaults to one derived from the Story ID. */
  citedArticleIds?: string[];
}

export function storyCard(id: string, options: StoryOptions = {}) {
  const title = options.title ?? `Story ${id} headline`;
  const cited = options.citedArticleIds ?? [`${id}01`];
  const elements: unknown[] = [
    { id: `${id}1`, kind: "TITLE", position: 0, text: title, article_ids: cited },
  ];
  const summary = options.summary === undefined ? `Story ${id} summary.` : options.summary;
  if (summary !== null) {
    elements.push({
      id: `${id}2`,
      kind: "SUMMARY",
      position: 0,
      text: summary,
      article_ids: cited,
    });
  }
  if (options.context) {
    elements.push({
      id: `${id}3`,
      kind: "CONTEXT",
      position: 0,
      text: options.context,
      article_ids: cited,
    });
  }
  return {
    id,
    language: "en",
    created_at: "2026-09-19T10:00:00Z",
    first_published_at:
      options.firstPublishedAt === undefined ? "2026-09-19T08:15:00Z" : options.firstPublishedAt,
    last_published_at:
      options.lastPublishedAt === undefined ? "2026-09-19T09:45:00Z" : options.lastPublishedAt,
    content_state: "CURRENT",
    synthesis_id: `${id}9`,
    synthesized_at: "2026-09-19T10:05:00Z",
    title,
    elements,
    topics: (options.topics ?? []).map((label) => ({ slug: label.toLowerCase(), label })),
    article_count: options.articles ?? 1,
    source_count: options.sources ?? 1,
    viewer: { bookmarked: options.bookmarked ?? false },
  };
}

export function feedPage(results: unknown[], nextCursor: string | null = null) {
  return { ordering: "story_created_desc_v1", results, next_cursor: nextCursor };
}

export interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

export interface ArticleOptions {
  title?: string;
  publisher?: string;
  sourceId?: string;
  url?: string | null;
  publishedAt?: string | null;
  firstSeenAt?: string;
  byline?: string | null;
  current?: boolean;
}

export function sourceArticle(id: string, options: ArticleOptions = {}) {
  const sourceId = options.sourceId ?? "21";
  return {
    id,
    title: options.title ?? `Article ${id} headline`,
    canonical_url:
      options.url === undefined
        ? `https://publisher${sourceId}.example/articles/${id}`
        : options.url,
    source: {
      id: sourceId,
      name: options.publisher ?? `Publisher ${sourceId}`,
      slug: `publisher-${sourceId}`,
    },
    published_at: options.publishedAt === undefined ? "2026-09-19T08:15:00Z" : options.publishedAt,
    first_seen_at: options.firstSeenAt ?? "2026-09-19T08:20:00Z",
    byline: options.byline ?? null,
    duplicate_of_id: null,
    is_current_member: options.current ?? true,
  };
}

export interface DetailOptions extends StoryOptions {
  entities?: { kind: string; display_name: string }[];
  currentArticles?: number;
  currentSources?: number;
  /** Citation rows by Article ID; missing cited IDs get a default current row. */
  citations?: Record<string, ReturnType<typeof sourceArticle>>;
}

export function storyDetail(id: string, options: DetailOptions = {}) {
  const card = storyCard(id, options);
  const cited = new Set(
    (card.elements as { article_ids: string[] }[]).flatMap((element) => element.article_ids),
  );
  const citations: Record<string, ReturnType<typeof sourceArticle>> = {};
  for (const articleId of cited) {
    citations[articleId] = options.citations?.[articleId] ?? sourceArticle(articleId);
  }
  return {
    ...card,
    entities: options.entities ?? [],
    current_article_count: options.currentArticles ?? card.article_count,
    current_source_count: options.currentSources ?? card.source_count,
    citations,
    sources_path: `/api/stories/${id}/sources?synthesis_id=${card.synthesis_id}`,
  };
}

export function sourcesPage(results: unknown[], nextCursor: string | null = null) {
  return { results, next_cursor: nextCursor };
}
