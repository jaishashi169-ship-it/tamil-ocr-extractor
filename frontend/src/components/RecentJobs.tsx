"use client";

import React, { useState, useMemo } from "react";
import {
  Archive,
  CheckCircle2,
  CheckSquare,
  Clock,
  Download,
  Eye,
  FileCode,
  FileText,
  Loader2,
  RefreshCw,
  Square,
  Trash2,
  X,
  XCircle,
  ChevronDown,
} from "lucide-react";
import {
  createBatchZip,
  createExport,
  formatBytes,
  getExportDownloadUrl,
  getOriginalDownloadUrl,
} from "@/lib/api";
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
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isExporting, setIsExporting] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [activeDropdown, setActiveDropdown] = useState<string | null>(null);
  const [showBulkMenu, setShowBulkMenu] = useState(false);
  const [downloadingJobId, setDownloadingJobId] = useState<string | null>(null);

  const completedJobs = useMemo(() => jobs.filter((j) => j.status === "completed"), [jobs]);

  const allSelected = jobs.length > 0 && selectedIds.size === jobs.length;
  const someSelected = selectedIds.size > 0 && selectedIds.size < jobs.length;

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(jobs.map((j) => j.job_id)));
    }
  };

  const toggleSelectJob = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const next = new Set(selectedIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setSelectedIds(next);
  };

  const triggerBrowserDownload = (url: string, filename: string) => {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  // Quick single download
  const handleSingleDownload = async (job: JobRecord, format: string) => {
    setDownloadingJobId(`${job.job_id}-${format}`);
    try {
      if (format === "original") {
        const link = await getOriginalDownloadUrl(job.job_id);
        triggerBrowserDownload(link, job.original_filename);
        return;
      }
      const art = await createExport(job.job_id, format, "high");
      const link = await getExportDownloadUrl(job.job_id, art.artifact_id);
      const cleanStem = job.original_filename.replace(/\.[^/.]+$/, "");
      triggerBrowserDownload(link, `${cleanStem}.${format}`);
    } catch (err: any) {
      alert("Download failed: " + (err.message || String(err)));
    } finally {
      setDownloadingJobId(null);
      setActiveDropdown(null);
    }
  };

  // Bulk ZIP download
  const handleBulkBatchZip = async (format: string) => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setIsExporting(true);
    setShowBulkMenu(false);
    try {
      const res = await createBatchZip(ids, format, "high");
      const url = res.full_url || res.download_url;
      const dateStr = new Date().toISOString().slice(0, 10);
      triggerBrowserDownload(url, `tamil_studio_bulk_${format}_${dateStr}.zip`);
    } catch (err: any) {
      alert("Bulk batch export failed: " + (err.message || String(err)));
    } finally {
      setIsExporting(false);
    }
  };

  // Bulk individual files download
  const handleBulkIndividualDownload = async (format: string) => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setIsExporting(true);
    setShowBulkMenu(false);
    try {
      for (const id of ids) {
        const job = jobs.find((j) => j.job_id === id);
        if (!job || job.status !== "completed") continue;
        const art = await createExport(job.job_id, format, "high");
        const link = await getExportDownloadUrl(job.job_id, art.artifact_id);
        const cleanStem = job.original_filename.replace(/\.[^/.]+$/, "");
        triggerBrowserDownload(link, `${cleanStem}.${format}`);
        await new Promise((r) => setTimeout(r, 400));
      }
    } catch (err: any) {
      alert("Bulk download failed: " + (err.message || String(err)));
    } finally {
      setIsExporting(false);
    }
  };

  // Bulk delete
  const handleBulkDelete = async () => {
    const count = selectedIds.size;
    if (count === 0) return;
    if (!window.confirm(`Delete ${count} selected document(s)? This will remove extracted data and originals.`)) {
      return;
    }
    setIsDeleting(true);
    try {
      for (const id of Array.from(selectedIds)) {
        try {
          await onDeleteJob(id);
        } catch (err) {
          console.warn("Error deleting job", id, err);
        }
      }
      setSelectedIds(new Set());
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <section className="mt-10">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">
            {t(locale, "recentFiles")}
          </h2>
          {jobs.length > 0 && (
            <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {jobs.length} total
            </span>
          )}
        </div>

        {/* Bulk Action Controls */}
        {selectedIds.size > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-xl bg-teal-50 px-3 py-1.5 border border-teal-200 dark:bg-teal-950/50 dark:border-teal-800 animate-in fade-in">
            <span className="text-xs font-semibold text-teal-800 dark:text-teal-200">
              {selectedIds.size} file{selectedIds.size > 1 ? "s" : ""} selected
            </span>

            <div className="relative">
              <button
                type="button"
                onClick={() => setShowBulkMenu(!showBulkMenu)}
                disabled={isExporting}
                className="inline-flex items-center gap-1.5 rounded-lg bg-teal-600 px-3 py-1 text-xs font-medium text-white shadow-sm hover:bg-teal-700 disabled:opacity-50"
              >
                {isExporting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Download className="h-3.5 w-3.5" />
                )}
                <span>Bulk Export</span>
                <ChevronDown className="h-3 w-3" />
              </button>

              {showBulkMenu && (
                <div
                  className="absolute right-0 top-full z-30 mt-1 w-56 rounded-xl border border-slate-200 bg-white py-1.5 shadow-lg dark:border-slate-700 dark:bg-slate-900"
                  onClick={() => setShowBulkMenu(false)}
                >
                  <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    Download as Archive (ZIP)
                  </div>
                  <button
                    type="button"
                    onClick={() => handleBulkBatchZip("docx")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <Archive className="h-3.5 w-3.5 text-teal-600" />
                    <span>Batch ZIP (Word .docx)</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleBulkBatchZip("txt")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <Archive className="h-3.5 w-3.5 text-teal-600" />
                    <span>Batch ZIP (Plain Text .txt)</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleBulkBatchZip("html")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <Archive className="h-3.5 w-3.5 text-teal-600" />
                    <span>Batch ZIP (HTML .html)</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleBulkBatchZip("json")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <Archive className="h-3.5 w-3.5 text-teal-600" />
                    <span>Batch ZIP (JSON Data)</span>
                  </button>

                  <div className="my-1 border-t border-slate-100 dark:border-slate-800" />
                  <div className="px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                    Individual Downloads
                  </div>
                  <button
                    type="button"
                    onClick={() => handleBulkIndividualDownload("docx")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <FileText className="h-3.5 w-3.5 text-blue-600" />
                    <span>Download all as .docx files</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleBulkIndividualDownload("txt")}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    <FileCode className="h-3.5 w-3.5 text-amber-600" />
                    <span>Download all as .txt files</span>
                  </button>
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={handleBulkDelete}
              disabled={isDeleting}
              className="inline-flex items-center gap-1 rounded-lg border border-rose-200 bg-white px-2.5 py-1 text-xs font-medium text-rose-600 hover:bg-rose-50 dark:border-rose-900 dark:bg-slate-900 dark:text-rose-400 dark:hover:bg-rose-950/50"
            >
              {isDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              <span>Delete Selected</span>
            </button>

            <button
              type="button"
              onClick={() => setSelectedIds(new Set())}
              aria-label="Clear selection"
              className="rounded-lg p-1 text-slate-400 hover:bg-slate-200/50 hover:text-slate-600 dark:hover:bg-slate-800"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>

      {jobs.length === 0 ? (
        <div className="rounded-2xl border border-slate-200 bg-white p-6 text-center text-sm text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
          {t(locale, "noRecentFiles")}
        </div>
      ) : (
        <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
          {/* Table Header with Select All */}
          <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/70 px-4 py-2.5 text-xs font-medium text-slate-500 dark:border-slate-800 dark:bg-slate-900/70 dark:text-slate-400 sm:px-5">
            <button
              type="button"
              onClick={toggleSelectAll}
              className="inline-flex items-center gap-2 hover:text-slate-800 dark:hover:text-slate-200"
            >
              {allSelected ? (
                <CheckSquare className="h-4 w-4 text-teal-600" />
              ) : someSelected ? (
                <Square className="h-4 w-4 text-teal-500 fill-teal-100 dark:fill-teal-950" />
              ) : (
                <Square className="h-4 w-4 text-slate-400" />
              )}
              <span>Select All ({jobs.length})</span>
            </button>
            <span>Actions & Status</span>
          </div>

          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {jobs.map((job) => {
              const isSelected = selectedIds.has(job.job_id);
              const isDropdownOpen = activeDropdown === job.job_id;

              return (
                <li
                  key={job.job_id}
                  className={`flex flex-wrap items-center justify-between gap-3 px-4 py-3.5 transition-colors sm:px-5 ${
                    isSelected
                      ? "bg-teal-50/40 dark:bg-teal-950/20 border-l-2 border-teal-500"
                      : "hover:bg-slate-50/60 dark:hover:bg-slate-800/40"
                  }`}
                >
                  <div className="flex min-w-0 flex-1 items-center gap-3">
                    <button
                      type="button"
                      onClick={(e) => toggleSelectJob(job.job_id, e)}
                      aria-label={isSelected ? "Deselect" : "Select"}
                      className="text-slate-400 hover:text-teal-600 focus:outline-none"
                    >
                      {isSelected ? (
                        <CheckSquare className="h-4 w-4 text-teal-600" />
                      ) : (
                        <Square className="h-4 w-4 text-slate-300 dark:text-slate-600" />
                      )}
                    </button>

                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                        {job.original_filename}
                      </p>
                      <p className="text-xs text-slate-500 dark:text-slate-400">
                        {formatBytes(job.file_size_bytes)} · {job.format_type.toUpperCase()} ·{" "}
                        {job.page_count > 0 ? `${job.page_count} pages · ` : ""}
                        {job.created_at ? new Date(job.created_at).toLocaleDateString("en-US") : ""}
                      </p>
                    </div>
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

                    {/* Quick Single Download Dropdown */}
                    {job.status === "completed" && (
                      <div className="relative">
                        <button
                          type="button"
                          onClick={() => setActiveDropdown(isDropdownOpen ? null : job.job_id)}
                          className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
                        >
                          <Download className="h-3.5 w-3.5 text-teal-600" />
                          <span>Export</span>
                          <ChevronDown className="h-3 w-3" />
                        </button>

                        {isDropdownOpen && (
                          <div
                            className="absolute right-0 top-full z-20 mt-1 w-44 rounded-xl border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-900"
                            onClick={() => setActiveDropdown(null)}
                          >
                            <button
                              type="button"
                              onClick={() => handleSingleDownload(job, "docx")}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                            >
                              <FileText className="h-3.5 w-3.5 text-blue-600" />
                              <span>Word (.docx)</span>
                            </button>
                            <button
                              type="button"
                              onClick={() => handleSingleDownload(job, "txt")}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                            >
                              <FileCode className="h-3.5 w-3.5 text-amber-600" />
                              <span>Plain Text (.txt)</span>
                            </button>
                            <button
                              type="button"
                              onClick={() => handleSingleDownload(job, "html")}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                            >
                              <FileCode className="h-3.5 w-3.5 text-rose-600" />
                              <span>HTML (.html)</span>
                            </button>
                            <button
                              type="button"
                              onClick={() => handleSingleDownload(job, "json")}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-700 hover:bg-teal-50 dark:text-slate-200 dark:hover:bg-slate-800"
                            >
                              <FileCode className="h-3.5 w-3.5 text-emerald-600" />
                              <span>JSON Data (.json)</span>
                            </button>
                            <div className="my-1 border-t border-slate-100 dark:border-slate-800" />
                            <button
                              type="button"
                              onClick={() => handleSingleDownload(job, "original")}
                              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800"
                            >
                              <Download className="h-3.5 w-3.5 text-slate-400" />
                              <span>Original PDF</span>
                            </button>
                          </div>
                        )}
                      </div>
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
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
