import {
  decodeBookmarkResult,
  decodeFeedImpressionResponse,
  decodeFeedPage,
  decodeSavedPage,
  decodeSourcePage,
  decodeStoryDetail,
} from "../decoders";
import { DecodeError } from "../errors";

// Repository contracts are test inputs only; runtime never falls back to them.
const feed = require("../../../../docs/contracts/mobile-feed/feed-current.json") as unknown;
const single =
  require("../../../../docs/contracts/mobile-feed/story-current-single-source.json") as unknown;
const updating = require("../../../../docs/contracts/mobile-feed/story-updating.json") as unknown;
const preparing = require("../../../../docs/contracts/mobile-feed/story-preparing.json") as unknown;
const sources = require("../../../../docs/contracts/mobile-feed/sources-page.json") as unknown;
const impressions = require("../../../../docs/contracts/mobile-feed/feed-impressions.json") as {
  response: unknown;
};
const viewers = require("../../../../docs/contracts/mobile-feed/story-current-viewers.json") as {
  user_a: unknown;
  user_b: unknown;
};
const bookmark = require("../../../../docs/contracts/mobile-feed/bookmark.json") as {
  saved: unknown;
  unavailable_entry: unknown;
};

describe("Mobile Feed contract decoders", () => {
  it("decodes current, updating, preparing and source fixtures without coercing IDs", () => {
    const decodedFeed = decodeFeedPage(feed);
    expect(decodedFeed.results[0].id).toBe("9007199254740993");
    expect(typeof decodedFeed.results[0].id).toBe("string");

    const decodedSingle = decodeStoryDetail(single);
    expect(decodedSingle.first_published_at).toBeNull();
    expect(decodedSingle.citations["101"].byline).toBeNull();
    expect(decodeStoryDetail(updating).content_state).toBe("UPDATING");
    expect(decodeStoryDetail(preparing)).toMatchObject({
      content_state: "PREPARING",
      synthesis_id: null,
      synthesized_at: null,
      elements: [],
    });
    expect(decodeSourcePage(sources).results).toHaveLength(2);
    expect(decodeFeedImpressionResponse(impressions.response).results[0].outcome).toBe("accepted");
    expect(decodeBookmarkResult(bookmark.saved).story_id).toBe("9007199254740993");
    expect(
      decodeSavedPage({ results: [bookmark.unavailable_entry], next_cursor: null }).results[0],
    ).toMatchObject({ availability: "UNAVAILABLE", story_id: "45" });
  });

  it("preserves equal shared facts while viewer bookmark metadata differs", () => {
    const userA = decodeStoryDetail(viewers.user_a);
    const userB = decodeStoryDetail(viewers.user_b);
    const { viewer: viewerA, ...factsA } = userA;
    const { viewer: viewerB, ...factsB } = userB;
    expect(factsA).toEqual(factsB);
    expect(viewerA.bookmarked).toBe(false);
    expect(viewerB.bookmarked).toBe(true);
  });

  it("fails visibly for incompatible fixture mutations", () => {
    const mutated = JSON.parse(JSON.stringify(feed)) as { results: Record<string, unknown>[] };
    mutated.results[0].id = 9007199254740992;
    expect(() => decodeFeedPage(mutated)).toThrow(DecodeError);

    const missingViewer = JSON.parse(JSON.stringify(feed)) as {
      results: Record<string, unknown>[];
    };
    delete missingViewer.results[0].viewer;
    expect(() => decodeFeedPage(missingViewer)).toThrow(DecodeError);

    const privateSource = JSON.parse(JSON.stringify(sources)) as {
      results: { canonical_url: string }[];
    };
    privateSource.results[0].canonical_url = "http://127.0.0.1/private";
    expect(() => decodeSourcePage(privateSource)).toThrow(DecodeError);
  });
});
