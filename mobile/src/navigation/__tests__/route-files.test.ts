interface DirectoryEntry {
  name: string;
  isDirectory(): boolean;
}

// Node's fs is used directly; the app's TypeScript config carries no Node types.
const fs = jest.requireActual<{
  readdirSync(path: string, options: { withFileTypes: true }): DirectoryEntry[];
}>("fs");

const APP_DIR = String(expect.getState().testPath).replace(
  /[\\/]navigation[\\/]__tests__[\\/][^\\/]+$/,
  "/app",
);

function files(directory: string, prefix = ""): string[] {
  return fs
    .readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) =>
      entry.isDirectory()
        ? files(`${directory}/${entry.name}`, `${prefix}${entry.name}/`)
        : [`${prefix}${entry.name}`],
    );
}

describe("src/app route discovery", () => {
  const all = files(APP_DIR);

  it("contains only route and layout files outside __tests__", () => {
    const routes = all.filter((file) => !file.split("/").includes("__tests__")).sort();
    expect(routes).toEqual([
      "(app)/(tabs)/_layout.tsx",
      "(app)/(tabs)/index.tsx",
      "(app)/(tabs)/saved.tsx",
      "(app)/_layout.tsx",
      "(app)/stories/[storyId]/index.tsx",
      "(app)/stories/[storyId]/sources.tsx",
      "+not-found.tsx",
      "_layout.tsx",
      "sign-in.tsx",
    ]);
  });

  it("keeps every test inside a __tests__ directory, which Metro excludes", () => {
    const tests = all.filter((file) => /\.(test|spec)\.[jt]sx?$/.test(file));
    expect(tests.length).toBeGreaterThan(0);
    for (const file of tests) expect(file.split("/")).toContain("__tests__");

    const { default: exclusionList } = jest.requireActual<{
      default: () => RegExp;
    }>("metro-config/private/defaults/exclusionList");
    for (const file of tests) expect(exclusionList().test(`/project/src/app/${file}`)).toBe(true);
  });
});
