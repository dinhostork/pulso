import { decodeFeedPage } from "@/api/decoders";
import { MAX_FEED_PAGES } from "@/server-state/query";
import { feedPage, storyCard } from "@/test-utils/stories";

import { atRetainedPageBound, feedItems, nextFeedCursor, type FeedData } from "../feedPages";

function chain(...pages: ReturnType<typeof feedPage>[]): FeedData {
  return {
    pages: pages.map((page) => decodeFeedPage(page)),
    pageParams: pages.map((_, index) => (index === 0 ? null : `c${index}`)),
  };
}

describe("feed pages", () => {
  it("keeps server order and gives each Story one absolute position", () => {
    const data = chain(
      feedPage([storyCard("30"), storyCard("20")], "c1"),
      feedPage([storyCard("10")], null),
    );
    expect(feedItems(data).map((item) => [item.story.id, item.position])).toEqual([
      ["30", 0],
      ["20", 1],
      ["10", 2],
    ]);
  });

  it("renders a repeated Story ID from an overlapping or retried page only once", () => {
    const data = chain(
      feedPage([storyCard("30"), storyCard("20")], "c1"),
      feedPage([storyCard("20"), storyCard("10"), storyCard("30")], null),
    );
    expect(feedItems(data).map((item) => item.story.id)).toEqual(["30", "20", "10"]);
    expect(feedItems(data).map((item) => item.position)).toEqual([0, 1, 2]);
  });

  it("returns no items before the first page", () => {
    expect(feedItems(undefined)).toEqual([]);
  });

  it("stops the cursor chain at the retained-page bound and reports the bound", () => {
    const pages = Array.from({ length: MAX_FEED_PAGES }, (_, index) =>
      feedPage([storyCard(String(index + 1))], `c${index + 1}`),
    );
    const decoded = chain(...pages);
    const last = decoded.pages[decoded.pages.length - 1];
    expect(nextFeedCursor(last, decoded.pages)).toBeUndefined();
    expect(nextFeedCursor(last, decoded.pages.slice(0, 3))).toBe(`c${MAX_FEED_PAGES}`);
    expect(atRetainedPageBound(decoded)).toBe(true);
  });

  it("does not report the bound when the chain simply ended", () => {
    const pages = Array.from({ length: MAX_FEED_PAGES }, (_, index) =>
      feedPage([storyCard(String(index + 1))], index === MAX_FEED_PAGES - 1 ? null : `c${index}`),
    );
    expect(atRetainedPageBound(chain(...pages))).toBe(false);
    expect(atRetainedPageBound(chain(feedPage([storyCard("1")], "c1")))).toBe(false);
  });
});
