import { useRouter } from "expo-router";
import { useCallback } from "react";
import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, View } from "react-native";

import type { SavedEntry } from "@/api/types";
import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { Screen } from "@/components/Screen";
import { EmptyState, ErrorState, LoadingState, StatusLabel } from "@/components/StatusState";
import { StoryCard } from "@/features/feed/StoryCard";
import { formatTimestamp } from "@/features/stories/format";
import { storyHref, storySourcesHref } from "@/navigation/routes";
import { AccountActions } from "@/session/components/AccountActions";
import { useAccountId } from "@/session/SessionProvider";
import { radius, spacing, useTheme } from "@/theme";

import { BookmarkButton } from "./BookmarkButton";
import { useSaved, type SavedController } from "./useSaved";

/**
 * The account's saved Stories, newest save first, read page by page from the
 * server. Saved is not a Feed surface: it reports no FeedImpressions and its
 * rows carry no visibility tracking.
 */
export function SavedScreen() {
  const accountId = useAccountId();
  return (
    <Screen headerAction={<AccountActions />} scroll={false} title="Saved">
      {accountId === null ? null : <SavedContent key={accountId} accountId={accountId} />}
    </Screen>
  );
}

/**
 * A saved Story that can no longer be read (archived or emptied). It shows
 * only the Bookmark's own facts, never an obsolete synthesis, and stays until
 * the reader removes it; it is never moved to another Story.
 */
function UnavailableRow({ accountId, entry }: { accountId: string; entry: SavedEntry }) {
  const { colors } = useTheme();
  return (
    <View
      style={[styles.tombstone, { backgroundColor: colors.surface, borderColor: colors.border }]}
      testID={`saved-unavailable-${entry.story_id}`}
    >
      <StatusLabel label="Unavailable" tone="attention" />
      <AppText variant="heading">This Story is no longer available</AppText>
      <AppText tone="muted">
        It was saved {formatTimestamp(entry.saved_at)}. Pulso no longer shows its summary or
        sources. You can remove it from Saved.
      </AppText>
      <BookmarkButton
        accountId={accountId}
        bookmarked
        storyId={entry.story_id}
        testID={`saved-bookmark-${entry.story_id}`}
      />
    </View>
  );
}

function RefreshNotice({ saved }: { saved: SavedController }) {
  if (saved.refreshStatus !== "failed") return null;
  return (
    <View style={styles.notice} testID="saved-refresh-error">
      <AppText accessibilityLiveRegion="polite" accessibilityRole="alert">
        Saved could not be refreshed. The Stories below were loaded earlier.
      </AppText>
      <Button
        label="Try refreshing again"
        onPress={() => void saved.refresh()}
        variant="secondary"
      />
    </View>
  );
}

function Footer({ saved }: { saved: SavedController }) {
  const { colors } = useTheme();
  switch (saved.pageStatus) {
    case "loading":
      return (
        <View accessibilityLiveRegion="polite" style={styles.footer} testID="saved-footer-loading">
          <ActivityIndicator
            accessibilityLabel="Loading more saved Stories"
            color={colors.accent}
          />
        </View>
      );
    case "error":
      return (
        <View style={styles.footer} testID="saved-footer-error">
          <AppText accessibilityRole="alert">More saved Stories could not be loaded.</AppText>
          <Button label="Try again" onPress={saved.retryNextPage} variant="secondary" />
        </View>
      );
    case "expired":
      return (
        <View style={styles.footer} testID="saved-footer-expired">
          <AppText accessibilityRole="alert">This list has expired. Reload it to continue.</AppText>
          <Button
            busy={saved.refreshStatus === "refreshing"}
            label="Reload Saved"
            onPress={() => void saved.refresh()}
            variant="secondary"
          />
        </View>
      );
    case "end":
      return (
        <View style={styles.footer} testID="saved-footer-end">
          <AppText tone="muted">You have reached the end of Saved.</AppText>
        </View>
      );
    default:
      return null;
  }
}

function SavedContent({ accountId }: { accountId: string }) {
  const router = useRouter();
  const saved = useSaved(accountId);
  const { colors } = useTheme();
  const openStory = useCallback((storyId: string) => router.push(storyHref(storyId)), [router]);
  const openSources = useCallback(
    (storyId: string) => router.push(storySourcesHref(storyId)),
    [router],
  );

  if (saved.status === "loading") return <LoadingState label="Loading saved Stories" />;
  if (saved.status === "error") {
    return (
      <ErrorState
        message="Saved Stories could not be loaded. Check your connection and try again."
        primary={{ label: "Try again", onPress: saved.retryFirstLoad }}
        title="Saved unavailable"
      />
    );
  }
  if (saved.entries.length === 0 && saved.pageStatus === "end") {
    return (
      <View style={styles.fill} testID="saved-empty">
        <RefreshNotice saved={saved} />
        <EmptyState
          action={{
            label: saved.refreshStatus === "refreshing" ? "Refreshing…" : "Refresh",
            onPress: () => void saved.refresh(),
          }}
          message="Save a Story from the Feed or its details to find it here later."
          title="No saved Stories"
        />
      </View>
    );
  }

  return (
    <FlatList
      ItemSeparatorComponent={Separator}
      ListFooterComponent={<Footer saved={saved} />}
      ListHeaderComponent={<RefreshNotice saved={saved} />}
      data={saved.entries}
      keyExtractor={(entry) => entry.story_id}
      onEndReached={saved.loadMore}
      onEndReachedThreshold={0.5}
      refreshControl={
        <RefreshControl
          accessibilityLabel="Refresh Saved"
          colors={[colors.accent]}
          onRefresh={() => void saved.refresh()}
          refreshing={saved.refreshStatus === "refreshing"}
          tintColor={colors.accent}
        />
      }
      renderItem={({ item }) =>
        item.availability === "AVAILABLE" ? (
          <StoryCard
            accountId={accountId}
            onOpen={openStory}
            onOpenSources={openSources}
            story={item.story}
            testIDPrefix="saved"
          />
        ) : (
          <UnavailableRow accountId={accountId} entry={item} />
        )
      }
      style={styles.fill}
      testID="saved-list"
    />
  );
}

function Separator() {
  return <View style={styles.separator} />;
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  separator: { height: spacing.md },
  notice: { gap: spacing.sm, paddingBottom: spacing.md },
  footer: { alignItems: "center", gap: spacing.sm, paddingVertical: spacing.lg },
  tombstone: {
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    gap: spacing.xs,
    padding: spacing.md,
  },
});
