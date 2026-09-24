import { openPublisherUrl } from "@/navigation/external";
import { tabBarHeight } from "@/navigation/ReadingTabs";
import { parseStoryId, storyHref, storySourcesHref } from "@/navigation/routes";

jest.mock("expo-linking", () => ({ openURL: jest.fn(async () => true) }));

describe("parseStoryId", () => {
  it.each(["1", "7", "9007199254740993", "9223372036854775807"])("accepts %s", (value) => {
    expect(parseStoryId(value)).toBe(value);
  });

  it.each([
    undefined,
    7,
    ["7"],
    "",
    "0",
    "07",
    "-7",
    "7.0",
    " 7",
    "7/sources",
    "9223372036854775808",
    "12345678901234567890",
  ])("rejects %p", (value) => {
    expect(parseStoryId(value)).toBeNull();
  });

  it("builds relative in-app hrefs that carry only the Story ID", () => {
    expect(storyHref("7")).toBe("/stories/7");
    expect(storySourcesHref("7")).toBe("/stories/7/sources");
  });
});

describe("openPublisherUrl", () => {
  const { openURL } = jest.requireMock("expo-linking") as { openURL: jest.Mock };

  beforeEach(() => openURL.mockClear());

  it("hands HTTP(S) publisher pages to the operating system", async () => {
    await expect(openPublisherUrl("https://publisher.example/a")).resolves.toBe(true);
    expect(openURL).toHaveBeenCalledWith("https://publisher.example/a");
  });

  it.each(["javascript:alert(1)", "pulso://stories/7", "file:///etc/hosts", "/stories/7"])(
    "never opens %s",
    async (url) => {
      await expect(openPublisherUrl(url)).resolves.toBe(false);
      expect(openURL).not.toHaveBeenCalled();
    },
  );

  it("reports a failed hand-off instead of throwing", async () => {
    openURL.mockRejectedValueOnce(new Error("no handler"));
    await expect(openPublisherUrl("https://publisher.example/a")).resolves.toBe(false);
  });
});

describe("tabBarHeight", () => {
  it("keeps the platform height at default text and adds the safe-area inset", () => {
    expect(tabBarHeight(1, 0)).toBe(49);
    expect(tabBarHeight(1, 34)).toBe(83);
  });

  it("grows with large dynamic text so labels are not clipped", () => {
    expect(tabBarHeight(2, 0)).toBeGreaterThan(tabBarHeight(1, 0));
    expect(tabBarHeight(3.1, 0) - tabBarHeight(2, 0)).toBeGreaterThanOrEqual(20);
  });
});
