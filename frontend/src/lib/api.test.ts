import { describe, expect, it, vi, beforeEach } from "vitest";
import { clearStoredToken, ensureAuthToken, formatBytes, resolveApiBase, setStoredToken, uploadFileInChunks } from "./api";
import { MAX_UPLOAD_BYTES } from "./types";

const mockVercelBlobUpload = vi.fn();
vi.mock("@vercel/blob/client", () => ({
  upload: (...args: unknown[]) => mockVercelBlobUpload(...args),
}));

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
    expect(resolveApiBase("/api/v1", "preview-123.vercel.app")).toBe("/api/v1");
    expect(resolveApiBase("/api/", "preview-123.vercel.app")).toBe("/api/v1");
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

  it("rejects files exceeding MAX_UPLOAD_BYTES before initializing upload", async () => {
    const oversized = { name: "huge.pdf", size: MAX_UPLOAD_BYTES + 1 } as File;
    await expect(uploadFileInChunks(oversized, noopCallbacks)).rejects.toThrow("400 MB");
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

  it("uploads via official @vercel/blob/client SDK when upload_mode is vercel_blob", async () => {
    localStorage.setItem("teds_access_token", "test-token");
    mockVercelBlobUpload.mockResolvedValueOnce({
      url: "https://store.private.blob.vercel-storage.com/uploads/u1/s-blob/doc.pdf",
      pathname: "uploads/u1/s-blob/doc.pdf",
    });

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/uploads/init")) {
        return new Response(
          JSON.stringify({
            session_id: "s-blob",
            total_chunks: 1,
            chunk_size_bytes: 8_388_608,
            upload_mode: "vercel_blob",
            storage_key: "uploads/u1/s-blob/doc.pdf",
            handle_upload_url: "/api/blob/upload",
            upload_authorization_token: "signed-upload-auth-jwt",
          }),
          { status: 201 }
        );
      }
      if (url.endsWith("/uploads/s-blob/complete")) {
        return new Response(
          JSON.stringify({
            session_id: "s-blob",
            status: "completed",
            format_type: "pdf",
            sha256_hash: "sha256-blob-ok",
          }),
          { status: 200 }
        );
      }
      return new Response("Not found", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["%PDF-1.4 sample"], "doc.pdf", { type: "application/pdf" });
    const res = await uploadFileInChunks(file, noopCallbacks);
    expect(res.sessionId).toBe("s-blob");
    expect(res.formatType).toBe("pdf");
    expect(res.sha256).toBe("sha256-blob-ok");
    expect(mockVercelBlobUpload).toHaveBeenCalledTimes(1);
    const [pathnameArg, _fileArg, optsArg] = mockVercelBlobUpload.mock.calls[0];
    expect(pathnameArg).toBe("uploads/u1/s-blob/doc.pdf");
    expect(optsArg.access).toBe("private");
    expect(optsArg.handleUploadUrl).toBe("/api/blob/upload");
    expect(optsArg.multipart).toBe(true);
    expect(JSON.parse(optsArg.clientPayload)).toEqual({
      session_id: "s-blob",
      upload_authorization_token: "signed-upload-auth-jwt",
    });
  });
});
