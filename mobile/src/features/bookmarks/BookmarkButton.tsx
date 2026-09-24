import { StyleSheet, View } from "react-native";

import { AppText } from "@/components/AppText";
import { Button, type ButtonVariant } from "@/components/Button";
import { spacing } from "@/theme";

import { useBookmark, type BookmarkIntent, type BookmarkWriteError } from "./useBookmark";

function failureMessage(error: BookmarkWriteError): string {
  const saving = error.intent === "save";
  switch (error.failure) {
    case "unconfirmed":
      return saving
        ? "Pulso could not confirm whether this Story was saved. Try again."
        : "Pulso could not confirm whether this Story was removed from Saved. Try again.";
    case "rate_limited":
      return "Too many changes right now. Wait a moment, then try again.";
    case "unavailable":
      return "This Story is no longer available, so it cannot be saved.";
    case "not_found":
      return "This Story no longer exists, so it cannot be saved.";
    default:
      return saving
        ? "This Story was not saved. Try again."
        : "This Story was not removed from Saved. Try again.";
  }
}

const PENDING_LABEL: Record<BookmarkIntent, string> = {
  save: "Saving…",
  remove: "Removing…",
};

const RETRY_LABEL: Record<BookmarkIntent, string> = {
  save: "Try saving again",
  remove: "Try removing again",
};

/**
 * Save/remove for one Story, identical on Feed, detail and Saved. The shown
 * state is the server's (`bookmarked` comes from the account's cached server
 * payload); while a write is pending the control is busy and disabled; after a
 * failure it explains the outcome and repeats the failed intent on request.
 */
export function BookmarkButton({
  accountId,
  storyId,
  bookmarked,
  variant = "secondary",
  testID,
}: {
  accountId: string;
  storyId: string;
  bookmarked: boolean;
  variant?: ButtonVariant;
  testID?: string;
}) {
  const bookmark = useBookmark(accountId, storyId);
  const { failure } = bookmark;
  const retrying = failure !== null && failure.retryable;
  const label = bookmark.pendingIntent
    ? PENDING_LABEL[bookmark.pendingIntent]
    : retrying
      ? RETRY_LABEL[failure.intent]
      : bookmarked
        ? "Remove from Saved"
        : "Save";
  return (
    <View style={styles.container}>
      <Button
        busy={bookmark.pending}
        hint={bookmarked ? "Removes this Story from Saved" : "Adds this Story to Saved"}
        label={label}
        onPress={retrying ? bookmark.retry : bookmarked ? bookmark.remove : bookmark.save}
        testID={testID}
        variant={variant}
      />
      {failure !== null && !bookmark.pending ? (
        <AppText
          accessibilityLiveRegion="polite"
          accessibilityRole="alert"
          testID={testID ? `${testID}-error` : undefined}
          tone="danger"
        >
          {failureMessage(failure)}
        </AppText>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: spacing.xs },
});
