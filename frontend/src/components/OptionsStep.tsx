"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronUp, FileCheck2, Lock, Play } from "lucide-react";
import { formatBytes } from "@/lib/api";
import { t } from "@/lib/i18n";
import { DocumentType, JobSettings, OcrEngine, OcrLanguage, QualityPreset, UploadQueueItem } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface OptionsStepProps {
  completedUploads: UploadQueueItem[];
  settings: JobSettings;
  onChangeSettings: (next: JobSettings) => void;
  password: string;
  onChangePassword: (pwd: string) => void;
  isStarting: boolean;
  onStartProcessing: () => void;
  onBack: () => void;
}

const OUTPUT_FORMATS = [
  { id: "docx", label: "Word (.docx)" },
  { id: "txt", label: "Plain Text (.txt)" },
  { id: "md", label: "Markdown (.md)" },
  { id: "searchable_pdf", label: "Searchable PDF (.pdf)" },
  { id: "pdf", label: "Reflowed PDF (.pdf)" },
  { id: "html", label: "HTML (.html)" },
  { id: "json", label: "Structured JSON (.json)" },
  { id: "xlsx", label: "Excel Tables (.xlsx)" },
  { id: "csv", label: "CSV Tables (.csv)" },
  { id: "png", label: "PNG Preview (.png)" },
  { id: "jpg", label: "JPEG Preview (.jpg)" },
  { id: "webp", label: "WEBP Preview (.webp)" },
];

