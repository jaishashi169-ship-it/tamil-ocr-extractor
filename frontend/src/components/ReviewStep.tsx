"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  Layers,
  Redo2,
  RefreshCw,
  Search,
  Sparkles,
  Undo2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  applyHeadersFooters,
  applyLegacyTamil,
  fetchPageImageBlobUrl,
  previewHeadersFooters,
  previewLegacyTamil,
  reprocessPage,
  reviewPageGrammar,
  savePageReview,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { JobRecord, PageResultRecord } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface ReviewStepProps {
  job: JobRecord;
  pages: PageResultRecord[];
  onPagesUpdated: (pages: PageResultRecord[]) => void;
  onContinueToDownload: () => void;
  onOpenStudyStudio: () => void;
}

export function ReviewStep({
  job,
  pages,
  onPagesUpdated,
  onContinueToDownload,
  onOpenStudyStudio,
}: ReviewStepProps) {
  const { locale } = useStudio();
  const [currentIdx, setCurrentIdx] = useState(0);
  const [mobileTab, setMobileTab] = useState<"original" | "extracted">("extracted");
  const [zoom, setZoom] = useState(1);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "failed">("saved");
  const [actionError, setActionError] = useState<string | null>(null);
  const [highlightUncertain, setHighlightUncertain] = useState(true);
  const [showFindReplace, setShowFindReplace] = useState(false);
  const [findTerm, setFindTerm] = useState("");
  const [replaceTerm, setReplaceTerm] = useState("");
  const [isReprocessing, setIsReprocessing] = useState(false);
  const [grammarResult, setGrammarResult] = useState<{
    original_text: string;
    corrected_text: string;
    has_changes: boolean;
    notes: Array<{ type: string; description: string }>;
    diff: string[];
  } | null>(null);

  // Undo/Redo history per page
  const [historyMap, setHistoryMap] = useState<Record<number, { stack: string[]; ptr: number }>>({});
  const autosaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Legacy Tamil & Header/Footer preview
  const [legacyEncoding, setLegacyEncoding] = useState<"bamini" | "tab">("bamini");
  const [legacyPreview, setLegacyPreview] = useState<string | null>(null);
  const [hfCandidates, setHfCandidates] = useState<{ headers: string[]; footers: string[] } | null>(null);

  const currentPage = pages[currentIdx];

  useEffect(() => {
    let active = true;
    setPreviewUrl(null);
    if (currentPage?.has_preview) {
      fetchPageImageBlobUrl(job.job_id, currentPage.page_number, "preview")
        .then((url) => {
          if (active) setPreviewUrl(url);
        })
        .catch(() => {});
    }
    return () => {
      active = false;
    };
  }, [job.job_id, currentPage?.page_number, currentPage?.has_preview]);

  if (!currentPage) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center dark:border-slate-800 dark:bg-slate-900">
        <p className="text-sm text-slate-600 dark:text-slate-300">No extracted pages available to review.</p>
      </div>
    );
  }

  const updatePageText = (newText: string, pushHistory = true) => {
    const pNum = currentPage.page_number;
    const updated = pages.map((p, idx) => (idx === currentIdx ? { ...p, edited_text: newText, has_user_edits: true } : p));
    onPagesUpdated(updated);

    if (pushHistory) {
      setHistoryMap((prev) => {
        const cur = prev[pNum] || { stack: [currentPage.edited_text], ptr: 0 };
        const sliced = cur.stack.slice(0, cur.ptr + 1);
        return { ...prev, [pNum]: { stack: [...sliced, newText], ptr: sliced.length } };
      });
    }

    setSaveState("saving");
    setActionError(null);
    const expectedVersion = currentPage.draft_version;
    if (autosaveTimer.current) clearTimeout(autosaveTimer.current);
    autosaveTimer.current = setTimeout(async () => {
      try {
        const res = await savePageReview(job.job_id, pNum, newText, true, expectedVersion);
        onPagesUpdated(
          updated.map((p) => (p.page_number === pNum ? { ...p, draft_version: res.draft_version } : p))
        );
        setSaveState("saved");
      } catch (err) {
        setSaveState("failed");
        setActionError(err instanceof Error ? err.message : "Autosave failed");
      }
    }, 600);
  };

  const handleUndo = () => {
    const cur = historyMap[currentPage.page_number];
    if (!cur || cur.ptr <= 0) return;
    const nextPtr = cur.ptr - 1;
    setHistoryMap((prev) => ({ ...prev, [currentPage.page_number]: { ...cur, ptr: nextPtr } }));
    updatePageText(cur.stack[nextPtr], false);
  };

  const handleRedo = () => {
    const cur = historyMap[currentPage.page_number];
    if (!cur || cur.ptr >= cur.stack.length - 1) return;
    const nextPtr = cur.ptr + 1;
    setHistoryMap((prev) => ({ ...prev, [currentPage.page_number]: { ...cur, ptr: nextPtr } }));
    updatePageText(cur.stack[nextPtr], false);
  };

  const handleReplaceAll = () => {
    if (!findTerm) return;
    const next = currentPage.edited_text.split(findTerm).join(replaceTerm);
    updatePageText(next, true);
  };

  const handleReprocess = async () => {
    setIsReprocessing(true);
    setActionError(null);
    try {
      const res = await reprocessPage(job.job_id, currentPage.page_number, {
        language: (job.settings.language as string) || "tam+eng",
      });
      const updated = pages.map((p, idx) =>
        idx === currentIdx
          ? {
              ...p,
              raw_text: res.text,
              edited_text: p.has_user_edits ? p.edited_text : res.text,
              confidence_avg: res.confidence_avg ?? p.confidence_avg,
            }
          : p
      );
      onPagesUpdated(updated);
      setSaveState("saved");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "OCR reprocessing failed");
    } finally {
      setIsReprocessing(false);
    }
  };

  const handleGrammarReview = async () => {
    setActionError(null);
    try {
      const res = await reviewPageGrammar(job.job_id, currentPage.page_number);
      setGrammarResult(res);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Grammar review failed");
    }
  };

  const lowConfidenceBlocks = (currentPage.model?.blocks || []).filter(
    (b) => b.confidence !== null && b.confidence !== undefined && b.confidence < 75
  );
  const psdLayers = (currentPage.model?.metadata?.psd_layers as Array<{ name: string; kind: string; has_editable_text?: boolean }>) || [];
  const pageWarnings = currentPage.model?.warnings || [];

  return (
    <div className="space-y-4 pb-20 sm:pb-0">
      {/* Compact Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2.5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={currentIdx === 0}
            onClick={() => setCurrentIdx((i) => Math.max(0, i - 1))}
            className="rounded-lg border border-slate-200 p-1.5 disabled:opacity-40 dark:border-slate-700"
            aria-label="Previous page"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>
          <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
            Page {currentPage.page_number} / {pages.length}
          </span>
          <button
            type="button"
            disabled={currentIdx >= pages.length - 1}
            onClick={() => setCurrentIdx((i) => Math.min(pages.length - 1, i + 1))}
            className="rounded-lg border border-slate-200 p-1.5 disabled:opacity-40 dark:border-slate-700"
            aria-label="Next page"
          >
            <ChevronRight className="h-4 w-4" />
          </button>

          <div className="hidden sm:flex items-center gap-1 border-l border-slate-200 pl-2 dark:border-slate-700">
            <button
              type="button"
              onClick={() => setZoom((z) => Math.max(0.5, z - 0.15))}
              className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300"
              aria-label="Zoom out"
            >
              <ZoomOut className="h-4 w-4" />
            </button>
            <span className="text-xs text-slate-500 w-10 text-center">{Math.round(zoom * 100)}%</span>
            <button
              type="button"
              onClick={() => setZoom((z) => Math.min(2.2, z + 0.15))}
              className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100 dark:text-slate-300"
              aria-label="Zoom in"
            >
              <ZoomIn className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={handleUndo}
            className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
            title={t(locale, "undo")}
          >
            <Undo2 className="inline h-3.5 w-3.5 mr-1" />
            <span className="hidden md:inline">{t(locale, "undo")}</span>
          </button>
          <button
            type="button"
            onClick={handleRedo}
            className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
            title={t(locale, "redo")}
          >
            <Redo2 className="inline h-3.5 w-3.5 mr-1" />
            <span className="hidden md:inline">{t(locale, "redo")}</span>
          </button>
          <button
            type="button"
            onClick={() => setShowFindReplace((v) => !v)}
            className={`rounded-lg border px-2.5 py-1.5 text-xs font-medium ${
              showFindReplace ? "border-indigo-500 bg-indigo-50 text-indigo-700" : "border-slate-200 dark:border-slate-700"
            }`}
          >
            <Search className="inline h-3.5 w-3.5 mr-1" />
            <span className="hidden md:inline">{t(locale, "findReplace")}</span>
          </button>
          <button
            type="button"
            onClick={() => setHighlightUncertain((v) => !v)}
            className={`rounded-lg border px-2.5 py-1.5 text-xs font-medium ${
              highlightUncertain ? "border-amber-400 bg-amber-50 text-amber-800" : "border-slate-200 dark:border-slate-700"
            }`}
          >
            <AlertTriangle className="inline h-3.5 w-3.5 mr-1" />
            <span className="hidden lg:inline">{t(locale, "highlightUncertain")}</span>
          </button>
          {currentPage.has_preview && (
            <button
              type="button"
              disabled={isReprocessing}
              onClick={handleReprocess}
              className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
            >
              <RefreshCw className={`inline h-3.5 w-3.5 mr-1 ${isReprocessing ? "animate-spin" : ""}`} />
              <span className="hidden lg:inline">{t(locale, "reprocessPage")}</span>
            </button>
          )}
          <button
            type="button"
            onClick={handleGrammarReview}
            className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
          >
            <Sparkles className="inline h-3.5 w-3.5 mr-1 text-indigo-600" />
            <span className="hidden lg:inline">Grammar Review</span>
          </button>
          <span
            className={`ml-1 inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
              saveState === "failed"
                ? "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300"
                : "bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300"
            }`}
          >
            <Check className="h-3 w-3" />
            {saveState === "saving" ? t(locale, "saving") : saveState === "failed" ? "Save failed" : t(locale, "autosaved")}
          </span>
        </div>
      </div>

      {actionError && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
          {actionError}
        </div>
      )}

      {grammarResult && (
        <div className="rounded-xl border border-indigo-200 bg-indigo-50/70 p-4 text-xs dark:border-indigo-900 dark:bg-indigo-950/30 space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-indigo-900 dark:text-indigo-200">
              Grammar & Spelling Suggestions (Protected: quotes, formulas, numbers)
            </span>
            <button
              type="button"
              onClick={() => setGrammarResult(null)}
              className="text-slate-500 hover:text-slate-700"
            >
              Close
            </button>
          </div>
          {!grammarResult.has_changes ? (
            <p className="text-slate-600 dark:text-slate-300">No spacing or spelling issues detected on this page.</p>
          ) : (
            <>
              <ul className="list-disc pl-4 space-y-0.5 text-slate-700 dark:text-slate-300">
                {grammarResult.notes.map((n, i) => (
                  <li key={i}>{n.description}</li>
                ))}
              </ul>
              <pre className="max-h-36 overflow-auto rounded-lg bg-white p-2 font-mono text-[11px] dark:bg-slate-900">
                {grammarResult.diff.join("\n") || grammarResult.corrected_text}
              </pre>
              <button
                type="button"
                onClick={() => {
                  updatePageText(grammarResult.corrected_text, true);
                  setGrammarResult(null);
                }}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 font-semibold text-white hover:bg-indigo-700"
              >
                Apply Suggestions (Reversible)
              </button>
            </>
          )}
        </div>
      )}

      {/* Find & Replace Bar */}
      {showFindReplace && (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <input
            type="text"
            value={findTerm}
            onChange={(e) => setFindTerm(e.target.value)}
            placeholder="Find text..."
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
          />
          <input
            type="text"
            value={replaceTerm}
            onChange={(e) => setReplaceTerm(e.target.value)}
            placeholder="Replace with..."
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
          />
          <button
            type="button"
            onClick={handleReplaceAll}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700"
          >
            Replace all on page
          </button>
        </div>
      )}

      {/* Warnings / Notices */}
      {pageWarnings.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
          {pageWarnings.map((w, i) => (
            <p key={i}>• {w}</p>
          ))}
        </div>
      )}

      {/* Mobile Tabs */}
      <div className="flex rounded-xl bg-slate-200/70 p-1 sm:hidden dark:bg-slate-800">
        <button
          type="button"
          onClick={() => setMobileTab("original")}
          className={`flex-1 rounded-lg py-2 text-xs font-semibold ${
            mobileTab === "original" ? "bg-white text-slate-900 shadow-sm dark:bg-slate-900 dark:text-white" : "text-slate-600"
          }`}
        >
          {t(locale, "originalTab")}
        </button>
        <button
          type="button"
          onClick={() => setMobileTab("extracted")}
          className={`flex-1 rounded-lg py-2 text-xs font-semibold ${
            mobileTab === "extracted" ? "bg-white text-slate-900 shadow-sm dark:bg-slate-900 dark:text-white" : "text-slate-600"
          }`}
        >
          {t(locale, "extractedTab")}
        </button>
      </div>

      {/* Desktop Split Pane / Mobile Tabbed Content */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Left: Original Document Preview */}
        <div
          className={`${
            mobileTab === "original" ? "block" : "hidden"
          } lg:block rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900`}
        >
          <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
            <span>
              Source: {job.original_filename} · Page {currentPage.page_number} ({currentPage.extraction_method})
            </span>
            {currentPage.confidence_avg !== null && currentPage.confidence_avg !== undefined && (
              <span className="font-medium">OCR Confidence: {currentPage.confidence_avg.toFixed(1)}%</span>
            )}
          </div>

          <div className="h-[460px] overflow-auto rounded-xl border border-slate-100 bg-slate-50 p-3 flex items-start justify-center dark:border-slate-800 dark:bg-slate-950">
            {previewUrl ? (
              <img
                src={previewUrl}
                alt={`Original page ${currentPage.page_number}`}
                style={{ transform: `scale(${zoom})`, transformOrigin: "top center" }}
                className="max-w-full rounded shadow-sm transition-transform"
              />
            ) : (
              <div className="my-auto text-center text-xs text-slate-500 p-6">
                <p className="font-medium text-slate-700 dark:text-slate-300">Direct text source</p>
                <p className="mt-1">Original raw text:</p>
                <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-white p-3 text-left text-xs text-slate-700 border border-slate-200 dark:bg-slate-900 dark:text-slate-300 dark:border-slate-800">
                  {currentPage.raw_text}
                </pre>
              </div>
            )}
          </div>

          {/* PSD Layer Inspection */}
          {psdLayers.length > 0 && (
            <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-800/50">
              <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-slate-700 dark:text-slate-200">
                <Layers className="h-3.5 w-3.5" /> PSD Layers ({psdLayers.length})
              </p>
              <ul className="max-h-28 overflow-auto space-y-1 text-xs text-slate-600 dark:text-slate-300">
                {psdLayers.map((l, idx) => (
                  <li key={idx}>
                    • {l.name} <span className="text-slate-400">({l.kind})</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Right: Editable Extracted Text */}
        <div
          className={`${
            mobileTab === "extracted" ? "block" : "hidden"
          } lg:block rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 flex flex-col`}
        >
          <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
            <span>
              Editable Unicode Text ({currentPage.language_detected})
            </span>
            {highlightUncertain && lowConfidenceBlocks.length > 0 && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 font-medium text-amber-800">
                {lowConfidenceBlocks.length} low-confidence block(s)
              </span>
            )}
          </div>

          <textarea
            value={currentPage.edited_text}
            onChange={(e) => updatePageText(e.target.value, true)}
            rows={18}
            aria-label="Editable extracted text"
            className="w-full flex-1 rounded-xl border border-slate-300 bg-white p-3.5 text-sm leading-relaxed text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
          />

          {highlightUncertain && lowConfidenceBlocks.length > 0 && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50/70 p-2.5 text-xs text-amber-900 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-200">
              <p className="font-semibold mb-1">Low-confidence OCR segments to review:</p>
              <ul className="space-y-1 max-h-24 overflow-auto">
                {lowConfidenceBlocks.map((b) => (
                  <li key={b.id}>
                    • ({b.confidence?.toFixed(0)}%) “{b.text.slice(0, 90)}”
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Optional Tamil Legacy Encoding & Header/Footer Cleanup Tools */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <h4 className="text-xs font-semibold text-slate-800 dark:text-slate-200 mb-2">
            Legacy Tamil Encoding Conversion (Explicit Selection Only)
          </h4>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={legacyEncoding}
              onChange={(e) => setLegacyEncoding(e.target.value as "bamini" | "tab")}
              className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
            >
              <option value="bamini">Bamini → Unicode</option>
              <option value="tab">TAB → Unicode</option>
            </select>
            <button
              type="button"
              onClick={async () => {
                const res = await previewLegacyTamil(job.job_id, legacyEncoding, currentPage.page_number);
                setLegacyPreview(res.previews[0]?.converted_preview || "");
              }}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
            >
              Preview conversion
            </button>
            {legacyPreview !== null && (
              <button
                type="button"
                onClick={async () => {
                  await applyLegacyTamil(job.job_id, legacyEncoding, currentPage.page_number);
                  updatePageText(legacyPreview, true);
                  setLegacyPreview(null);
                }}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white"
              >
                Apply to page
              </button>
            )}
          </div>
          {legacyPreview !== null && (
            <p className="mt-2 rounded-lg bg-slate-50 p-2 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-200">
              Preview: {legacyPreview.slice(0, 220)}
            </p>
          )}
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <h4 className="text-xs font-semibold text-slate-800 dark:text-slate-200 mb-2">
            Repeated Header, Footer & Page Number Cleanup
          </h4>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={async () => {
                const res = await previewHeadersFooters(job.job_id);
                setHfCandidates(res);
              }}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
            >
              <Sparkles className="inline h-3.5 w-3.5 mr-1" />
              Scan repeated lines
            </button>
            {hfCandidates && (hfCandidates.headers.length > 0 || hfCandidates.footers.length > 0) && (
              <button
                type="button"
                onClick={async () => {
                  await applyHeadersFooters(job.job_id, hfCandidates.headers, hfCandidates.footers);
                  setHfCandidates(null);
                }}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white"
              >
                Remove detected
              </button>
            )}
          </div>
          {hfCandidates && (
            <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
              Headers: {hfCandidates.headers.join(", ") || "None"} · Footers: {hfCandidates.footers.join(", ") || "None"}
            </p>
          )}
        </div>
      </div>

      {/* Bottom Primary Actions */}
      <div className="fixed bottom-0 left-0 right-0 z-20 flex items-center justify-between border-t border-slate-200 bg-white p-3 sm:static sm:border-0 sm:bg-transparent sm:p-0 dark:border-slate-800 dark:bg-slate-900">
        <button
          type="button"
          onClick={onOpenStudyStudio}
          className="inline-flex items-center gap-2 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2.5 text-sm font-semibold text-indigo-700 hover:bg-indigo-100 dark:border-indigo-900 dark:bg-indigo-950 dark:text-indigo-300"
        >
          <BookOpen className="h-4 w-4" />
          <span>{t(locale, "openStudyStudio")}</span>
        </button>

        <button
          type="button"
          onClick={onContinueToDownload}
          className="rounded-xl bg-indigo-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500"
        >
          {t(locale, "continueDownload")} →
        </button>
      </div>
    </div>
  );
}
