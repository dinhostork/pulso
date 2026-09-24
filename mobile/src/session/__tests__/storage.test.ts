import * as SecureStore from "expo-secure-store";

import { validatedPendingReturnRoute } from "../routes";
import {
  createNativeRefreshTokenStore,
  createRefreshTokenStore,
  SerializedRefreshTokenStore,
  type RefreshTokenStore,
  type SecureStoreApi,
} from "../storage";

jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => undefined),
  deleteItemAsync: jest.fn(async () => undefined),
}));

describe("refresh-token storage", () => {
  beforeEach(() => jest.clearAllMocks());

  it("keeps the native refresh token in SecureStore under one versioned key", async () => {
    const secure: SecureStoreApi = {
      getItemAsync: jest.fn(async () => "refresh"),
      setItemAsync: jest.fn(async () => undefined),
      deleteItemAsync: jest.fn(async () => undefined),
    };
    const store = createNativeRefreshTokenStore(secure);
    await store.write("refresh");
    await expect(store.read()).resolves.toBe("refresh");
    await store.clear();
    expect(secure.setItemAsync).toHaveBeenCalledWith("pulso.session.refresh.v1", "refresh");
    expect(secure.getItemAsync).toHaveBeenCalledWith("pulso.session.refresh.v1");
    expect(secure.deleteItemAsync).toHaveBeenCalledWith("pulso.session.refresh.v1");
  });

  it("selects SecureStore on native platforms", async () => {
    await createRefreshTokenStore("ios").write("refresh");
    await createRefreshTokenStore("android").read();
    expect(SecureStore.setItemAsync).toHaveBeenCalledTimes(1);
    expect(SecureStore.getItemAsync).toHaveBeenCalledTimes(1);
  });

  it("keeps web credentials in memory only, so a reload requires sign-in", async () => {
    const beforeReload = createRefreshTokenStore("web");
    await beforeReload.write("refresh");
    await expect(beforeReload.read()).resolves.toBe("refresh");

    const afterReload = createRefreshTokenStore("web");
    await expect(afterReload.read()).resolves.toBeNull();
    expect(SecureStore.setItemAsync).not.toHaveBeenCalled();
    expect(SecureStore.getItemAsync).not.toHaveBeenCalled();
  });

  it("applies reads, writes and deletes in call order even when a write is slow", async () => {
    let value: string | null = null;
    let releaseWrite!: () => void;
    const slow: RefreshTokenStore = {
      read: async () => value,
      write: (next) =>
        new Promise<void>((resolve) => {
          releaseWrite = () => {
            value = next;
            resolve();
          };
        }),
      clear: async () => {
        value = null;
      },
    };
    const store = new SerializedRefreshTokenStore(slow);
    const write = store.write("refresh");
    const clear = store.clear();
    const read = store.read();
    await Promise.resolve();
    releaseWrite();
    await Promise.all([write, clear]);
    await expect(read).resolves.toBeNull();
  });

  it("continues the queue after a failed operation", async () => {
    const failing: RefreshTokenStore = {
      read: async () => "refresh",
      write: async () => {
        throw new Error("keychain unavailable");
      },
      clear: async () => undefined,
    };
    const store = new SerializedRefreshTokenStore(failing);
    await expect(store.write("refresh")).rejects.toThrow("keychain unavailable");
    await expect(store.read()).resolves.toBe("refresh");
  });
});

describe("pending return routes", () => {
  it.each(["/stories/42", "/stories/42/sources"])("accepts in-app route %s", (route) => {
    expect(validatedPendingReturnRoute(route)).toBe(route);
  });

  it.each([
    "https://evil.example/stories/1",
    "//evil.example/stories/1",
    "pulso://stories/1",
    "/stories/../settings",
    "/stories/1%2F..",
    "/stories/0",
    "/stories/1?next=https://evil.example",
    "/sign-in",
    "/",
    42,
    null,
    `/stories/${"1".repeat(300)}`,
  ])("rejects %p", (route) => {
    expect(validatedPendingReturnRoute(route)).toBeNull();
  });
});
