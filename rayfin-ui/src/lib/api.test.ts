// Run with: npm test (uses Node's built-in test runner, no extra packages).
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { ApiError, apiFetch, setTokenProvider } from "./api.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  setTokenProvider(null);
});

function mockFetch(status: number, body: unknown) {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    })) as typeof fetch;
}

test("returns the parsed body on success", async () => {
  mockFetch(200, { ok: true });
  assert.deepEqual(await apiFetch("/x"), { ok: true });
});

test("a network failure is an unreachable ApiError", async () => {
  globalThis.fetch = (async () => {
    throw new TypeError("fetch failed");
  }) as typeof fetch;
  await assert.rejects(apiFetch("/x"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    return error.unreachable && error.code === "NETWORK";
  });
});

test("a 422 envelope yields field-level messages", async () => {
  mockFetch(422, {
    error: {
      code: "VALIDATION_ERROR",
      message: "Invalid prompt: too short",
      details: {
        errors: [
          { field: "prompt", message: "too short" },
          { field: "prompt", message: "second message is ignored" },
          { field: "workspace_id", message: "must be a GUID" },
        ],
      },
    },
  });
  await assert.rejects(apiFetch("/x"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 422);
    assert.equal(error.code, "VALIDATION_ERROR");
    assert.deepEqual(error.fields, {
      prompt: "too short",
      workspace_id: "must be a GUID",
    });
    return true;
  });
});

test("a non-JSON error still gives a readable message", async () => {
  globalThis.fetch = (async () =>
    new Response("<html>bad gateway</html>", { status: 502 })) as typeof fetch;
  await assert.rejects(apiFetch("/x"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    return error.status === 502 && /HTTP 502/.test(error.message);
  });
});

test("keeps the code and message from the API", async () => {
  mockFetch(404, { error: { code: "RUN_NOT_FOUND", message: "Run not found: abc" } });
  await assert.rejects(apiFetch("/runs/abc"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    return error.code === "RUN_NOT_FOUND" && error.message === "Run not found: abc";
  });
});

type Call = { auth: string | undefined };

/** A fake fetch that answers from a queue and records each call's Authorization header. */
function queueFetch(replies: Array<[number, unknown]>): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
    calls.push({ auth: (init?.headers as Record<string, string>)?.Authorization });
    const [status, body] = replies.shift() ?? [500, {}];
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return calls;
}

const expired = { error: { code: "TOKEN_EXPIRED", message: "The access token has expired" } };

test("sends no Authorization header when sign-in isn't in use", async () => {
  const calls = queueFetch([[200, { ok: true }]]);
  await apiFetch("/x");
  assert.equal(calls[0].auth, undefined);
});

test("sends the bearer token from the token provider", async () => {
  const calls = queueFetch([[200, { ok: true }]]);
  setTokenProvider(async () => "abc");
  await apiFetch("/x");
  assert.equal(calls[0].auth, "Bearer abc");
});

test("an expired token is refreshed silently and retried once", async () => {
  const calls = queueFetch([[401, expired], [200, { ok: true }]]);
  const forced: boolean[] = [];
  setTokenProvider(async (force) => {
    forced.push(force);
    return force ? "fresh" : "stale";
  });
  assert.deepEqual(await apiFetch("/x"), { ok: true });
  assert.deepEqual(forced, [false, true]);
  assert.deepEqual(calls.map((c) => c.auth), ["Bearer stale", "Bearer fresh"]);
});

test("a second TOKEN_EXPIRED is not retried again", async () => {
  const calls = queueFetch([[401, expired], [401, expired], [200, {}]]);
  setTokenProvider(async () => "t");
  await assert.rejects(apiFetch("/x"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    return error.code === "TOKEN_EXPIRED";
  });
  assert.equal(calls.length, 2);
});

test("other 401s are not retried", async () => {
  const calls = queueFetch([[401, { error: { code: "UNAUTHENTICATED", message: "no" } }], [200, {}]]);
  setTokenProvider(async () => "t");
  await assert.rejects(apiFetch("/x"));
  assert.equal(calls.length, 1);
});

test("a token provider failure is reported as an expired session", async () => {
  const calls = queueFetch([[200, {}]]);
  setTokenProvider(async () => {
    throw new Error("interaction required");
  });
  await assert.rejects(apiFetch("/x"), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    return error.status === 401 && error.code === "UNAUTHENTICATED";
  });
  assert.equal(calls.length, 0);
});
