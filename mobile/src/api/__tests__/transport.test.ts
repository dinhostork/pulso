import { ApiError } from "../errors";
import { buildApiUrl, createTransport, type CredentialHooks } from "../transport";

const errorContract = require("../../../../docs/contracts/mobile-feed/errors.json") as {
  examples: { status: number; body: { code: string; detail: string } }[];
};

function response(body: string, init: ResponseInit = {}): Response {
  return new Response(body, { status: 200, ...init });
}

describe("API URL construction", () => {
  it.each([
    ["https://api.example.com", "/api/feed", "https://api.example.com/api/feed"],
    ["https://api.example.com/", "/api/auth/me", "https://api.example.com/api/auth/me"],
  ])("joins %s and %s", (base, path, expected) => {
    expect(buildApiUrl(base, path)).toBe(expected);
  });

  it.each([
    "https://foreign.example/api/feed",
    "//foreign.example/api/feed",
    "/other/feed",
    "/api/../secret",
    "/api/feed#fragment",
    "/api/feed\\other",
  ])("rejects non-approved or absolute route %s", (path) => {
    expect(() => buildApiUrl("https://api.example.com", path)).toThrow(ApiError);
  });

  it("allows local HTTP only when the development boundary opts in", () => {
    expect(() => buildApiUrl("http://localhost:8000", "/api/feed")).toThrow(ApiError);
    expect(buildApiUrl("http://localhost:8000/", "/api/feed", true)).toBe(
      "http://localhost:8000/api/feed",
    );
  });
});

describe("fetch transport", () => {
  it("attaches a bearer credential only after relative-path validation", async () => {
    const fetchMock = jest.fn(async () => response("{}"));
    const credentials: CredentialHooks = {
      accessToken: () => "secret-access-token",
      sessionEpoch: () => 1,
    };
    const transport = createTransport({
      baseUrl: "https://api.example.com",
      fetch: fetchMock as typeof fetch,
      credentials,
    });
    await transport.request({ operation: "test", path: "/api/feed" });
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.com/api/feed",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer secret-access-token" }),
      }),
    );
    await expect(
      transport.request({ operation: "test", path: "https://foreign.example/api/feed" }),
    ).rejects.toMatchObject({ kind: "configuration" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("normalizes timeout and caller cancellation separately", async () => {
    jest.useFakeTimers();
    const abortingFetch = jest.fn(
      (_url: string | URL | Request, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const transport = createTransport({
      baseUrl: "https://api.example.com",
      fetch: abortingFetch as typeof fetch,
    });
    const timed = transport.request({ operation: "feed.read", path: "/api/feed", timeoutMs: 10 });
    const timedExpectation = expect(timed).rejects.toMatchObject({
      kind: "timeout",
      operation: "feed.read",
    });
    await jest.advanceTimersByTimeAsync(10);
    await timedExpectation;

    const controller = new AbortController();
    const cancelled = transport.request({
      operation: "story.read",
      path: "/api/stories/1",
      signal: controller.signal,
    });
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ kind: "aborted" });
    jest.useRealTimers();
  });

  it.each([204, 205])("accepts an empty %s response", async (status) => {
    const transport = createTransport({
      baseUrl: "https://api.example.com",
      fetch: jest.fn(async () => new Response(null, { status })) as typeof fetch,
    });
    await expect(
      transport.request<void>({ operation: "bookmark.remove", path: "/api/bookmarks/1" }),
    ).resolves.toBeUndefined();
  });

  it("distinguishes malformed JSON from malformed decoded DTOs", async () => {
    const malformedJson = createTransport({
      baseUrl: "https://api.example.com",
      fetch: jest.fn(async () => response("{")) as typeof fetch,
    });
    await expect(
      malformedJson.request({ operation: "feed.read", path: "/api/feed" }),
    ).rejects.toMatchObject({ kind: "malformed_json" });

    const malformedDto = createTransport({
      baseUrl: "https://api.example.com",
      fetch: jest.fn(async () => response("{}")) as typeof fetch,
    });
    await expect(
      malformedDto.request({
        operation: "feed.read",
        path: "/api/feed",
        decode: () => {
          throw new Error("bad DTO");
        },
      }),
    ).rejects.toMatchObject({ kind: "malformed_dto" });
  });

  it("normalizes product and authentication HTTP errors without retaining bodies", async () => {
    const transport = createTransport({
      baseUrl: "https://api.example.com",
      fetch: jest.fn(async () =>
        response(
          JSON.stringify({
            code: "validation_error",
            detail: "The request is invalid.",
            fields: { limit: ["Must be between 1 and 50."] },
            token: "must-not-survive",
          }),
          { status: 429, headers: { "Retry-After": "12" } },
        ),
      ) as typeof fetch,
    });
    const failure = await transport
      .request({ operation: "feed.read", path: "/api/feed" })
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({
      kind: "http",
      status: 429,
      code: "validation_error",
      detail: "The request is invalid.",
      retryAfterSeconds: 12,
      fields: { limit: ["Must be between 1 and 50."] },
    });
    expect(JSON.stringify(failure)).not.toContain("must-not-survive");
  });

  it("normalizes every repository-owned product error fixture", async () => {
    for (const example of errorContract.examples) {
      const transport = createTransport({
        baseUrl: "https://api.example.com",
        fetch: jest.fn(async () =>
          response(JSON.stringify(example.body), { status: example.status }),
        ) as typeof fetch,
      });
      await expect(
        transport.request({ operation: "contract.error", path: "/api/feed" }),
      ).rejects.toMatchObject({
        kind: "http",
        status: example.status,
        code: example.body.code,
        detail: example.body.detail,
      });
    }
  });

  it("replays one 401 only while the captured session epoch remains current", async () => {
    let token = "old";
    const credentials: CredentialHooks = {
      accessToken: () => token,
      sessionEpoch: () => 7,
      refreshAfterUnauthorized: jest.fn(async () => {
        token = "new";
        return token;
      }),
    };
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(response(JSON.stringify({ detail: "expired" }), { status: 401 }))
      .mockResolvedValueOnce(response(JSON.stringify({ ok: true })));
    const transport = createTransport({
      baseUrl: "https://api.example.com",
      fetch: fetchMock as typeof fetch,
      credentials,
    });
    await expect(transport.request({ operation: "test", path: "/api/feed" })).resolves.toEqual({
      ok: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer new");
  });
});
