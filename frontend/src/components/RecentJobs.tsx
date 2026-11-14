"use client";

import React from "react";
import { CheckCircle2, Clock, Eye, Loader2, RefreshCw, Trash2, XCircle } from "lucide-react";
import { formatBytes } from "@/lib/api";
import { t } from "@/lib/i18n";
import { JobRecord } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface RecentJobsProps {
  jobs: JobRecord[];
  onOpenJob: (job: JobRecord) => void;
  onRetryJob: (jobId: string) => void;
  onDeleteJob: (jobId: string) => void;
}

export function RecentJobs({ jobs, onOpenJob, onRetryJob, onDeleteJob }: RecentJobsProps) {
  const { locale } = useStudio();

  return (
    <section className="mt-10">
      <h2 className="mb-3 text-base font-semibold text-slate-900 dark:text-slate-100">{t(locale, "recentFiles")}</h2>
      {jobs.length === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-6 text-center text-sm text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
          {t(locale, "noRecentFiles")}
        </div>
      ) : (
        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {jobs.map((job) => (
              <li key={job.job_id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                    {job.original_filename}
                  </p>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    {formatBytes(job.file_size_bytes)} · {job.format_type.toUpperCase()} ·{" "}
                    {job.created_at ? new Date(job.created_at).toLocaleDateString("en-US") : ""}
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  {job.status === "completed" && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-teal-50 px-2.5 py-1 text-xs font-medium text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                      <CheckCircle2 className="h-3.5 w-3.5" /> Completed
                    </span>
                  )}
                  {(job.status === "queued" || job.status === "running") && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> {Math.round(job.progress_percent)}%
                    </span>
                  )}
                  {job.status === "failed" && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                      <XCircle className="h-3.5 w-3.5" /> Review / Failed
                    </span>
                  )}
                  {job.status === "cancelled" && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                      <Clock className="h-3.5 w-3.5" /> Cancelled
                    </span>
                  )}

                  {job.status === "completed" && (
                    <button
                      type="button"
                      onClick={() => onOpenJob(job)}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200"
                    >
                      <Eye className="h-3.5 w-3.5" />
                      <span>Open</span>
                    </button>
                  )}
                  {job.status === "failed" && (
                    <button
                      type="button"
                      onClick={() => onRetryJob(job.job_id)}
                      className="inline-flex items-center gap-1 rounded-lg border border-amber-300 px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-50"
                    >
                      <RefreshCw className="h-3.5 w-3.5" />
                      <span>{t(locale, "retry")}</span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => onDeleteJob(job.job_id)}
                    aria-label={t(locale, "deleteNow")}
                    className="rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
