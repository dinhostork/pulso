import { useLocalSearchParams } from "expo-router";

import { StorySourcesScreen } from "@/features/stories/StoryScreens";

export default function StorySourcesRoute() {
  const { storyId } = useLocalSearchParams();
  return <StorySourcesScreen storyIdParam={storyId} />;
}
