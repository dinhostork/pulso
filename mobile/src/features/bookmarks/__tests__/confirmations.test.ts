import { clearAccountServerState, createQueryClient } from "@/server-state/query";

import {
  confirmationMark,
  hasConfirmationsSince,
  recordConfirmation,
  withConfirmedBookmarks,
} from "../confirmations";

const card = (id: string, bookmarked: boolean) => ({ id, viewer: { bookmarked } });

describe("confirmed bookmark ordering", () => {
  it("lets a confirmation win only over responses to requests started before it", () => {
    const client = createQueryClient();
    const before = confirmationMark();
    recordConfirmation(client, "1", "42", true);
    const after = confirmationMark();
    recordConfirmation(client, "1", "43", false);

    const stale = [card("42", false), card("43", true), card("44", false)];
    const merged = withConfirmedBookmarks(client, "1", before, stale);
    expect(merged).toEqual([card("42", true), card("43", false), card("44", false)]);
    // An unrelated Story keeps the response's own object and value.
    expect(merged[2]).toBe(stale[2]);

    // A request started after the Story 42 confirmation carries newer server state.
    const newer = [card("42", false), card("43", true)];
    expect(withConfirmedBookmarks(client, "1", after, newer)).toEqual([
      card("42", false),
      card("43", false),
    ]);
    expect(withConfirmedBookmarks(client, "1", confirmationMark(), newer)).toBe(newer);
    expect(hasConfirmationsSince(client, "1", before)).toBe(true);
    expect(hasConfirmationsSince(client, "1", confirmationMark())).toBe(false);
    client.clear();
  });

  it("is scoped to the account and cleared at its session boundary", async () => {
    const client = createQueryClient();
    const mark = confirmationMark();
    recordConfirmation(client, "1", "42", true);
    const other = [card("42", false)];
    expect(withConfirmedBookmarks(client, "2", mark, other)).toBe(other);

    await clearAccountServerState(client, "1");
    const own = [card("42", false)];
    expect(withConfirmedBookmarks(client, "1", mark, own)).toBe(own);
    client.clear();
  });
});
