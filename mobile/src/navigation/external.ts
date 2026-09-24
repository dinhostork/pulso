import * as Linking from "expo-linking";

import { isSafePublisherUrl } from "@/api/urlSafety";

/**
 * `opened`: handed to the operating system. `rejected`: the target failed the
 * publisher-link rule and was never handed over. `failed`: the system could
 * not open it. The category is the only diagnostic; the URL is never logged.
 */
export type PublisherOpenResult = "opened" | "rejected" | "failed";

/**
 * The seam for leaving Pulso to read a publisher's page. The URL has already
 * passed the SourceArticle decoder; the same HTTP(S)/credential/private-address
 * rule is re-checked here as defense in depth. The OS browser opens only the
 * URL string — no WebView, proxy, header or Pulso credential — so the in-app
 * route stack is left untouched and returning to Pulso resumes the same screen.
 */
export async function openPublisherUrl(url: string): Promise<PublisherOpenResult> {
  if (!isSafePublisherUrl(url)) return "rejected";
  try {
    await Linking.openURL(url);
    return "opened";
  } catch {
    return "failed";
  }
}
