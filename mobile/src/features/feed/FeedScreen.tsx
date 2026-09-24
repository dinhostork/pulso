import { Screen } from "@/components/Screen";
import { EmptyState } from "@/components/StatusState";
import { AccountActions } from "@/session/components/AccountActions";

/**
 * Route target for the Feed tab. #48 replaces the body with the paginated
 * Story list; until then it states plainly that no Story content is shown.
 */
export function FeedScreen() {
  return (
    <Screen headerAction={<AccountActions />} title="Feed">
      <EmptyState
        message="The Story feed is not available in this build yet."
        title="No Stories to show"
      />
    </Screen>
  );
}
