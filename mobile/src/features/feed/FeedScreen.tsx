import { useRouter } from "expo-router";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef } from "react";
import {
  ActivityIndicator,
  FlatList,
  RefreshControl,
  StyleSheet,
  View,
  type CellRendererProps,
  type LayoutChangeEvent,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";

import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { Screen } from "@/components/Screen";
import { EmptyState, ErrorState, LoadingState } from "@/components/StatusState";
import { storyHref, storySourcesHref } from "@/navigation/routes";
import { AccountActions } from "@/session/components/AccountActions";
import { useAccountId } from "@/session/SessionProvider";
import { spacing, useTheme } from "@/theme";

import type { FeedItem } from "./feedPages";
import { StoryCard } from "./StoryCard";
import { useFeed, type FeedController } from "./useFeed";
import { FeedVisibilityTracker, type CardVisibility } from "./visibility";

const VisibilityContext = createContext<FeedVisibilityTracker | null>(null);

/** Reports each laid-out card's geometry to the visibility seam; nothing else. */
function FeedCell({ item, onLayout, children, ...props }: CellRendererProps<FeedItem>) {
  const tracker = useContext(VisibilityContext);
  const { story, position } = item;
  useEffect(() => {
    if (tracker === null) return;
    return () => tracker.removeCard(story.id);
  }, [tracker, story.id]);
  useEffect(() => tracker?.setPosition(story.id, position), [tracker, story.id, position]);
  return (
    <View
      {...props}
      onLayout={(event: LayoutChangeEvent) => {
        onLayout?.(event);
        const { y, height } = event.nativeEvent.layout;
        tracker?.setCard(story.id, position, { top: y, height });
      }}
      testID={`feed-cell-${story.id}`}
    >
      {children}
    </View>
  );
}

function RefreshNotice({ feed }: { feed: FeedController }) {
  if (feed.refreshStatus !== "failed") return null;
  return (
    <View style={styles.notice} testID="feed-refresh-error">
      <AppText accessibilityLiveRegion="polite" accessibilityRole="alert">
        The Feed could not be refreshed. The Stories below were loaded earlier.
      </AppText>
      <Button
        label="Try refreshing again"
        onPress={() => void feed.refresh()}
        variant="secondary"
      />
    </View>
  );
}

function Footer({ feed, onRestart }: { feed: FeedController; onRestart: () => void }) {
  const { colors } = useTheme();
  switch (feed.nextPageStatus) {
    case "loading":
      return (
        <View accessibilityLiveRegion="polite" style={styles.footer} testID="feed-footer-loading">
          <ActivityIndicator accessibilityLabel="Loading more Stories" color={colors.accent} />
        </View>
      );
    case "error":
      return (
        <View style={styles.footer} testID="feed-footer-error">
          <AppText accessibilityRole="alert">More Stories could not be loaded.</AppText>
          <Button label="Try again" onPress={feed.retryNextPage} variant="secondary" />
        </View>
      );
    case "expired":
      return (
        <View style={styles.footer} testID="feed-footer-expired">
          <AppText accessibilityRole="alert">
            This Feed has expired. Restart to continue with the newest Stories.
          </AppText>
          <Button
            busy={feed.refreshStatus === "refreshing"}
            label="Restart from the newest Stories"
            onPress={onRestart}
            variant="secondary"
          />
        </View>
      );
    case "bound":
      return (
        <View style={styles.footer} testID="feed-footer-bound">
          <AppText tone="muted">
            You have reached the most Stories the Feed keeps loaded at once.
          </AppText>
          <Button
            busy={feed.refreshStatus === "refreshing"}
            label="Restart from the newest Stories"
            onPress={onRestart}
            variant="secondary"
          />
        </View>
      );
    case "end":
      return (
        <View style={styles.footer} testID="feed-footer-end">
          <AppText tone="muted">You have reached the end of the Feed.</AppText>
        </View>
      );
    default:
      return null;
  }
}

/**
 * The paginated Story Feed. It renders the server's immutable chronological
 * order for the signed-in account and keeps its scroll position while Story
 * and source screens are pushed above it.
 */
export function FeedScreen({
  onVisibilityChange,
}: {
  /** The #51 exposure seam (a stable callback); absent means geometry is not tracked at all. */
  onVisibilityChange?: (cards: CardVisibility[]) => void;
}) {
  const accountId = useAccountId();
  return (
    <Screen headerAction={<AccountActions />} scroll={false} title="Feed">
      {accountId === null ? null : (
        <FeedContent
          key={accountId}
          accountId={accountId}
          onVisibilityChange={onVisibilityChange}
        />
      )}
    </Screen>
  );
}

function FeedContent({
  accountId,
  onVisibilityChange,
}: {
  accountId: string;
  onVisibilityChange?: (cards: CardVisibility[]) => void;
}) {
  const router = useRouter();
  const feed = useFeed(accountId);
  const { colors } = useTheme();
  const list = useRef<FlatList<FeedItem>>(null);

  // Consumers pass a stable callback; a new one starts a new tracker.
  const tracker = useMemo(
    () => (onVisibilityChange ? new FeedVisibilityTracker(onVisibilityChange) : null),
    [onVisibilityChange],
  );
  const viewportHeight = useRef(0);
  const viewportOffset = useRef(0);

  const openStory = useCallback((storyId: string) => router.push(storyHref(storyId)), [router]);
  const openSources = useCallback(
    (storyId: string) => router.push(storySourcesHref(storyId)),
    [router],
  );
  const restart = useCallback(() => {
    void feed.refresh().then((replaced) => {
      if (replaced) list.current?.scrollToOffset({ offset: 0, animated: false });
    });
  }, [feed]);

  if (feed.status === "loading") return <LoadingState label="Loading Stories" />;
  if (feed.status === "error") {
    return (
      <ErrorState
        message="The Feed could not be loaded. Check your connection and try again."
        primary={{ label: "Try again", onPress: feed.retryFirstLoad }}
        title="Feed unavailable"
      />
    );
  }
  if (feed.items.length === 0 && feed.nextPageStatus === "end") {
    return (
      <View style={styles.fill}>
        <RefreshNotice feed={feed} />
        <EmptyState
          action={{
            label: feed.refreshStatus === "refreshing" ? "Refreshing…" : "Refresh",
            onPress: () => void feed.refresh(),
          }}
          message="Stories appear here once they are ready. There are none right now."
          title="No Stories yet"
        />
      </View>
    );
  }

  return (
    <VisibilityContext.Provider value={tracker}>
      <FlatList
        CellRendererComponent={FeedCell}
        ItemSeparatorComponent={Separator}
        ListFooterComponent={<Footer feed={feed} onRestart={restart} />}
        ListHeaderComponent={<RefreshNotice feed={feed} />}
        data={feed.items}
        keyExtractor={(item) => item.story.id}
        onEndReached={feed.loadMore}
        onEndReachedThreshold={0.5}
        onLayout={(event) => {
          viewportHeight.current = event.nativeEvent.layout.height;
          tracker?.setViewport({ offset: viewportOffset.current, height: viewportHeight.current });
        }}
        onScroll={(event: NativeSyntheticEvent<NativeScrollEvent>) => {
          viewportOffset.current = event.nativeEvent.contentOffset.y;
          tracker?.setViewport({
            offset: viewportOffset.current,
            height: event.nativeEvent.layoutMeasurement.height || viewportHeight.current,
          });
        }}
        ref={list}
        refreshControl={
          <RefreshControl
            accessibilityLabel="Refresh the Feed"
            colors={[colors.accent]}
            onRefresh={() => void feed.refresh()}
            refreshing={feed.refreshStatus === "refreshing"}
            tintColor={colors.accent}
          />
        }
        renderItem={({ item }) => (
          <StoryCard onOpen={openStory} onOpenSources={openSources} story={item.story} />
        )}
        scrollEventThrottle={100}
        style={styles.fill}
        testID="feed-list"
      />
    </VisibilityContext.Provider>
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
});
