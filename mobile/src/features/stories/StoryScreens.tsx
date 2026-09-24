import { useRouter } from "expo-router";

import { Screen } from "@/components/Screen";
import { EmptyState, ErrorState } from "@/components/StatusState";
import { FEED_HREF, parseStoryId, storySourcesHref } from "@/navigation/routes";

const EDGES = ["bottom", "left", "right"] as const;

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

/** Route target for Story details; #49 replaces the body with the Story. */
export function StoryScreen({ storyIdParam }: { storyIdParam: unknown }) {
  const router = useRouter();
  const storyId = parseStoryId(storyIdParam);
  if (storyId === null) return <InvalidStoryLink />;
  return (
    <Screen edges={[...EDGES]}>
      <EmptyState
        action={{ label: "View sources", onPress: () => router.push(storySourcesHref(storyId)) }}
        message="Story details are not available in this build yet."
        title="Story details"
      />
    </Screen>
  );
}

/** Route target for a Story's source list; #49 replaces the body with source rows. */
export function StorySourcesScreen({ storyIdParam }: { storyIdParam: unknown }) {
  const storyId = parseStoryId(storyIdParam);
  if (storyId === null) return <InvalidStoryLink />;
  return (
    <Screen edges={[...EDGES]}>
      <EmptyState message="The source list is not available in this build yet." title="Sources" />
    </Screen>
  );
}
