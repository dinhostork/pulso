import * as Linking from "expo-linking";

/**
 * The seam for leaving Pulso to read a publisher's page (#49 wires it to
 * source rows). The URL has already passed the SourceArticle decoder's
 * HTTP(S)/credential/private-address checks; this re-checks the scheme only as
 * defense in depth. The OS opens the page outside the app — no WebView, proxy
 * or Authorization header — so the in-app route stack is left untouched and
 * returning to Pulso resumes the same screen.
 */
export async function openPublisherUrl(url: string): Promise<boolean> {
  if (!/^https?:\/\//i.test(url)) return false;
  try {
    await Linking.openURL(url);
    return true;
  } catch {
    return false;
  }
}
