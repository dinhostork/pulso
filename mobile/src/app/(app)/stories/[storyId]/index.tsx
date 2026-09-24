import { useLocalSearchParams } from "expo-router";

import { StoryScreen } from "@/features/stories/StoryScreens";

export default function StoryRoute() {
  const { storyId } = useLocalSearchParams();
  return <StoryScreen storyIdParam={storyId} />;
}
