import { useRouter } from "expo-router";
import { useState, type ReactNode } from "react";
import { ActivityIndicator, FlatList, StyleSheet, View } from "react-native";

import { ApiError } from "@/api/errors";
import type { SourceArticle, StoryDetail, StoryElement } from "@/api/types";
import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { Screen } from "@/components/Screen";
import { SourceRow } from "@/components/SourceRow";
import { ErrorState, LoadingState, StatusLabel } from "@/components/StatusState";
import { openPublisherUrl, type PublisherOpenResult } from "@/navigation/external";
import { FEED_HREF, parseStoryId, storySourcesHref } from "@/navigation/routes";
import { useAccountId } from "@/session/SessionProvider";
import { spacing, useTheme } from "@/theme";

import {
  articlesFromSources,
  articleTimeLabel,
  formatTimestamp,
  storyPublicationLabel,
  synthesisLabel,
} from "./format";
import { useStoryDetail, useStorySources } from "./useStory";

const EDGES = ["bottom", "left", "right"] as const;

const ENTITY_KIND = {
  PERSON: "Person",
  ORGANIZATION: "Organization",
  PLACE: "Place",
  OTHER: "Other",
} as const;

const OPEN_ERROR: Record<Exclude<PublisherOpenResult, "opened">, string> = {
  rejected: "This link cannot be opened safely, so Pulso did not open it.",
  failed: "The publication could not be opened on this device. Try again.",
};

/** Links open the publication's current address; no historical copy is kept. */
const LINK_NOTE =
  "Links open each publication on its publisher's site as it is now; Pulso does not keep copies of earlier versions.";

function useGoBack() {
  const router = useRouter();
  return () => (router.canGoBack() ? router.back() : router.replace(FEED_HREF));
}

function InvalidStoryLink() {
  const router = useRouter();
  return (
    <Screen edges={[...EDGES]}>
      <ErrorState
        message="This Story link is not valid."
        primary={{ label: "Go to Feed", onPress: () => router.replace(FEED_HREF) }}
        title="Story not found"
      />
    </Screen>
  );
}

/** 404/410 get a recoverable back action; other failures a retry. No obsolete facts are shown. */
function StoryLoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const goBack = useGoBack();
  const router = useRouter();
  const status = error instanceof ApiError ? error.status : undefined;
  if (status === 404 || status === 410) {
    return (
      <ErrorState
        message={
          status === 404
            ? "This Story does not exist. The link may be out of date."
            : "This Story is no longer available in Pulso."
        }
        primary={{ label: "Go back", onPress: goBack }}
        secondary={{ label: "Go to Feed", onPress: () => router.replace(FEED_HREF) }}
        title={status === 404 ? "Story not found" : "Story unavailable"}
      />
    );
  }
  return (
    <ErrorState
      message="The Story could not be loaded. Check your connection and try again."
      primary={{ label: "Try again", onPress: onRetry }}
      secondary={{ label: "Go back", onPress: goBack }}
      title="Story unavailable right now"
    />
  );
}

/** One publication with its OS-browser hand-off and a visible failure category. */
function PublicationRow({ article, testID }: { article: SourceArticle; testID?: string }) {
  const [openError, setOpenError] = useState<string | null>(null);
  const url = article.canonical_url;
  const notes = [
    ...(article.byline ? [`By ${article.byline}`] : []),
    ...(article.is_current_member ? [] : ["No longer among this Story's current sources"]),
  ];
  return (
    <SourceRow
      detail={articleTimeLabel(article)}
      notes={notes}
      onOpen={
        url === null
          ? undefined
          : () => {
              setOpenError(null);
              void openPublisherUrl(url).then((result) =>
                setOpenError(result === "opened" ? null : OPEN_ERROR[result]),
              );
            }
      }
      openError={openError}
      publisher={article.source.name}
      testID={testID}
      title={article.title}
    />
  );
}

/**
 * The named Article sources of one synthesis element. Citations come from the
 * detail metadata, so they resolve without paging and even when an Article
 * has since left the Story's current membership.
 */
function Citations({
  element,
  citations,
}: {
  element: StoryElement;
  citations: StoryDetail["citations"];
}) {
  const [open, setOpen] = useState(false);
  const cited = element.article_ids.map((articleId) => citations[articleId]);
  const publishers = [...new Set(cited.map((article) => article.source.name))].join(", ");
  return (
    <View style={styles.citations} testID={`citations-${element.id}`}>
      <AppText tone="muted" variant="caption">
        Cited: {publishers}
      </AppText>
      <Button
        label={open ? "Hide cited publications" : `Show cited publications (${cited.length})`}
        onPress={() => setOpen((value) => !value)}
        variant="link"
      />
      {open
        ? cited.map((article) => (
            <PublicationRow
              article={article}
              key={article.id}
              testID={`citation-${element.id}-${article.id}`}
            />
          ))
        : null}
    </View>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <View style={styles.section}>
      <AppText variant="heading">{title}</AppText>
      {children}
    </View>
  );
}

