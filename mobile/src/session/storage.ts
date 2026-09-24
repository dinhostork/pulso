import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

const REFRESH_TOKEN_KEY = "pulso.session.refresh.v1";

export interface RefreshTokenStore {
  read(): Promise<string | null>;
  write(refreshToken: string): Promise<void>;
  clear(): Promise<void>;
}

export interface SecureStoreApi {
  getItemAsync(key: string): Promise<string | null>;
  setItemAsync(key: string, value: string): Promise<void>;
  deleteItemAsync(key: string): Promise<void>;
}

export function createNativeRefreshTokenStore(
  secureStore: SecureStoreApi = SecureStore,
): RefreshTokenStore {
  return {
    read: () => secureStore.getItemAsync(REFRESH_TOKEN_KEY),
    write: (refreshToken) => secureStore.setItemAsync(REFRESH_TOKEN_KEY, refreshToken),
    clear: () => secureStore.deleteItemAsync(REFRESH_TOKEN_KEY),
  };
}

export function createWebMemoryRefreshTokenStore(): RefreshTokenStore {
  let refreshToken: string | null = null;
  return {
    read: async () => refreshToken,
    write: async (value) => {
      refreshToken = value;
    },
    clear: async () => {
      refreshToken = null;
    },
  };
}

export function createRefreshTokenStore(platform = Platform.OS): RefreshTokenStore {
  return platform === "web" ? createWebMemoryRefreshTokenStore() : createNativeRefreshTokenStore();
}

export class SerializedRefreshTokenStore implements RefreshTokenStore {
  private tail: Promise<void> = Promise.resolve();

  constructor(private readonly store: RefreshTokenStore) {}

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.tail.then(operation, operation);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  read(): Promise<string | null> {
    return this.enqueue(() => this.store.read());
  }

  write(refreshToken: string): Promise<void> {
    return this.enqueue(() => this.store.write(refreshToken));
  }

  clear(): Promise<void> {
    return this.enqueue(() => this.store.clear());
  }
}
