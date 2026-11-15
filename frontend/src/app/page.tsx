"use client";

import React, { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, Loader2, Lock, XCircle } from "lucide-react";
import {
  cancelJob,
  createExport,
  createJob,
  deleteJobNow,
  getJob,
  getJobPages,
  listJobs,
  retryJob,
  uploadFileInChunks,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import {
  ExportArtifactRecord,
  JobRecord,
  JobSettings,
  MAX_UPLOAD_BYTES,
  PageResultRecord,
  UploadQueueItem,
  WorkflowAction,
  WorkflowStep,
} from "@/lib/types";
import { useStudio } from "@/components/StudioContext";
import { UploadCard } from "@/components/UploadCard";
import { OptionsStep } from "@/components/OptionsStep";
import { ReviewStep } from "@/components/ReviewStep";
import { DownloadStep } from "@/components/DownloadStep";
import { RecentJobs } from "@/components/RecentJobs";

export default function HomePage() {
  const { locale } = useStudio();
  const router = useRouter();

  const [step, setStep] = useState<WorkflowStep>("upload");
  const [selectedAction, setSelectedAction] = useState<WorkflowAction>("extract");
  const [queue, setQueue] = useState<UploadQueueItem[]>([]);
  const pauseFlags = useRef<Record<string, boolean>>({});
  const cancelFlags = useRef<Record<string, boolean>>({});

  const [settings, setSettings] = useState<JobSettings>({
    language: "tam+eng",
    conversion_mode: "clean_editable",
    workflow_action: "extract",
    target_output_format: "docx",
    quality_preset: "high",
    auto_rotate: true,
    deskew: true,
    denoise: false,
    enhance_contrast: true,
    multi_column: true,
  });
  const [password, setPassword] = useState("");
  const [isStarting, setIsStarting] = useState(false);

  const [activeJob, setActiveJob] = useState<JobRecord | null>(null);
  const [batchJobIds, setBatchJobIds] = useState<string[]>([]);
  const [pages, setPages] = useState<PageResultRecord[]>([]);
  const [latestArtifact, setLatestArtifact] = useState<ExportArtifactRecord | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobRecord[]>([]);
  const [promptPassword, setPromptPassword] = useState("");
  const [backendError, setBackendError] = useState<string | null>(null);

  const refreshRecent = async () => {
    try {
      const res = await listJobs(1, 15);
      setRecentJobs(res.items);
      setBackendError(null);
    } catch (err) {
      setBackendError(
        err instanceof Error
          ? err.message
          : "Backend API is currently unavailable. Please verify NEXT_PUBLIC_API_URL and backend health."
      );
    }
  };

  useEffect(() => {
    refreshRecent();
  }, []);

  // Poll active job while queued or running
  useEffect(() => {
    if (!activeJob || (activeJob.status !== "queued" && activeJob.status !== "running")) return;
    const timer = setInterval(async () => {
      try {
        const updated = await getJob(activeJob.job_id);
        setActiveJob(updated);
        if (updated.status === "completed") {
          const pagesRes = await getJobPages(updated.job_id);
          setPages(pagesRes.pages);
          setStep("review");
          refreshRecent();
        } else if (updated.status === "failed" || updated.status === "cancelled") {
          refreshRecent();
        }
      } catch {
        // ignore transient poll error
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [activeJob]);

  const startChunkedUpload = async (item: UploadQueueItem) => {
    pauseFlags.current[item.localId] = false;
    cancelFlags.current[item.localId] = false;

    setQueue((prev) =>
      prev.map((q) => (q.localId === item.localId ? { ...q, status: "uploading", error: undefined } : q))
    );

    try {
      const res = await uploadFileInChunks(item.file, {
        resumeSessionId: item.sessionId,
        onSessionCreated: (sid) => {
          setQueue((prev) => prev.map((q) => (q.localId === item.localId ? { ...q, sessionId: sid } : q)));
        },
        onProgress: (uploadedBytes, _total, percent) => {
          setQueue((prev) =>
            prev.map((q) =>
              q.localId === item.localId ? { ...q, uploadedBytes, progressPercent: percent } : q
            )
          );
        },
        shouldPause: () => Boolean(pauseFlags.current[item.localId]),
        shouldCancel: () => Boolean(cancelFlags.current[item.localId]),
      });

      setQueue((prev) =>
        prev.map((q) =>
          q.localId === item.localId
            ? {
                ...q,
                status: "completed",
                progressPercent: 100,
                uploadedBytes: item.file.size,
                sessionId: res.sessionId,
                formatType: res.formatType,
                sha256: res.sha256,
              }
            : q
        )
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      if (msg === "UPLOAD_PAUSED") {
        setQueue((prev) => prev.map((q) => (q.localId === item.localId ? { ...q, status: "paused" } : q)));
      } else if (msg === "Upload cancelled") {
        setQueue((prev) => prev.map((q) => (q.localId === item.localId ? { ...q, status: "cancelled" } : q)));
      } else {
        setQueue((prev) =>
          prev.map((q) => (q.localId === item.localId ? { ...q, status: "error", error: msg } : q))
        );
      }
    }
  };

  const handleAddFiles = (fileList: FileList | File[]) => {
    const incoming = Array.from(fileList);
    const newItems: UploadQueueItem[] = incoming.map((file) => {
      const localId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      if (file.size > MAX_UPLOAD_BYTES) {
        return {
          localId,
          file,
          status: "error",
          progressPercent: 0,
          uploadedBytes: 0,
          error: `Exceeds 500 MB limit (${file.size.toLocaleString()} / ${MAX_UPLOAD_BYTES.toLocaleString()} bytes).`,
        };
      }
      if (file.size === 0) {
        return {
          localId,
          file,
          status: "error",
          progressPercent: 0,
          uploadedBytes: 0,
          error: "File is empty (0 bytes).",
        };
      }
      return { localId, file, status: "pending", progressPercent: 0, uploadedBytes: 0 };
    });

    setQueue((prev) => [...prev, ...newItems]);
    newItems.filter((i) => i.status === "pending").forEach((i) => startChunkedUpload(i));
  };

  const handleStartProcessing = async () => {
    const ready = queue.filter((q) => q.status === "completed" && q.sessionId);
    if (ready.length === 0) return;

    setIsStarting(true);
    try {
      const batchId = ready.length > 1 ? `batch-${Date.now()}` : undefined;
      const createdIds: string[] = [];
      let primaryJob: JobRecord | null = null;

      for (const item of ready) {
        const job = await createJob(
          item.sessionId!,
          { ...settings, workflow_action: selectedAction },
          batchId,
          password
        );
        createdIds.push(job.job_id);
        if (!primaryJob) primaryJob = job;
      }

      setBatchJobIds(createdIds);
      if (primaryJob) {
        setActiveJob(primaryJob);
        if (primaryJob.status === "completed") {
          const pagesRes = await getJobPages(primaryJob.job_id);
          setPages(pagesRes.pages);
          setStep("review");
        }
      }
      refreshRecent();
    } finally {
      setIsStarting(false);
    }
  };

  const handleOpenExistingJob = async (job: JobRecord) => {
    setActiveJob(job);
    const pagesRes = await getJobPages(job.job_id);
    setPages(pagesRes.pages);
    setLatestArtifact(null);
    setStep("review");
  };

  const handleContinueToDownload = async () => {
    if (!activeJob) return;
    try {
      const art = await createExport(
        activeJob.job_id,
        (activeJob.settings.target_output_format as string) || settings.target_output_format,
        (activeJob.settings.quality_preset as string) || settings.quality_preset
      );
      setLatestArtifact(art);
    } catch {
      setLatestArtifact(null);
    }
    setStep("download");
  };

  const steps: Array<{ id: WorkflowStep; label: string }> = [
    { id: "upload", label: t(locale, "stepUpload") },
    { id: "options", label: t(locale, "stepOptions") },
    { id: "review", label: t(locale, "stepReview") },
    { id: "download", label: t(locale, "stepDownload") },
  ];

  return (
    <div className="space-y-8">
      {/* Hero Title */}
      <div className="text-center">
        <h1 className="text-3xl font-bold tracking-tight text-slate-900 dark:text-slate-100 sm:text-4xl">
          {t(locale, "heroTitle")}
        </h1>
        <p className="mt-2 text-base text-slate-600 dark:text-slate-300">{t(locale, "heroSubtitle")}</p>
      </div>

      {backendError && (
        <div
          role="alert"
          className="mx-auto max-w-3xl rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
        >
          <strong>Backend Unavailable:</strong> {backendError}
        </div>
      )}

      {/* 4-Step Guided Progress Bar */}
      <nav aria-label="Workflow steps" className="mx-auto flex max-w-2xl items-center justify-between">
        {steps.map((s, idx) => {
          const isCurrent = step === s.id;
          const stepIndex = steps.findIndex((x) => x.id === step);
          const isDone = idx < stepIndex;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                if (s.id === "upload") setStep("upload");
                if (s.id === "options" && queue.some((q) => q.status === "completed")) setStep("options");
                if (s.id === "review" && activeJob?.status === "completed") setStep("review");
                if (s.id === "download" && activeJob?.status === "completed") setStep("download");
              }}
              className="flex items-center gap-2 text-xs sm:text-sm font-semibold focus:outline-none"
            >
              <span
                className={`flex h-7 w-7 items-center justify-center rounded-full text-xs ${
                  isCurrent
                    ? "bg-indigo-600 text-white"
                    : isDone
                    ? "bg-teal-600 text-white"
                    : "bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-400"
                }`}
              >
                {idx + 1}
              </span>
              <span className={isCurrent ? "text-indigo-700 dark:text-indigo-300" : "text-slate-600 dark:text-slate-400"}>
                {s.label}
              </span>
            </button>
          );
        })}
      </nav>

      {/* Active Processing Banner */}
      {activeJob && (activeJob.status === "queued" || activeJob.status === "running") && (
        <div className="mx-auto max-w-2xl rounded-2xl border border-indigo-200 bg-indigo-50/70 p-4 dark:border-indigo-900 dark:bg-indigo-950/40">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <Loader2 className="h-5 w-5 animate-spin text-indigo-600" />
              <div>
                <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {activeJob.stage_label} ({Math.round(activeJob.progress_percent)}%)
                </p>
                <p className="text-xs text-slate-600 dark:text-slate-300">
                  Page {activeJob.processed_pages} of {activeJob.page_count || "?"}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={async () => {
                const cancelled = await cancelJob(activeJob.job_id);
                setActiveJob(cancelled);
              }}
              className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-white px-3 py-1.5 text-xs font-medium text-rose-600 hover:bg-rose-50"
            >
              <XCircle className="h-3.5 w-3.5" /> {t(locale, "cancel")}
            </button>
          </div>
          <div className="mt-2.5 h-2 w-full overflow-hidden rounded-full bg-indigo-100 dark:bg-indigo-900">
            <div className="h-full bg-indigo-600 transition-all" style={{ width: `${activeJob.progress_percent}%` }} />
          </div>
        </div>
      )}

      {/* Password / Error Prompt Banner */}
      {activeJob && activeJob.status === "failed" && (
        <div className="mx-auto max-w-2xl rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <div className="flex items-start gap-2.5">
            <AlertCircle className="h-5 w-5 shrink-0 mt-0.5" />
            <div className="flex-1">
              <p className="font-semibold">{activeJob.error_message || "Processing failed."}</p>
              {activeJob.needs_password && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Lock className="h-4 w-4" />
                  <input
                    type="password"
                    value={promptPassword}
                    onChange={(e) => setPromptPassword(e.target.value)}
                    placeholder="Enter document password"
                    className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs text-slate-900"
                  />
                  <button
                    type="button"
                    onClick={async () => {
                      const retried = await retryJob(activeJob.job_id, promptPassword);
                      setActiveJob(retried);
                      setPromptPassword("");
                    }}
                    className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white"
                  >
                    Unlock & Retry
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Step Content */}
      {step === "upload" && (
        <UploadCard
          queue={queue}
          selectedAction={selectedAction}
          onSelectAction={setSelectedAction}
          onAddFiles={handleAddFiles}
          onPauseItem={(id) => {
            pauseFlags.current[id] = true;
          }}
          onResumeItem={(id) => {
            const item = queue.find((q) => q.localId === id);
            if (item) startChunkedUpload(item);
          }}
          onCancelItem={(id) => {
            cancelFlags.current[id] = true;
          }}
          onRetryItem={(id) => {
            const item = queue.find((q) => q.localId === id);
            if (item) startChunkedUpload(item);
          }}
          onRemoveItem={(id) => setQueue((prev) => prev.filter((q) => q.localId !== id))}
          onContinueToOptions={() => setStep("options")}
        />
      )}

      {step === "options" && (
        <OptionsStep
          completedUploads={queue.filter((q) => q.status === "completed")}
          settings={settings}
          onChangeSettings={setSettings}
          password={password}
          onChangePassword={setPassword}
          isStarting={isStarting}
          onStartProcessing={handleStartProcessing}
          onBack={() => setStep("upload")}
        />
      )}

      {step === "review" && activeJob && (
        <ReviewStep
          job={activeJob}
          pages={pages}
          onPagesUpdated={setPages}
          onContinueToDownload={handleContinueToDownload}
          onOpenStudyStudio={() => router.push(`/study?jobId=${activeJob.job_id}`)}
        />
      )}

      {step === "download" && activeJob && (
        <DownloadStep
          job={activeJob}
          batchJobIds={batchJobIds}
          initialArtifact={latestArtifact}
          onArtifactCreated={setLatestArtifact}
          onBackToReview={() => setStep("review")}
        />
      )}

      {/* Recent Files */}
      <RecentJobs
        jobs={recentJobs}
        onOpenJob={handleOpenExistingJob}
        onRetryJob={async (id) => {
          const retried = await retryJob(id);
          setActiveJob(retried);
        }}
        onDeleteJob={async (id) => {
          await deleteJobNow(id);
          refreshRecent();
        }}
      />
    </div>
  );
}