function StoryDetailView({ story }: { story: StoryDetail }) {
  const router = useRouter();
  const { colors } = useTheme();
  const byKind = (kind: StoryElement["kind"]) =>
    story.elements.filter((element) => element.kind === kind);
  const titleElement = byKind("TITLE")[0];
  const summaries = byKind("SUMMARY");
  const contexts = byKind("CONTEXT");
  const publication = storyPublicationLabel(story);
  const preparing = story.content_state === "PREPARING";
  return (
    <View style={styles.detail}>
      {story.content_state === "UPDATING" ? (
        <View style={[styles.notice, { borderColor: colors.border }]} testID="story-updating">
          <StatusLabel label="Updating" />
          <AppText>
            Newer reporting on this Story is being processed. The summary below was generated{" "}
            {formatTimestamp(story.synthesized_at!)} from the publications it cites, and the current
            sources are listed separately.
          </AppText>
        </View>
      ) : null}
      {preparing ? (
        <View style={[styles.notice, { borderColor: colors.border }]} testID="story-preparing">
          <StatusLabel label="Preparing" />
          <AppText>
            A summary of this Story is being prepared. Its current sources are available now.
          </AppText>
        </View>
      ) : null}
      {story.topics.length > 0 ? (
        <AppText
          accessibilityLabel={`Topics: ${story.topics.map((topic) => topic.label).join(", ")}`}
          tone="muted"
          variant="caption"
        >
          {story.topics.map((topic) => topic.label).join(" · ")}
        </AppText>
      ) : null}
      <AppText variant="title">{story.title}</AppText>
      {!preparing ? (
        <AppText tone="muted" variant="caption">
          {synthesisLabel(story.synthesized_at!)}
        </AppText>
      ) : null}
      {titleElement ? <Citations citations={story.citations} element={titleElement} /> : null}
      {publication ? (
        <AppText tone="muted" variant="caption">
          {publication}
        </AppText>
      ) : null}

      {!preparing ? (
        <Section title="Summary">
          {summaries.length === 0 ? (
            <AppText tone="muted">This Story has no summary text.</AppText>
          ) : (
            summaries.map((element) => (
              <View key={element.id} style={styles.element}>
                <AppText>{element.text}</AppText>
                <Citations citations={story.citations} element={element} />
              </View>
            ))
          )}
        </Section>
      ) : null}
      {contexts.length > 0 ? (
        <Section title="Context">
          {contexts.map((element) => (
            <View key={element.id} style={styles.element}>
              <AppText>{element.text}</AppText>
              <Citations citations={story.citations} element={element} />
            </View>
          ))}
        </Section>
      ) : null}
      {story.entities.length > 0 ? (
        <Section title="Mentioned">
          {story.entities.map((entity) => (
            <AppText key={`${entity.kind}:${entity.display_name}`}>
              {ENTITY_KIND[entity.kind]}: {entity.display_name}
            </AppText>
          ))}
        </Section>
      ) : null}

      <Section title="Sources">
        {!preparing ? (
          <AppText tone="muted">
            This summary cites {articlesFromSources(story.article_count, story.source_count)}.
          </AppText>
        ) : null}
        <AppText>
          Current sources:{" "}
          {articlesFromSources(story.current_article_count, story.current_source_count)}.
        </AppText>
        <AppText tone="muted" variant="caption">
          {LINK_NOTE}
        </AppText>
        <Button
          label="View current sources"
          onPress={() => router.push(storySourcesHref(story.id))}
          variant="secondary"
        />
      </Section>
    </View>
  );
}

/** Story details: the shared factual Story, its synthesis provenance and source access. */
export function StoryScreen({ storyIdParam }: { storyIdParam: unknown }) {
  const storyId = parseStoryId(storyIdParam);
  const accountId = useAccountId();
  if (storyId === null) return <InvalidStoryLink />;
  if (accountId === null) return null;
  return <StoryDetailContent accountId={accountId} storyId={storyId} />;
}

function StoryDetailContent({ accountId, storyId }: { accountId: string; storyId: string }) {
  const detail = useStoryDetail(accountId, storyId);
  return (
    <Screen edges={[...EDGES]}>
      {detail.data !== undefined ? (
        <StoryDetailView story={detail.data} />
      ) : detail.isError ? (
        <StoryLoadError error={detail.error} onRetry={() => void detail.refetch()} />
      ) : (
        <LoadingState label="Loading the Story" />
      )}
    </Screen>
  );
}