export function OptionsStep({
  completedUploads,
  settings,
  onChangeSettings,
  password,
  onChangePassword,
  isStarting,
  onStartProcessing,
  onBack,
}: OptionsStepProps) {
  const { locale } = useStudio();
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [installedLanguages, setInstalledLanguages] = useState<Array<{ code: string; label: string }>>([]);

  React.useEffect(() => {
    fetch("/api/v1/languages")
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => {
        if (Array.isArray(data) && data.length > 0) {
          setInstalledLanguages(data);
        }
      })
      .catch(() => {});
  }, []);

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      {/* Uploaded File Summary */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h3 className="mb-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
          Selected File{completedUploads.length > 1 ? "s" : ""} ({completedUploads.length})
        </h3>
        <div className="space-y-2">
          {completedUploads.map((u) => (
            <div
              key={u.localId}
              className="flex items-center justify-between rounded-xl bg-slate-50 px-3.5 py-2.5 dark:bg-slate-800/60"
            >
              <div className="flex items-center gap-2.5 min-w-0">
                <FileCheck2 className="h-5 w-5 shrink-0 text-teal-600" />
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{u.file.name}</p>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    {formatBytes(u.file.size)} · {(u.formatType || "document").toUpperCase()}
                  </p>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Main Options Card */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900 space-y-5">
        {/* Language Selector */}
        <div>
          <label className="block text-sm font-semibold text-slate-900 dark:text-slate-100 mb-2">
            {t(locale, "languageLabel")}
          </label>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            {[
              { id: "tam" as OcrLanguage, label: t(locale, "langTamil") },
              { id: "eng" as OcrLanguage, label: t(locale, "langEnglish") },
              { id: "tam+eng" as OcrLanguage, label: t(locale, "langBoth") },
            ].map((opt) => (
              <button
                key={opt.id}
                type="button"
                onClick={() => onChangeSettings({ ...settings, language: opt.id })}
                className={`rounded-xl border px-3 py-2.5 text-sm font-medium transition-colors ${
                  settings.language === opt.id
                    ? "border-indigo-600 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"
                    : "border-slate-200 text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {installedLanguages.length > 0 && (
            <div className="mt-2.5">
              <label className="block text-xs text-slate-500 dark:text-slate-400 mb-1">
                Installed OCR recognition models:
              </label>
              <select
                value={settings.language}
                onChange={(e) => onChangeSettings({ ...settings, language: e.target.value })}
                className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs text-slate-800 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
              >
                <option value="tam+eng">Tamil + English (tam+eng)</option>
                <option value="tam">Tamil only (tam)</option>
                <option value="eng">English only (eng)</option>
                {installedLanguages
                  .filter((l) => l.code !== "tam" && l.code !== "eng")
                  .map((l) => (
                    <option key={l.code} value={l.code}>
                      {l.label} ({l.code})
                    </option>
                  ))}
              </select>
            </div>
          )}
        </div>

        {/* Document Style & Content Type */}
        <div>
          <label className="block text-sm font-semibold text-slate-900 dark:text-slate-100 mb-2">
            Document Style & Source Type
          </label>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            {[
              {
                id: "printed" as DocumentType,
                title: "Standard Printed",
                desc: "Typed docs, letters, forms & digital papers",
                badge: "Tesseract / Auto",
              },
              {
                id: "textbook" as DocumentType,
                title: "Textbooks & Guides",
                desc: "School/college books, TNPSC guides, multi-column & gutter fix",
                badge: "Layout + Gutter Fix",
              },
              {
                id: "handwritten" as DocumentType,
                title: "Handwritten Notes",
                desc: "Tamil & English cursive, notebooks, exam answers & manuscripts",
                badge: "Stroke Enhancer + Vision AI",
              },
            ].map((opt) => (
              <button
                key={opt.id}
                type="button"
                onClick={() => onChangeSettings({ ...settings, document_type: opt.id })}
                className={`flex flex-col text-left rounded-xl border p-3 transition-colors ${
                  (settings.document_type || "printed") === opt.id
                    ? "border-indigo-600 bg-indigo-50/80 ring-1 ring-indigo-600 text-indigo-900 dark:bg-indigo-950/70 dark:text-indigo-200"
                    : "border-slate-200 text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-xs sm:text-sm">{opt.title}</span>
                  <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-300">
                    {opt.badge}
                  </span>
                </div>
                <p className="mt-1 text-[11px] leading-snug text-slate-500 dark:text-slate-400">
                  {opt.desc}
                </p>
              </button>
            ))}
          </div>
        </div>

        {/* Output Format */}
        <div>
          <label htmlFor="output-format-select" className="block text-sm font-semibold text-slate-900 dark:text-slate-100 mb-2">
            {t(locale, "outputFormatLabel")}
          </label>
          <select
            id="output-format-select"
            value={settings.target_output_format}
            onChange={(e) => onChangeSettings({ ...settings, target_output_format: e.target.value })}
            className="w-full rounded-xl border border-slate-300 bg-white px-3.5 py-2.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
          >
            {OUTPUT_FORMATS.map((f) => (
              <option key={f.id} value={f.id}>
                {f.label}
              </option>
            ))}
          </select>
        </div>

        {/* Quality Preset */}
        <div>
          <label className="block text-sm font-semibold text-slate-900 dark:text-slate-100 mb-2">
            {t(locale, "qualityLabel")}
          </label>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
            {[
              { id: "high" as QualityPreset, label: t(locale, "qualityHigh") },
              { id: "balanced" as QualityPreset, label: t(locale, "qualityBalanced") },
              { id: "smallest" as QualityPreset, label: t(locale, "qualitySmallest") },
            ].map((q) => (
              <button
                key={q.id}
                type="button"
                onClick={() => onChangeSettings({ ...settings, quality_preset: q.id })}
                className={`rounded-xl border px-3 py-2.5 text-sm font-medium transition-colors ${
                  settings.quality_preset === q.id
                    ? "border-indigo-600 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"
                    : "border-slate-200 text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                {q.label}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">{t(locale, "qualityNotice")}</p>
        </div>

        {/* Expandable Advanced Settings */}
        <div className="border-t border-slate-100 pt-4 dark:border-slate-800">
          <button
            type="button"
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="flex w-full items-center justify-between text-sm font-medium text-slate-700 hover:text-indigo-600 dark:text-slate-300"
          >
            <span>{t(locale, "advancedSettings")}</span>
            {showAdvanced ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </button>

          {showAdvanced && (
            <div className="mt-4 space-y-4 rounded-xl bg-slate-50 p-4 dark:bg-slate-800/50">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {[
                  { key: "auto_rotate", label: "Auto-correct page rotation" },
                  { key: "deskew", label: "Deskew tilted scans" },
                  { key: "enhance_contrast", label: "Enhance contrast (CLAHE)" },
                  { key: "denoise", label: "Denoise grainy scans" },
                  { key: "multi_column", label: "Detect multi-column reading order" },
                  { key: "force_ocr", label: "Force full visual OCR (Bypass PDF embedded text - for scans/legacy fonts)" },
                ].map((chk) => (
                  <label key={chk.key} className="flex items-center gap-2 text-xs font-medium text-slate-700 dark:text-slate-300">
                    <input
                      type="checkbox"
                      checked={Boolean(settings[chk.key as keyof JobSettings])}
                      onChange={(e) => onChangeSettings({ ...settings, [chk.key]: e.target.checked })}
                      className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                    />
                    <span>{chk.label}</span>
                  </label>
                ))}
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1">
                  OCR Engine
                </label>
                <select
                  value={settings.engine || "auto"}
                  onChange={(e) =>
                    onChangeSettings({
                      ...settings,
                      engine: e.target.value as OcrEngine,
                    })
                  }
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
                >
                  <option value="auto">Auto (Gemini Vision AI for handwriting + Enhanced Tesseract)</option>
                  <option value="tesseract">Local Tesseract Engine (Offline & Fast)</option>
                  <option value="gemini">Gemini Multimodal Vision AI (Maximum Handwriting & Textbook Accuracy)</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-700 dark:text-slate-300 mb-1">
                  Conversion Mode
                </label>
                <select
                  value={settings.conversion_mode}
                  onChange={(e) =>
                    onChangeSettings({
                      ...settings,
                      conversion_mode: e.target.value as "clean_editable" | "layout_preserved",
                    })
                  }
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
                >
                  <option value="clean_editable">Clean editable content (readable paragraphs & headings)</option>
                  <option value="layout_preserved">Layout preservation (keep page structure where supported)</option>
                </select>
              </div>

              <div>
                <label className="flex items-center gap-1.5 text-xs font-medium text-slate-700 dark:text-slate-300 mb-1">
                  <Lock className="h-3.5 w-3.5" />
                  <span>Document password (only if encrypted; never stored)</span>
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => onChangePassword(e.target.value)}
                  placeholder="Optional password for protected PDF"
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
                />
              </div>
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={onBack}
            className="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300"
          >
            ← Back
          </button>
          <button
            type="button"
            disabled={isStarting}
            onClick={onStartProcessing}
            className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:opacity-60 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          >
            <Play className="h-4 w-4" />
            <span>{isStarting ? t(locale, "processing") : t(locale, "startProcessing")}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
