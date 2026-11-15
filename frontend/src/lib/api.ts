import {
  CHUNK_SIZE_BYTES,
  ExportArtifactRecord,
  JobRecord,
  JobSettings,
  MAX_UPLOAD_BYTES,
  PageResultRecord,
  StudyItemRecord,
  StudyProjectRecord,
} from "./types";

const PLACEHOLDER_HOSTS = [
  "example.com",
  "example.org",
  "example.net",
  ".example",
  "your-domain",
  "placeholder",
  "localhost",
  "127.0.0.1",
];

export function resolveApiBase(
  envUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL,
  hostname: string | undefined = typeof window !== "undefined" ? window.location.hostname : "localhost"
): string {
  const isLocalHost = !hostname || hostname === "localhost" || hostname === "127.0.0.1";
  const trimmed = (envUrl || "").trim().replace(/\/+$/, "");
  if (trimmed) {
    if (!isLocalHost) {
      let parsed: URL;
      try {
        parsed = new URL(trimmed);
      } catch {
        throw new Error(`NEXT_PUBLIC_API_URL is not a valid URL: "${trimmed}"`);
      }
      if (parsed.protocol !== "https:") {
        throw new Error("NEXT_PUBLIC_API_URL must use HTTPS in production.");
      }
      const apiHost = parsed.hostname.toLowerCase();
      if (PLACEHOLDER_HOSTS.some((p) => apiHost === p || apiHost.includes(p))) {
        throw new Error(`NEXT_PUBLIC_API_URL cannot use a placeholder or local host ("${apiHost}") in production.`);
      }
    }
    return trimmed.endsWith("/api/v1") ? trimmed : `${trimmed}/api/v1`;
  }
  if (isLocalHost) {
    return "http://localhost:8000/api/v1";
  }
  throw new Error(
    "NEXT_PUBLIC_API_URL is not configured. Set NEXT_PUBLIC_API_URL to your HTTPS backend API base (including /api/v1) before building the frontend."
  );
}

function getApiBase(): string {
  return resolveApiBase();
}

const TOKEN_STORAGE_KEY = "teds_access_token";
let authEpoch = 0;
let inFlightAuthPromise: Promise<string> | null = null;
let inFlightAuthEpoch = -1;

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setStoredToken(token: string): void {
  if (typeof window !== "undefined") {
    authEpoch += 1;
    inFlightAuthPromise = null;
    inFlightAuthEpoch = -1;
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
  }
}