/** A Story's current source publications, page by page, plus citations outside them. */
export function StorySourcesScreen({ storyIdParam }: { storyIdParam: unknown }) {
  const storyId = parseStoryId(storyIdParam);
  const accountId = useAccountId();
  if (storyId === null) return <InvalidStoryLink />;
  if (accountId === null) return null;
  return <StorySourcesContent accountId={accountId} storyId={storyId} />;
}

function StorySourcesContent({ accountId, storyId }: { accountId: string; storyId: string }) {
  const detail = useStoryDetail(accountId, storyId);
  return (
    <Screen edges={[...EDGES]} scroll={detail.data === undefined}>
      {detail.data !== undefined ? (
        <SourceList accountId={accountId} story={detail.data} />
      ) : detail.isError ? (
        <StoryLoadError error={detail.error} onRetry={() => void detail.refetch()} />
      ) : (
        <LoadingState label="Loading sources" />
      )}
    </Screen>
  );
}

function SourceList({ accountId, story }: { accountId: string; story: StoryDetail }) {
  const { colors } = useTheme();
  const sources = useStorySources(accountId, story.id, story.synthesis_id);
  const earlierCitations = Object.values(story.citations).filter(
    (article) => !article.is_current_member,
  );

  const header = (
    <View style={styles.detail}>
      <AppText variant="heading">{story.title}</AppText>
      <AppText>
        Current sources:{" "}
        {articlesFromSources(story.current_article_count, story.current_source_count)}. Each
        publication is listed separately, including several from the same publisher.
      </AppText>
      <AppText tone="muted" variant="caption">
        {LINK_NOTE}
      </AppText>
      {sources.contextStatus === "restarted" ? (
        <AppText accessibilityLiveRegion="polite" testID="sources-restarted">
          The sources changed while you were browsing, so the list was reloaded.
        </AppText>
      ) : null}
      {earlierCitations.length > 0 ? (
        <Section title="Cited earlier, no longer current">
          <AppText tone="muted" variant="caption">
            The summary cites these publications, which are no longer among this Story&apos;s
            current sources.
          </AppText>
          {earlierCitations.map((article) => (
            <PublicationRow article={article} key={article.id} testID={`earlier-${article.id}`} />
          ))}
        </Section>
      ) : null}
      <AppText variant="heading">Current sources</AppText>
    </View>
  );

  if (sources.status !== "ready") {
    return (
      <FlatList
        ListFooterComponent={
          sources.status === "error" ? (
            <ErrorState
              message="The sources could not be loaded. Check your connection and try again."
              primary={{ label: "Try again", onPress: sources.retryFirstPage }}
            />
          ) : (
            <LoadingState
              label={
                sources.contextStatus === "restarted" ? "Reloading sources" : "Loading sources"
              }
            />
          )
        }
        ListHeaderComponent={header}
        contentContainerStyle={styles.list}
        data={[]}
        renderItem={null}
      />
    );
  }

  return (
    <FlatList
      ListFooterComponent={
        sources.contextStatus === "changed_again" ? (
          <View style={styles.footer} testID="sources-changed-again">
            <AppText accessibilityRole="alert">
              The sources changed again while loading. Reload to see the current list.
            </AppText>
            <Button
              label="Reload sources"
              onPress={sources.reloadAfterChange}
              variant="secondary"
            />
          </View>
        ) : sources.pageStatus === "loading" ? (
          <View style={styles.footer} testID="sources-footer-loading">
            <ActivityIndicator accessibilityLabel="Loading more sources" color={colors.accent} />
          </View>
        ) : sources.pageStatus === "error" ? (
          <View style={styles.footer} testID="sources-footer-error">
            <AppText accessibilityRole="alert">More sources could not be loaded.</AppText>
            <Button label="Try again" onPress={sources.retryNextPage} variant="secondary" />
          </View>
        ) : sources.pageStatus === "end" ? (
          <View style={styles.footer} testID="sources-footer-end">
            <AppText tone="muted">
              {sources.articles.length === 0
                ? "No current sources are listed."
                : "All current sources are listed."}
            </AppText>
          </View>
        ) : null
      }
      ListHeaderComponent={header}
      contentContainerStyle={styles.list}
      data={sources.articles}
      keyExtractor={(article) => article.id}
      onEndReached={sources.loadMore}
      onEndReachedThreshold={0.5}
      renderItem={({ item }) => <PublicationRow article={item} testID={`source-${item.id}`} />}
      testID="sources-list"
    />
  );
}

const styles = StyleSheet.create({
  detail: { gap: spacing.md },
  notice: { gap: spacing.sm, borderWidth: 1, borderRadius: 8, padding: spacing.sm },
  section: { gap: spacing.sm },
  element: { gap: spacing.xs },
  citations: { gap: spacing.xs },
  list: { gap: spacing.sm, padding: spacing.md },
  footer: { alignItems: "center", gap: spacing.sm, paddingVertical: spacing.lg },
});
