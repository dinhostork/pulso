/**
 * `EXPO_PUBLIC_`-prefixed variables are inlined into the JS bundle at build
 * time (Expo/Metro convention) and are therefore public: never put a secret
 * behind this prefix. See mobile/README.md for device/emulator host
 * differences and how to override this without editing source.
 */
export const apiBaseUrl: string = process.env.EXPO_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