export function clearStoredToken(): void {
  if (typeof window !== "undefined") {
    authEpoch += 1;
    inFlightAuthPromise = null;
    inFlightAuthEpoch = -1;
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export async function ensureAuthToken(): Promise<string> {
  if (typeof window === "undefined") {
    throw new Error("Authentication is only available in the browser.");
  }

  const existing = getStoredToken();
  if (existing) return existing;

  if (!inFlightAuthPromise || inFlightAuthEpoch !== authEpoch) {
    const requestEpoch = authEpoch;
    inFlightAuthEpoch = requestEpoch;
    inFlightAuthPromise = (async () => {
      const res = await fetch(`${getApiBase()}/auth/guest`, { method: "POST" });
      if (!res.ok) {
        throw new Error("Unable to initialize studio session.");
      }
      const data = await res.json();
      const token = data.access_token as string;
      if (authEpoch === requestEpoch) {
        localStorage.setItem(TOKEN_STORAGE_KEY, token);
        return token;
      }
      const current = getStoredToken();
      if (current) return current;
      throw new Error("Session changed while authenticating.");
    })().finally(() => {
      if (inFlightAuthEpoch === requestEpoch) {
        inFlightAuthPromise = null;
        inFlightAuthEpoch = -1;
      }
    });
  }
  return inFlightAuthPromise;
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await ensureAuthToken();
  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (!headers.has("Content-Type") && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const apiBase = getApiBase();
  const res = await fetch(`${apiBase}${path}`, { ...init, headers });
  if (res.status === 401 && typeof window !== "undefined") {
    clearStoredToken();
    const freshToken = await ensureAuthToken();
    headers.set("Authorization", `Bearer ${freshToken}`);
    const retry = await fetch(`${apiBase}${path}`, { ...init, headers });
    if (!retry.ok) {
      const errBody = await retry.json().catch(() => ({}));
      throw new Error(errBody.detail || `Request failed (${retry.status})`);
    }
    if (retry.status === 204) return undefined as T;
    return retry.json();
  }

  if (!res.ok) {
    const errBody = await res.json().catch(() => ({}));
    throw new Error(errBody.detail || `Request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export interface ChunkUploadController {
  pause: () => void;
  abort: () => void;
}

export async function uploadFileInChunks(
  file: File,
  callbacks: {
    onSessionCreated?: (sessionId: string) => void;
    onProgress: (uploadedBytes: number, totalBytes: number, percent: number) => void;
    shouldPause: () => boolean;
    shouldCancel: () => boolean;
    resumeSessionId?: string;
  }
): Promise<{ sessionId: string; formatType: string; sha256: string }> {
  if (file.size === 0) {
    throw new Error("Cannot upload an empty file (0 bytes).");
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error(`File "${file.name}" (${file.size.toLocaleString()} bytes) exceeds the 500 MB (500,000,000 bytes) limit.`);
  }

  const token = await ensureAuthToken();
  let sessionId = callbacks.resumeSessionId;
  let effectiveChunkSize = CHUNK_SIZE_BYTES;
  let totalChunks = Math.max(1, Math.ceil(file.size / effectiveChunkSize));
  let receivedSet = new Set<number>();
  let uploadMode: "proxy" | "direct_s3" = "proxy";

  if (sessionId) {
    const statusRes = await apiFetch<{
      session_id: string;
      chunk_size_bytes: number;
      total_chunks: number;
      received_chunks: number[];
      upload_mode?: "proxy" | "direct_s3";
    }>(`/uploads/${sessionId}`);
    effectiveChunkSize = statusRes.chunk_size_bytes || CHUNK_SIZE_BYTES;
    totalChunks = statusRes.total_chunks;
    receivedSet = new Set(statusRes.received_chunks);
    uploadMode = statusRes.upload_mode || "proxy";
  } else {
    const initRes = await apiFetch<{
      session_id: string;
      chunk_size_bytes: number;
      total_chunks: number;
      upload_mode?: "proxy" | "direct_s3";
    }>("/uploads/init", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        expected_size_bytes: file.size,
        chunk_size_bytes: CHUNK_SIZE_BYTES,
      }),
    });
    sessionId = initRes.session_id;
    effectiveChunkSize = initRes.chunk_size_bytes || CHUNK_SIZE_BYTES;
    totalChunks = initRes.total_chunks;
    uploadMode = initRes.upload_mode || "proxy";
    callbacks.onSessionCreated?.(sessionId);
  }

  for (let idx = 0; idx < totalChunks; idx++) {
    if (callbacks.shouldCancel()) {
      await apiFetch(`/uploads/${sessionId}`, { method: "DELETE" }).catch(() => {});
      throw new Error("Upload cancelled");
    }
    if (callbacks.shouldPause()) {
      await apiFetch(`/uploads/${sessionId}/pause`, { method: "POST" }).catch(() => {});
      throw new Error("UPLOAD_PAUSED");
    }
    if (receivedSet.has(idx)) {
      const doneBytes = Math.min(file.size, (idx + 1) * effectiveChunkSize);
      callbacks.onProgress(doneBytes, file.size, Math.round((doneBytes / file.size) * 100));
      continue;
    }

    const start = idx * effectiveChunkSize;
    const end = Math.min(file.size, start + effectiveChunkSize);
    const slice = file.slice(start, end);

    if (uploadMode === "direct_s3") {
      const partUrlRes = await apiFetch<{ upload_url: string; part_number: number }>(
        `/uploads/${sessionId}/part-url/${idx}`
      );
      const s3Res = await fetch(partUrlRes.upload_url, {
        method: "PUT",
        body: slice,
      });
      if (!s3Res.ok) {
        throw new Error(`Direct storage upload failed for chunk ${idx} (${s3Res.status})`);
      }
      const etag = s3Res.headers.get("ETag") || s3Res.headers.get("etag") || "";
      if (!etag) {
        throw new Error(
          `Direct storage response missing ETag header for chunk ${idx}; verify S3 CORS ExposeHeaders includes ETag`
        );
      }
      await apiFetch(`/uploads/${sessionId}/parts/${idx}`, {
        method: "POST",
        body: JSON.stringify({ etag }),
      });
    } else {
      const formData = new FormData();
      formData.append("chunk", slice, `part_${idx}`);

      const res = await fetch(`${getApiBase()}/uploads/${sessionId}/chunk/${idx}`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || `Chunk ${idx} upload failed`);
      }
    }

    callbacks.onProgress(end, file.size, Math.round((end / file.size) * 100));
  }

  let completed = await apiFetch<{
    session_id: string;
    status: string;
    format_type: string;
    sha256_hash: string;
  }>(`/uploads/${sessionId}/complete`, { method: "POST" });

  const deadline = Date.now() + 300_000;
  while (completed.status === "processing") {
    if (Date.now() > deadline) {
      throw new Error("Upload finalization timed out");
    }
    await new Promise((r) => setTimeout(r, 400));
    completed = await apiFetch<{
      session_id: string;
      status: string;
      format_type: string;
      sha256_hash: string;
    }>(`/uploads/${sessionId}`);
  }
  if (completed.status !== "completed") {
    throw new Error(`Upload validation failed (status: ${completed.status})`);
  }

  return {
    sessionId: completed.session_id,
    formatType: completed.format_type,
    sha256: completed.sha256_hash,
  };
}

export async function createJob(
  uploadSessionId: string,
  settings: Partial<JobSettings>,
  batchId?: string,
  password?: string
): Promise<JobRecord> {
  return apiFetch<JobRecord>("/jobs", {
    method: "POST",
    body: JSON.stringify({
      upload_session_id: uploadSessionId,
      batch_id: batchId,
      settings,
      password: password || undefined,
    }),
  });
}

export async function listJobs(page = 1, pageSize = 20): Promise<{ total: number; items: JobRecord[] }> {
  return apiFetch(`/jobs?page=${page}&page_size=${pageSize}`);
}

export async function getJob(jobId: string): Promise<JobRecord> {
  return apiFetch(`/jobs/${jobId}`);
}

export async function cancelJob(jobId: string): Promise<JobRecord> {
  return apiFetch(`/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function retryJob(jobId: string, password?: string): Promise<JobRecord> {
  return apiFetch(`/jobs/${jobId}/retry`, {
    method: "POST",
    body: JSON.stringify({ password: password || undefined }),
  });
}

export async function deleteJobNow(jobId: string): Promise<void> {
  return apiFetch(`/jobs/${jobId}`, { method: "DELETE" });
}

export async function getJobPages(jobId: string): Promise<{ job_id: string; total_pages: number; pages: PageResultRecord[] }> {
  return apiFetch(`/jobs/${jobId}/pages`);
}

export async function fetchPageImageBlobUrl(jobId: string, pageNumber: number, kind: "preview" | "thumbnail" = "preview"): Promise<string> {
  const token = await ensureAuthToken();
  const res = await fetch(`${getApiBase()}/jobs/${jobId}/pages/${pageNumber}/image?kind=${kind}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("Preview image unavailable");
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

export async function savePageReview(
  jobId: string,
  pageNumber: number,
  editedText: string,
  isAutosave = false,
  expectedVersion?: number
) {
  return apiFetch<{ saved: boolean; draft_version: number }>(`/jobs/${jobId}/pages/${pageNumber}`, {
    method: "PUT",
    body: JSON.stringify({
      edited_text: editedText,
      is_autosave: isAutosave,
      expected_version: expectedVersion,
    }),
  });
}

export async function reviewPageGrammar(jobId: string, pageNumber: number) {
  return apiFetch<{
    page_number: number;
    original_text: string;
    corrected_text: string;
    has_changes: boolean;
    notes: Array<{ type: string; description: string; original?: string; suggested?: string }>;
    diff: string[];
  }>(`/jobs/${jobId}/pages/${pageNumber}/grammar-review`, {
    method: "POST",
  });
}

export async function reprocessPage(
  jobId: string,
  pageNumber: number,
  options: { language: string; crop_box?: [number, number, number, number] }
) {
  return apiFetch<{ text: string; confidence_avg?: number; warnings: string[] }>(
    `/jobs/${jobId}/pages/${pageNumber}/reprocess`,
    {
      method: "POST",
      body: JSON.stringify(options),
    }
  );
}

export async function previewLegacyTamil(jobId: string, sourceEncoding: "bamini" | "tab", pageNumber?: number) {
  return apiFetch<{ previews: Array<{ page_number: number; original_preview: string; converted_preview: string; warnings: string[] }> }>(
    `/jobs/${jobId}/legacy-tamil/preview`,
    { method: "POST", body: JSON.stringify({ source_encoding: sourceEncoding, page_number: pageNumber }) }
  );
}

export async function applyLegacyTamil(jobId: string, sourceEncoding: "bamini" | "tab", pageNumber?: number) {
  return apiFetch<{ converted_pages: number }>(
    `/jobs/${jobId}/legacy-tamil/apply`,
    { method: "POST", body: JSON.stringify({ source_encoding: sourceEncoding, page_number: pageNumber }) }
  );
}

export async function previewHeadersFooters(jobId: string) {
  return apiFetch<{ headers: string[]; footers: string[] }>(`/jobs/${jobId}/headers-footers/preview`, { method: "POST" });
}

export async function applyHeadersFooters(jobId: string, headers: string[], footers: string[]) {
  return apiFetch<{ updated_pages: number }>(`/jobs/${jobId}/headers-footers/apply`, {
    method: "POST",
    body: JSON.stringify({ headers, footers, remove_page_numbers: true }),
  });
}

async function pollExportUntilReady(jobId: string, artifactId: string): Promise<ExportArtifactRecord> {
  const deadline = Date.now() + 300_000;
  while (true) {
    const art = await apiFetch<ExportArtifactRecord>(`/jobs/${jobId}/exports/${artifactId}`);
    if (art.status === "completed") return art;
    if (art.status === "failed") throw new Error("Export generation failed");
    if (Date.now() > deadline) throw new Error("Export generation timed out");
    await new Promise((r) => setTimeout(r, 400));
  }
}

export async function createExport(
  jobId: string,
  exportFormat: string,
  qualityPreset: string,
  options: Record<string, unknown> = {}
): Promise<ExportArtifactRecord> {
  const created = await apiFetch<ExportArtifactRecord>(`/jobs/${jobId}/exports`, {
    method: "POST",
    body: JSON.stringify({ export_format: exportFormat, quality_preset: qualityPreset, options }),
  });
  if (created.status === "processing") {
    return pollExportUntilReady(jobId, created.artifact_id);
  }
  return created;
}

function resolveDownloadUrl(downloadUrl: string): string {
  if (/^https?:\/\//i.test(downloadUrl)) {
    return downloadUrl;
  }
  const baseOrigin = getApiBase().replace(/\/api\/v1$/, "");
  const normalizedPath = downloadUrl.startsWith("/") ? downloadUrl : `/${downloadUrl}`;
  return `${baseOrigin}${normalizedPath}`;
}

export async function getExportDownloadUrl(jobId: string, artifactId: string): Promise<string> {
  const res = await apiFetch<{ download_url: string }>(`/jobs/${jobId}/exports/${artifactId}/link`);
  return resolveDownloadUrl(res.download_url);
}

export async function getOriginalDownloadUrl(jobId: string): Promise<string> {
  const res = await apiFetch<{ download_url: string }>(`/jobs/${jobId}/original/link`);
  return resolveDownloadUrl(res.download_url);
}

export async function createBatchZip(jobIds: string[], exportFormat = "txt", qualityPreset = "high") {
  const res = await apiFetch<{ artifact_id: string; job_id: string; status: string; file_size_bytes: number }>(
    "/jobs/batch/zip",
    {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds, export_format: exportFormat, quality_preset: qualityPreset }),
    }
  );
  const ready = res.status === "processing" ? await pollExportUntilReady(res.job_id, res.artifact_id) : res;
  const linkRes = await apiFetch<{ download_url: string }>(`/jobs/${res.job_id}/exports/${res.artifact_id}/link`);
  return {
    artifact_id: res.artifact_id,
    file_size_bytes: ready.file_size_bytes,
    download_url: linkRes.download_url,
    full_url: resolveDownloadUrl(linkRes.download_url),
  };
}

export async function getStudyProviderStatus(): Promise<{
  configured: boolean;
  provider: string | null;
  instructions: string;
}> {
  return apiFetch("/study/status");
}

export async function createStudyProject(payload: {
  job_id: string;
  title?: string;
  language?: string;
  difficulty?: string;
  mark_categories?: number[];
  question_count?: number | null;
}): Promise<StudyProjectRecord> {
  return apiFetch("/study/projects", { method: "POST", body: JSON.stringify(payload) });
}

export async function listStudyProjects() {
  return apiFetch<{ items: Array<{ project_id: string; job_id: string; title: string; status: string; created_at: string }> }>("/study/projects");
}

export async function getStudyProject(projectId: string): Promise<StudyProjectRecord> {
  return apiFetch(`/study/projects/${projectId}`);
}

export async function updateStudyItem(itemId: string, payload: Partial<StudyItemRecord>): Promise<StudyItemRecord> {
  return apiFetch(`/study/items/${itemId}`, { method: "PATCH", body: JSON.stringify(payload) });
}

export async function regenerateStudyProject(projectId: string): Promise<StudyProjectRecord> {
  return apiFetch(`/study/projects/${projectId}/regenerate`, { method: "POST" });
}

export async function exportStudyProjectBlob(
  projectId: string,
  exportFormat: string,
  layout = "complete_study_guide",
  targetTotalMarks?: number
): Promise<Blob> {
  const token = await ensureAuthToken();
  const res = await fetch(`${getApiBase()}/study/projects/${projectId}/export`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ export_format: exportFormat, layout, target_total_marks: targetTotalMarks }),
  });
  if (!res.ok) throw new Error("Study export failed");
  return res.blob();
}

export async function getCapabilities() {
  return apiFetch<{
    max_upload_bytes: number;
    installed_ocr_languages: string[];
    supported_imports: string[];
    supported_exports: string[];
  }>("/capabilities");
}

export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}
