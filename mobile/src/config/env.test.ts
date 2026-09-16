/** `env.ts` reads `process.env` at import time, so each case needs a fresh module. */
function importEnvWith(value: string | undefined): typeof import("./env") {
  jest.resetModules();
  if (value === undefined) {
    delete process.env.EXPO_PUBLIC_API_BASE_URL;
  } else {
    process.env.EXPO_PUBLIC_API_BASE_URL = value;
  }
  // A fresh require (not a static import) is required to re-evaluate the
  // module's top-level `process.env` read for each case.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require("./env") as typeof import("./env");
}

describe("apiBaseUrl", () => {
  const originalValue = process.env.EXPO_PUBLIC_API_BASE_URL;

  afterEach(() => {
    importEnvWith(originalValue);
  });

  it("falls back to localhost:8000 when unset, so the shell renders without a .env file", () => {
    const { apiBaseUrl } = importEnvWith(undefined);
    expect(apiBaseUrl).toBe("http://localhost:8000");
  });

  it("uses EXPO_PUBLIC_API_BASE_URL when set, with no source change required", () => {
    const { apiBaseUrl } = importEnvWith("http://10.0.2.2:8000");
    expect(apiBaseUrl).toBe("http://10.0.2.2:8000");
  });
});
