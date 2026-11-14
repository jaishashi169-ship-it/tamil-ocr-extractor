"use client";

import React, { useState } from "react";
import { Archive, CheckCircle2, Download, FileDown, Minimize2 } from "lucide-react";
import { createBatchZip, createExport, formatBytes, getExportDownloadUrl, getOriginalDownloadUrl } from "@/lib/api";
import { t } from "@/lib/i18n";
import { ExportArtifactRecord, JobRecord } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface DownloadStepProps {
  job: JobRecord;
  batchJobIds: string[];
  initialArtifact: ExportArtifactRecord | null;
  onArtifactCreated: (art: ExportArtifactRecord) => void;
  onBackToReview: () => void;
}

export function DownloadStep({
  job,
  batchJobIds,
  initialArtifact,
  onArtifactCreated,
  onBackToReview,
}: DownloadStepProps) {
  const { locale } = useStudio();
  const [artifact, setArtifact] = useState<ExportArtifactRecord | null>(initialArtifact);
  const [selectedFormat, setSelectedFormat] = useState(
    (job.settings.target_output_format as string) || "docx"
  );
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    if (!artifact) return;
    const url = await getExportDownloadUrl(job.job_id, artifact.artifact_id);
    window.open(url, "_blank", "noopener,noreferrer");
  };

  const handleDownloadOriginal = async () => {
    const url = await getOriginalDownloadUrl(job.job_id);
    window.open(url, "_blank", "noopener,noreferrer");
  };

  const handleDownloadZip = async () => {
    const ids = batchJobIds.length > 0 ? batchJobIds : [job.job_id];
    const res = await createBatchZip(ids, selectedFormat, artifact?.quality_preset || "high");
    window.open(res.full_url, "_blank", "noopener,noreferrer");
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-full bg-teal-50 text-teal-600 dark:bg-teal-950 dark:text-teal-300">
            <CheckCircle2 className="h-6 w-6" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
              {t(locale, "downloadCompleteTitle")}
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {job.original_filename} · {job.page_count} page(s)
            </p>
          </div>
        </div>

        {/* Format switcher */}
        <div className="mt-5 flex flex-wrap items-center gap-2">
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
            disabled={isGenerating}
            onClick={() => handleGenerate("high")}
            className="rounded-xl border border-slate-200 px-3.5 py-2 text-xs font-semibold hover:bg-slate-50 dark:border-slate-700"
          >
            {isGenerating ? "Generating..." : "Generate format"}
          </button>
        </div>

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

            {artifact.warnings.length > 0 && (
              <div className="mt-3 border-t border-slate-200 pt-2 text-xs text-amber-800 dark:border-slate-700 dark:text-amber-300">
                {artifact.warnings.map((w, i) => (
                  <p key={i}>• {w}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {error && <p className="mt-3 text-xs text-rose-600">{error}</p>}

        {/* Primary & Secondary Download Buttons */}
        <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
          <button
            type="button"
            disabled={!artifact || isGenerating}
            onClick={handleDownloadConverted}
            className="inline-flex flex-1 items-center justify-center gap-2 rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
          >
            <Download className="h-4 w-4" />
            <span>{t(locale, "downloadConverted")}</span>
          </button>

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
            disabled={isGenerating}
            onClick={() => handleGenerate("smallest")}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300"
          >
            <Minimize2 className="h-3.5 w-3.5" />
            <span>{t(locale, "createSmaller")}</span>
          </button>

          <button
            type="button"
            onClick={handleDownloadZip}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300"
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
    </div>
  );
}
