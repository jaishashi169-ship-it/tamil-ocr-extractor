"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  AlertCircle,
  Archive,
  Check,
  CheckCircle2,
  Copy,
  Download,
  FileDown,
  FileText,
  Loader2,
  Minimize2,
  RefreshCw,
  Sparkles,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import {
  createBatchZip,
  createExport,
  formatBytes,
  getExportDownloadUrl,
  getJobPages,
  getOriginalDownloadUrl,
  retryJob,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { ExportArtifactRecord, JobRecord, PageResultRecord } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface DownloadStepProps {
  job: JobRecord;
  batchJobIds: string[];
  initialArtifact: ExportArtifactRecord | null;
  pages?: PageResultRecord[];
  onArtifactCreated: (art: ExportArtifactRecord) => void;
  onBackToReview: () => void;
}

export function DownloadStep({
  job: initialJob,
  batchJobIds,
  initialArtifact,
  pages: initialPages,
  onArtifactCreated,
  onBackToReview,
}: DownloadStepProps) {
  const { locale } = useStudio();
  const [job, setJob] = useState<JobRecord>(initialJob);
  const [artifact, setArtifact] = useState<ExportArtifactRecord | null>(initialArtifact);
  const [pages, setPages] = useState<PageResultRecord[]>(initialPages || []);
  const [selectedFormat, setSelectedFormat] = useState(
    (initialJob.settings?.target_output_format as string) || "docx"
  );
  const [isGenerating, setIsGenerating] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [showFullExtract, setShowFullExtract] = useState(false);

  // Sync initialJob prop changes
  useEffect(() => {
    setJob(initialJob);
  }, [initialJob]);

  // Load pages if not passed from parent
  useEffect(() => {
    if ((!pages || pages.length === 0) && job?.job_id) {
      getJobPages(job.job_id)
        .then((res) => {
          if (res?.pages) setPages(res.pages);
        })
        .catch(() => {});
    }
  }, [job?.job_id, pages]);

  // Aggregate extracted text & stats
  const { fullText, totalWords, avgConfidence, languageList } = useMemo(() => {
    if (!pages || pages.length === 0) {
      return { fullText: "", totalWords: 0, avgConfidence: 0, languageList: [] };
    }
    const texts = pages.map((p) => p.edited_text || p.raw_text || "");
    const combined = texts.join("\n\n");
    const words = combined.trim().split(/\s+/).filter(Boolean).length;

    const confidences = pages
      .map((p) => p.confidence_avg)
      .filter((c): c is number => typeof c === "number" && c > 0);
    const avg =
      confidences.length > 0
        ? Math.round((confidences.reduce((a, b) => a + b, 0) / confidences.length) * 10) / 10
        : 0;

    const langs = Array.from(new Set(pages.map((p) => p.language_detected).filter(Boolean)));

    return {
      fullText: combined,
      totalWords: words,
      avgConfidence: avg,
      languageList: langs,
    };
  }, [pages]);

  const handleGenerate = async (preset: string) => {
    setIsGenerating(true);
    setError(null);
    try {
      const art = await createExport(job.job_id, selectedFormat, preset);
      setArtifact(art);
      onArtifactCreated(art);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setIsGenerating(false);
    }
  };

  const handleDownloadConverted = async () => {
    setIsGenerating(true);
    setError(null);
    try {
      let targetArt = artifact;
      // If artifact is not ready or format changed, generate on-the-fly
      if (!targetArt || targetArt.export_format !== selectedFormat) {
        targetArt = await createExport(job.job_id, selectedFormat, "high");
        setArtifact(targetArt);
        onArtifactCreated(targetArt);
      }
      const url = await getExportDownloadUrl(job.job_id, targetArt.artifact_id);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed. Please try again.");
    } finally {
      setIsGenerating(false);
    }
  };

  const handleDownloadOriginal = async () => {
    try {
      const url = await getOriginalDownloadUrl(job.job_id);
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to obtain original file URL");
    }
  };

  const handleDownloadZip = async () => {
    try {
      const ids = batchJobIds.length > 0 ? batchJobIds : [job.job_id];
      const res = await createBatchZip(ids, selectedFormat, artifact?.quality_preset || "high");
      window.open(res.full_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate batch ZIP");
    }
  };

  const handleDownloadQuickText = () => {
    if (!fullText) return;
    const blob = new Blob([fullText], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${job.original_filename.replace(/\.[^/.]+$/, "")}_extracted_tamil.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleCopyText = async () => {
    if (!fullText) return;
    try {
      await navigator.clipboard.writeText(fullText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback
    }
  };

  const handleRetry = async () => {
    setIsRetrying(true);
    setError(null);
    try {
      const updated = await retryJob(job.job_id);
      setJob(updated);
      onBackToReview();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Retry failed");
    } finally {
      setIsRetrying(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      {/* Primary Card */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-3">
          <div
            className={`flex h-11 w-11 items-center justify-center rounded-full ${
              job.status === "failed"
                ? "bg-amber-50 text-amber-600 dark:bg-amber-950 dark:text-amber-300"
                : "bg-teal-50 text-teal-600 dark:bg-teal-950 dark:text-teal-300"
            }`}
          >
            {job.status === "failed" ? (
              <AlertCircle className="h-6 w-6" />
            ) : (
              <CheckCircle2 className="h-6 w-6" />
            )}
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
              {job.status === "failed"
                ? "Job Interrupted — Ready to Retry"
                : t(locale, "downloadCompleteTitle")}
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {job.original_filename} · {job.page_count} page(s)
            </p>
          </div>
        </div>

        {/* Failed Job Banner & Retry Action */}
        {job.status === "failed" && (
          <div className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-4 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/60 dark:text-amber-200">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-amber-600 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="font-semibold text-sm">Processing stopped before all pages finished</p>
                <p className="mt-1 text-amber-800 dark:text-amber-300">
                  {job.error_message || "The previous attempt stopped during preview generation."}
                </p>
                <p className="mt-1.5 text-slate-700 dark:text-slate-300">
                  The system storage budget has been extended to <strong>50 GB</strong>. Click the button below to re-process all {job.page_count} pages completely:
                </p>
                <button
                  type="button"
                  onClick={handleRetry}
                  disabled={isRetrying}
                  className="mt-3 inline-flex items-center gap-2 rounded-xl bg-amber-600 px-4 py-2.5 font-semibold text-white shadow-sm hover:bg-amber-700 disabled:opacity-50"
                >
                  {isRetrying ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                  <span>Re-process Document (50 GB Capacity)</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Format switcher */}
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <label className="text-xs font-medium text-slate-600 dark:text-slate-400">
            Format:
          </label>
          <select
            value={selectedFormat}
            onChange={(e) => setSelectedFormat(e.target.value)}
            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
          >
            <option value="docx">Word (.docx)</option>
            <option value="txt">Plain Text (.txt)</option>
            <option value="md">Markdown (.md)</option>
            <option value="searchable_pdf">Searchable PDF (.pdf)</option>
            <option value="pdf">Reflowed PDF (.pdf)</option>
            <option value="html">HTML (.html)</option>
            <option value="json">JSON (.json)</option>
            <option value="xlsx">Excel (.xlsx)</option>
            <option value="csv">CSV (.csv)</option>
            <option value="png">PNG (.png)</option>
            <option value="jpg">JPEG (.jpg)</option>
            <option value="webp">WEBP (.webp)</option>
          </select>

          <button
            type="button"
            disabled={isGenerating || job.status !== "completed"}
            onClick={() => handleGenerate("high")}
            className="inline-flex items-center gap-1.5 rounded-xl border border-indigo-200 bg-indigo-50 px-3.5 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 disabled:opacity-50 dark:border-indigo-800 dark:bg-indigo-950/60 dark:text-indigo-300"
          >
            {isGenerating ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>Processing...</span>
              </>
            ) : (
              <>
                <Sparkles className="h-3.5 w-3.5" />
                <span>Prepare {selectedFormat.toUpperCase()}</span>
              </>
            )}
          </button>
        </div>

        {/* Artifact Details if ready */}
        {artifact && (
          <div className="mt-5 rounded-xl bg-slate-50 p-4 dark:bg-slate-800/60">
            <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
              <div>
                <p className="text-xs text-slate-500">Format & Quality</p>
                <p className="font-semibold uppercase">
                  {artifact.export_format} ({artifact.quality_preset})
                </p>
              </div>
              <div>
                <p className="text-xs text-slate-500">{t(locale, "actualSize")}</p>
                <p className="font-semibold">{formatBytes(artifact.file_size_bytes)}</p>
              </div>
              <div>
                <p className="text-xs text-slate-500">{t(locale, "sizeSavings")}</p>
                <p className="font-semibold">
                  {artifact.savings_percent !== null && artifact.savings_percent !== undefined
                    ? `${artifact.savings_percent}%`
                    : "0%"}
                </p>
              </div>
            </div>

            {artifact.warnings && artifact.warnings.length > 0 && (
              <div className="mt-3 border-t border-slate-200 pt-2 text-xs text-amber-800 dark:border-slate-700 dark:text-amber-300">
                {artifact.warnings.map((w, i) => (
                  <p key={i}>• {w}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Generating Indicator */}
        {isGenerating && (
          <div className="mt-4 flex items-center gap-3 rounded-xl border border-indigo-100 bg-indigo-50/50 p-4 text-xs text-indigo-800 dark:border-indigo-900/50 dark:bg-indigo-950/30 dark:text-indigo-300">
            <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />
            <div>
              <p className="font-semibold">Preparing your document export...</p>
              <p className="text-indigo-600/80 dark:text-indigo-400/80">
                Formatting extracted Tamil & English Unicode text. Please wait a moment.
              </p>
            </div>
          </div>
        )}

        {error && (
          <div className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
            {error}
          </div>
        )}

        {/* Primary & Secondary Download Buttons */}
        <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
          {/* Main Converted Download Button: never disabled when job completed */}
          <button
            type="button"
            disabled={isGenerating || (job.status !== "completed" && !fullText)}
            onClick={handleDownloadConverted}
            className="inline-flex flex-1 items-center justify-center gap-2 rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
          >
            {isGenerating ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>Generating & Downloading...</span>
              </>
            ) : (
              <>
                <Download className="h-4 w-4" />
                <span>
                  {artifact
                    ? t(locale, "downloadConverted")
                    : `Download ${selectedFormat.toUpperCase()} File`}
                </span>
              </>
            )}
          </button>

          {/* Instant Text File Download (Direct from Memory) */}
          {fullText && (
            <button
              type="button"
              onClick={handleDownloadQuickText}
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm font-semibold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200"
            >
              <FileText className="h-4 w-4 text-emerald-600" />
              <span>Download Text (.txt)</span>
            </button>
          )}

          {/* Original File Download */}
          <button
            type="button"
            onClick={handleDownloadOriginal}
            className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-3 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          >
            <FileDown className="h-4 w-4" />
            <span>{t(locale, "downloadOriginal")}</span>
          </button>
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 pt-2">
          <button
            type="button"
            disabled={isGenerating || job.status !== "completed"}
            onClick={() => handleGenerate("smallest")}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
          >
            <Minimize2 className="h-3.5 w-3.5" />
            <span>{t(locale, "createSmaller")}</span>
          </button>

          <button
            type="button"
            disabled={job.status !== "completed"}
            onClick={handleDownloadZip}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
          >
            <Archive className="h-3.5 w-3.5" />
            <span>{t(locale, "downloadZip")}</span>
          </button>

          <button
            type="button"
            onClick={onBackToReview}
            className="text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400"
          >
            ← Back to Review
          </button>
        </div>
      </div>

      {/* Extracted Document Highlights & Content Extract Card */}
      {fullText && (
        <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <FileText className="h-5 w-5 text-indigo-600 dark:text-indigo-400" />
              <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                Extracted Document Highlights
              </h3>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleDownloadQuickText}
                className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
              >
                <FileText className="h-3.5 w-3.5" />
                <span>Save .txt</span>
              </button>
              <button
                type="button"
                onClick={handleCopyText}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              >
                {copied ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-emerald-600" />
                    <span className="text-emerald-600 font-semibold">Copied!</span>
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    <span>Copy All</span>
                  </>
                )}
              </button>
            </div>
          </div>

          {/* Quick Stats Badges */}
          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-800/40">
              <p className="text-[11px] font-medium text-slate-500">Pages Processed</p>
              <p className="text-base font-bold text-slate-800 dark:text-slate-200">
                {pages.length || job.page_count}
              </p>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-800/40">
              <p className="text-[11px] font-medium text-slate-500">Total Words</p>
              <p className="text-base font-bold text-slate-800 dark:text-slate-200">
                {totalWords.toLocaleString()}
              </p>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-800/40">
              <p className="text-[11px] font-medium text-slate-500">OCR Confidence</p>
              <p className="text-base font-bold text-slate-800 dark:text-slate-200">
                {avgConfidence > 0 ? `${avgConfidence}%` : "Clean Digital"}
              </p>
            </div>
            <div className="rounded-xl border border-slate-100 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-800/40">
              <p className="text-[11px] font-medium text-slate-500">Script Detected</p>
              <p className="text-base font-bold text-slate-800 dark:text-slate-200 uppercase">
                {languageList.join(", ") || "Tamil / English"}
              </p>
            </div>
          </div>

          {/* Text Excerpt & Highlights Preview */}
          <div className="mt-4">
            <div className="flex items-center justify-between pb-1.5">
              <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">
                {showFullExtract ? "Full Extracted Text" : "Document Excerpt (First ~1,200 characters)"}
              </span>
              <button
                type="button"
                onClick={() => setShowFullExtract(!showFullExtract)}
                className="inline-flex items-center gap-1 text-xs text-indigo-600 hover:underline dark:text-indigo-400"
              >
                <span>{showFullExtract ? "Show Less" : "View Full Text"}</span>
                {showFullExtract ? (
                  <ChevronUp className="h-3 w-3" />
                ) : (
                  <ChevronDown className="h-3 w-3" />
                )}
              </button>
            </div>

            <div
              tabIndex={0}
              aria-label="Extracted document text preview"
              className="max-h-72 overflow-y-auto rounded-xl border border-slate-200 bg-slate-50/70 p-4 font-mono text-xs leading-relaxed text-slate-800 select-text focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-200"
            >
              <pre className="whitespace-pre-wrap font-sans text-xs">
                {showFullExtract
                  ? fullText
                  : fullText.slice(0, 1200) + (fullText.length > 1200 ? "\n\n... [Click 'View Full Text' to see all]" : "")}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
