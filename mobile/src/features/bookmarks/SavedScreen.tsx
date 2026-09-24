import { Screen } from "@/components/Screen";
import { EmptyState } from "@/components/StatusState";
import { AccountActions } from "@/session/components/AccountActions";

/** Route target for the Saved tab; #50 replaces the body with saved Stories. */
export function SavedScreen() {
  return (
    <Screen headerAction={<AccountActions />} title="Saved">
      <EmptyState
        message="Saved Stories are not available in this build yet."
        title="No saved Stories to show"
      />
    </Screen>
  );
}
