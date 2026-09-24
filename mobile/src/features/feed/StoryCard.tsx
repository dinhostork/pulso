import { memo, useState } from "react";
import { Pressable, StyleSheet, View } from "react-native";

import type { StoryCard as StoryCardData } from "@/api/types";
import { AppText } from "@/components/AppText";
import { Button } from "@/components/Button";
import { StatusLabel } from "@/components/StatusState";
import { countLabel, storyPublicationLabel } from "@/features/stories/format";
import { radius, spacing, useTheme } from "@/theme";

/**
 * One Story, however many Articles support it. Only persisted factual fields
 * are shown, as plain text: Topics, title, the first Summary and Context
 * elements, the publication window and the generation's source/article
 * counts. A missing field is left out or stated as missing, never filled in.
 */
export const StoryCard = memo(function StoryCard({
  story,
  onOpen,
  onOpenSources,
}: {
  story: StoryCardData;
  onOpen: (storyId: string) => void;
  onOpenSources: (storyId: string) => void;
}) {
  const { colors } = useTheme();
  const [focused, setFocused] = useState(false);
  const summary = story.elements.find((element) => element.kind === "SUMMARY");
  const context = story.elements.find((element) => element.kind === "CONTEXT");
  const publication = storyPublicationLabel(story);
  const topics = story.topics.map((topic) => topic.label).join(" · ");
  return (
    <View
      style={[styles.card, { backgroundColor: colors.surface, borderColor: colors.border }]}
      testID={`story-card-${story.id}`}
    >
      <Pressable
        accessibilityHint="Opens the Story details"
        accessibilityRole="button"
        onBlur={() => setFocused(false)}
        onFocus={() => setFocused(true)}
        onPress={() => onOpen(story.id)}
        style={({ pressed }) => [
          styles.body,
          pressed && styles.pressed,
          focused && { borderColor: colors.focus, borderWidth: 2 },
        ]}
        testID={`story-open-${story.id}`}
      >
        {topics ? (
          <AppText accessibilityLabel={`Topics: ${topics}`} tone="muted" variant="caption">
            {topics}
          </AppText>
        ) : null}
        <AppText variant="heading">{story.title}</AppText>
        {summary ? <AppText>{summary.text}</AppText> : null}
        {context ? (
          <View style={styles.context}>
            <AppText tone="muted" variant="caption">
              Context
            </AppText>
            <AppText>{context.text}</AppText>
          </View>
        ) : null}
        <AppText tone="muted" variant="caption">
          {publication ?? "Publication time not available"}
        </AppText>
        {story.content_state !== "PREPARING" ? (
          <AppText tone="muted" variant="caption">
            {countLabel(story.source_count, "source", "sources")} ·{" "}
            {countLabel(story.article_count, "article", "articles")}
          </AppText>
        ) : null}
        {story.content_state !== "CURRENT" || story.viewer.bookmarked ? (
          <View style={styles.labels}>
            {story.content_state === "UPDATING" ? <StatusLabel label="Updating" /> : null}
            {story.content_state === "PREPARING" ? <StatusLabel label="Preparing" /> : null}
            {story.viewer.bookmarked ? <StatusLabel label="Saved" /> : null}
          </View>
        ) : null}
      </Pressable>
      <Button
        hint="Lists the publications behind this Story"
        label="View sources"
        onPress={() => onOpenSources(story.id)}
        testID={`story-sources-${story.id}`}
        variant="link"
      />
    </View>
  );
});

const styles = StyleSheet.create({
  card: {
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    padding: spacing.sm,
    gap: spacing.xs,
  },
  body: {
    gap: spacing.xs,
    padding: spacing.sm,
    borderRadius: radius.sm,
    borderWidth: 2,
    borderColor: "transparent",
  },
  pressed: { opacity: 0.7 },
  context: { gap: 2 },
  labels: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
});
