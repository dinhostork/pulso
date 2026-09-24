export type ContentState = "CURRENT" | "UPDATING" | "PREPARING";
export type ElementKind = "TITLE" | "SUMMARY" | "CONTEXT";

export interface ViewerMetadata {
  bookmarked: boolean;
}

export interface StoryElement {
  id: string;
  kind: ElementKind;
  position: number;
  text: string;
  article_ids: string[];
}

export interface Topic {
  slug: string;
  label: string;
}

export interface Entity {
  kind: "PERSON" | "ORGANIZATION" | "PLACE" | "OTHER";
  display_name: string;
}

export interface SourceIdentity {
  id: string;
  name: string;
  slug: string;
}

export interface SourceArticle {
  id: string;
  title: string;
  /** Null when the server withheld an unsafe stored link; the publication stays attributable. */
  canonical_url: string | null;
  source: SourceIdentity;
  published_at: string | null;
  first_seen_at: string;
  byline: string | null;
  duplicate_of_id: string | null;
  is_current_member: boolean;
}

export interface StoryCard {
  id: string;
  language: string;
  created_at: string;
  first_published_at: string | null;
  last_published_at: string | null;
  content_state: ContentState;
  synthesis_id: string | null;
  synthesized_at: string | null;
  title: string;
  elements: StoryElement[];
  topics: Topic[];
  article_count: number;
  source_count: number;
  viewer: ViewerMetadata;
}

export interface StoryDetail extends StoryCard {
  entities: Entity[];
  current_article_count: number;
  current_source_count: number;
  citations: Record<string, SourceArticle>;
  sources_path: string;
}

export interface Page<T> {
  results: T[];
  next_cursor: string | null;
}

export interface FeedPage extends Page<StoryCard> {
  ordering: "story_created_desc_v1";
}

export interface BookmarkResult {
  story_id: string;
  bookmarked: true;
  saved_at: string;
}

export interface SavedUnavailableEntry {
  story_id: string;
  saved_at: string;
  availability: "UNAVAILABLE";
}

export interface SavedAvailableEntry {
  story_id: string;
  saved_at: string;
  availability: "AVAILABLE";
  story: StoryCard;
}

export type SavedEntry = SavedAvailableEntry | SavedUnavailableEntry;

export interface FeedImpressionEvent {
  event_id: string;
  story_id: string;
  feed_session_id: string;
  position: number;
  surface: "HOME_FEED";
  policy_version: 1;
  occurred_at: string;
}

export interface FeedImpressionOutcome {
  event_id: string;
  outcome: "accepted" | "duplicate" | "rejected";
  code: string | null;
}

export interface FeedImpressionResponse {
  results: FeedImpressionOutcome[];
}
