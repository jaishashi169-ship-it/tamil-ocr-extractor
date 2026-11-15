import { describe, expect, it, vi, beforeEach } from "vitest";
import { clearStoredToken, ensureAuthToken, formatBytes, resolveApiBase, setStoredToken, uploadFileInChunks } from "./api";
import { MAX_UPLOAD_BYTES } from "./types";

const noopCallbacks = {
  onProgress: () => {},
  shouldPause: () => false,
  shouldCancel: () => false,
};

describe("Frontend API & chunked upload behavior", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
      clear: () => {
        Object.keys(store).forEach((k) => delete store[k]);
      },
    });
  });

  it("formats byte sizes consistently with decimal MB", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(500_000_000)).toBe("500.0 MB");
  });

  it("normalizes NEXT_PUBLIC_API_URL without duplicating /api/v1 and rejects missing or placeholder URLs in production", () => {
    expect(resolveApiBase("https://api.tamilstudio.app", "app.vercel.app")).toBe("https://api.tamilstudio.app/api/v1");
    expect(resolveApiBase("https://api.tamilstudio.app/api/v1/", "app.vercel.app")).toBe(
      "https://api.tamilstudio.app/api/v1"
    );
    expect(resolveApiBase(undefined, "localhost")).toBe("http://localhost:8000/api/v1");
    expect(() => resolveApiBase(undefined, "app.vercel.app")).toThrow("NEXT_PUBLIC_API_URL is not configured");
    expect(() => resolveApiBase("https://api.example.com/api/v1", "app.vercel.app")).toThrow("placeholder");
    expect(() => resolveApiBase("http://api.tamilstudio.app/api/v1", "app.vercel.app")).toThrow("HTTPS");
  });

  it("deduplicates concurrent auth requests, clears on failure, and ignores stale results after logout", async () => {
    clearStoredToken();
    vi.stubGlobal("window", { location: { hostname: "localhost" } });

    let resolveFetch!: (val: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        })
    );
    vi.stubGlobal("fetch", fetchMock);

    const p1 = ensureAuthToken();
    const p2 = ensureAuthToken();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Simulate logout/session change while request is in flight
    clearStoredToken();
    resolveFetch(new Response(JSON.stringify({ access_token: "stale-token" }), { status: 200 }));
    await expect(p1).rejects.toThrow("Session changed");
    await expect(p2).rejects.toThrow("Session changed");
    expect(localStorage.getItem("teds_access_token")).toBeNull();

    // Next attempt after failure/clear should issue a fresh request
    const fetchFresh = vi.fn(async () => new Response(JSON.stringify({ access_token: "fresh-token" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchFresh);
    const tok = await ensureAuthToken();
    expect(tok).toBe("fresh-token");
    expect(fetchFresh).toHaveBeenCalledTimes(1);
    setStoredToken("manual-token");
    expect(await ensureAuthToken()).toBe("manual-token");
  });

  it("rejects files exceeding 500,000,000 bytes before initializing upload", async () => {
    const oversized = { name: "huge.pdf", size: MAX_UPLOAD_BYTES + 1 } as File;
    await expect(uploadFileInChunks(oversized, noopCallbacks)).rejects.toThrow("500 MB");
  });

  it("supports chunked upload, pause, and resume via /uploads endpoints", async () => {
    localStorage.setItem("teds_access_token", "test-token");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/guest")) {
        return new Response(JSON.stringify({ access_token: "test-token" }), { status: 200 });
      }
      if (url.endsWith("/uploads/init")) {
        return new Response(
          JSON.stringify({ session_id: "sess-1", total_chunks: 2, chunk_size_bytes: 5 }),
          { status: 201 }
        );
      }
      if (url.includes("/chunk/")) {
        return new Response(JSON.stringify({ session_id: "sess-1", received_chunks: [0] }), { status: 200 });
      }
      if (url.endsWith("/pause")) {
        return new Response(JSON.stringify({ session_id: "sess-1", status: "paused" }), { status: 200 });
      }
      if (url.endsWith("/uploads/sess-1")) {
        return new Response(
          JSON.stringify({ session_id: "sess-1", total_chunks: 2, chunk_size_bytes: 5, received_chunks: [0] }),
          { status: 200 }
        );
      }
      if (url.endsWith("/complete")) {
        return new Response(
          JSON.stringify({ session_id: "sess-1", status: "completed", format_type: "txt", sha256_hash: "abc123" }),
          { status: 200 }
        );
      }
      return new Response("Not found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["hello world"], "notes.txt", { type: "text/plain" });
    let pausedAfterFirst = false;

    await expect(
      uploadFileInChunks(file, {
        shouldPause: () => pausedAfterFirst,
        shouldCancel: () => false,
        onProgress: () => {
          pausedAfterFirst = true;
        },
      })
    ).rejects.toThrow("UPLOAD_PAUSED");

    const resumed = await uploadFileInChunks(file, {
      ...noopCallbacks,
      resumeSessionId: "sess-1",
    });
    expect(resumed.sessionId).toBe("sess-1");
    expect(resumed.formatType).toBe("txt");
    expect(resumed.sha256).toBe("abc123");
  });
});
