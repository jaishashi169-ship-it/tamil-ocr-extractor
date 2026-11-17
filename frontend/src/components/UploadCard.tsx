"use client";

import React, { useRef, useState } from "react";
import {
  CheckCircle2,
  FileUp,
  Pause,
  Play,
  RefreshCw,
  Trash2,
  X,
  FileText,
  ArrowRightLeft,
  Minimize2,
  AlertCircle,
  Zap,
  BookOpen,
  PenTool,
  Printer,
  Sparkles,
} from "lucide-react";
import { formatBytes } from "@/lib/api";
import { t } from "@/lib/i18n";
import { DocumentType, MAX_UPLOAD_BYTES, UploadQueueItem, WorkflowAction } from "@/lib/types";
import { useStudio } from "./StudioContext";

interface UploadCardProps {
  queue: UploadQueueItem[];
  selectedAction: WorkflowAction;
  onSelectAction: (action: WorkflowAction) => void;
  selectedDocType?: DocumentType;
  onSelectDocType?: (docType: DocumentType) => void;
  onAddFiles: (files: FileList | File[]) => void;
  onPauseItem: (localId: string) => void;
  onResumeItem: (localId: string) => void;
  onCancelItem: (localId: string) => void;
  onRetryItem: (localId: string) => void;
  onRemoveItem: (localId: string) => void;
  onClearQueue?: () => void;
  onContinueToOptions: () => void;
  onAutoExtract?: (localId?: string) => void;
  isStarting?: boolean;
}

export function UploadCard({
  queue,
  selectedAction,
  onSelectAction,
  selectedDocType = "textbook",
  onSelectDocType,
  onAddFiles,
  onPauseItem,
  onResumeItem,
  onCancelItem,
  onRetryItem,
  onRemoveItem,
  onClearQueue,
  onContinueToOptions,
  onAutoExtract,
  isStarting = false,
}: UploadCardProps) {
  const { locale } = useStudio();
  const inputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      onAddFiles(e.dataTransfer.files);
    }
  };

  const completedCount = queue.filter((q) => q.status === "completed").length;

  const docStyleCards: {
    id: DocumentType;
    title: string;
    engine: string;
    description: string;
    icon: React.ComponentType<{ className?: string }>;
  }[] = [
    {
      id: "printed",
      title: "Standard Printed",
      engine: "Tesseract / Auto",
      description: "Typed docs, letters, forms & digital papers",
      icon: Printer,
    },
    {
      id: "textbook",
      title: "Textbooks & Guides",
      engine: "Layout + Gutter Fix",
      description: "School/college books, TNPSC guides, multi-column & gutter fix",
      icon: BookOpen,
    },
    {
      id: "handwritten",
      title: "Handwritten Notes",
      engine: "Stroke Enhancer + Vision AI",
      description: "Tamil & English cursive, notebooks, exam answers & manuscripts",
      icon: PenTool,
    },
  ];

  return (
    <div className="space-y-6">
      {/* Main Upload Card */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        className={`rounded-2xl border-2 border-dashed bg-white p-8 text-center shadow-sm transition-colors dark:bg-slate-900 sm:p-12 ${
          isDragging
            ? "border-indigo-500 bg-indigo-50/50 dark:bg-indigo-950/30"
            : "border-slate-300 hover:border-indigo-400 dark:border-slate-700"
        }`}
      >
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-600 dark:bg-indigo-950 dark:text-indigo-400">
          <FileUp className="h-7 w-7" />
        </div>
        <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-100">{t(locale, "dropTitle")}</h2>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{t(locale, "orText")}</p>

        <div className="mt-4">
          <input
            ref={inputRef}
            type="file"
            multiple
            className="hidden"
            accept=".pdf,.docx,.doc,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp,.psd,.html,.htm,.csv,.json"
            onChange={(e) => {
              if (e.target.files && e.target.files.length > 0) {
                onAddFiles(e.target.files);
                e.target.value = "";
              }
            }}
          />
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2"
          >
            <FileUp className="h-4 w-4" />
            <span>{t(locale, "chooseFile")}</span>
          </button>
        </div>

        <p className="mt-4 text-xs font-medium text-slate-500 dark:text-slate-400">
          {t(locale, "uploadHint")} ({MAX_UPLOAD_BYTES.toLocaleString("en-US")} bytes)
        </p>
      </div>

      {/* Document Style & Source Type Selector */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <label className="text-sm font-semibold text-slate-800 dark:text-slate-200 flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-indigo-500" />
            <span>Document Style & Source Type</span>
          </label>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            Optimized OCR & Layout Pipeline
          </span>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {docStyleCards.map((card) => {
            const Icon = card.icon;
            const active = selectedDocType === card.id;
            return (
              <button
                key={card.id}
                type="button"
                onClick={() => onSelectDocType?.(card.id)}
                className={`relative flex flex-col items-start p-4 text-left rounded-xl border transition-all ${
                  active
                    ? "border-indigo-600 bg-indigo-50/70 ring-2 ring-indigo-500/20 dark:bg-indigo-950/50 dark:border-indigo-500"
                    : "border-slate-200 bg-white hover:border-slate-300 dark:border-slate-800 dark:bg-slate-900 dark:hover:border-slate-700"
                }`}
              >
                <div className="flex items-center justify-between w-full mb-2">
                  <div className="flex items-center gap-2">
                    <Icon className={`h-4 w-4 ${active ? "text-indigo-600 dark:text-indigo-400" : "text-slate-500"}`} />
                    <span className="text-sm font-bold text-slate-900 dark:text-slate-100">
                      {card.title}
                    </span>
                  </div>
                  {active && (
                    <span className="h-2 w-2 rounded-full bg-indigo-600 dark:bg-indigo-400" />
                  )}
                </div>
                <span className="inline-block rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-700 dark:bg-slate-800 dark:text-slate-300 mb-1.5">
                  {card.engine}
                </span>
                <p className="text-xs text-slate-500 dark:text-slate-400 leading-snug">
                  {card.description}
                </p>
              </button>
            );
          })}
        </div>
      </div>

      {/* Three Simple Choices */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {[
          { id: "extract" as WorkflowAction, label: t(locale, "actionExtract"), icon: FileText },
          { id: "convert" as WorkflowAction, label: t(locale, "actionConvert"), icon: ArrowRightLeft },
          { id: "compress" as WorkflowAction, label: t(locale, "actionCompress"), icon: Minimize2 },
        ].map((item) => {
          const Icon = item.icon;
          const active = selectedAction === item.id;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelectAction(item.id)}
              className={`flex items-center justify-center gap-2.5 rounded-xl border px-4 py-3 text-sm font-medium shadow-sm transition-all focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
                active
                  ? "border-indigo-600 bg-indigo-50 text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300"
                  : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
              }`}
            >
              <Icon className="h-4 w-4" />
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>

      {/* Batch Upload Queue */}
      {queue.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                Upload Queue ({completedCount}/{queue.length})
              </h3>
              {onClearQueue && (
                <button
                  type="button"
                  onClick={onClearQueue}
                  className="rounded px-2 py-0.5 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
                  title="Clear all items from queue"
                >
                  Clear Queue
                </button>
              )}
            </div>
            {completedCount > 0 && (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => onAutoExtract?.()}
                  disabled={isStarting}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-indigo-600 to-indigo-700 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:from-indigo-700 hover:to-indigo-800 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
                >
                  <Zap className="h-4 w-4 fill-amber-300 text-amber-300" />
                  <span>Auto Extract All ({completedCount})</span>
                </button>
                <button
                  type="button"
                  onClick={onContinueToOptions}
                  className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
                >
                  {t(locale, "stepOptions")} →
                </button>
              </div>
            )}
          </div>

          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {queue.map((item) => (
              <li key={item.localId} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{item.file.name}</p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">
                      {formatBytes(item.uploadedBytes)} / {formatBytes(item.file.size)} · {item.status}
                    </p>
                  </div>

                  <div className="flex items-center gap-1.5">
                    {item.status === "uploading" && (
                      <>
                        <button
                          type="button"
                          onClick={() => onPauseItem(item.localId)}
                          className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200"
                        >
                          <Pause className="inline h-3.5 w-3.5 mr-1" />
                          {t(locale, "pause")}
                        </button>
                        <button
                          type="button"
                          onClick={() => onCancelItem(item.localId)}
                          className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-rose-600 hover:bg-rose-50 dark:border-slate-700"
                        >
                          <X className="inline h-3.5 w-3.5 mr-1" />
                          {t(locale, "cancel")}
                        </button>
                      </>
                    )}
                    {item.status === "paused" && (
                      <button
                        type="button"
                        onClick={() => onResumeItem(item.localId)}
                        className="rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100"
                      >
                        <Play className="inline h-3.5 w-3.5 mr-1" />
                        {t(locale, "resume")}
                      </button>
                    )}
                    {item.status === "error" && (
                      <button
                        type="button"
                        onClick={() => onRetryItem(item.localId)}
                        className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100"
                      >
                        <RefreshCw className="inline h-3.5 w-3.5 mr-1" />
                        {t(locale, "retry")}
                      </button>
                    )}
                    {item.status === "completed" && (
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => onAutoExtract?.(item.localId)}
                          disabled={isStarting}
                          className="inline-flex items-center gap-1 rounded-lg bg-indigo-600 px-3 py-1 text-xs font-semibold text-white shadow-sm hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50"
                        >
                          <Zap className="h-3 w-3 fill-amber-300 text-amber-300" />
                          <span>Auto Extract Now</span>
                        </button>
                        <span className="inline-flex items-center gap-1 rounded-full bg-teal-50 px-2 py-0.5 text-xs font-medium text-teal-700 dark:bg-teal-950 dark:text-teal-300">
                          <CheckCircle2 className="h-3 w-3" />
                          Ready
                        </span>
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => onRemoveItem(item.localId)}
                      aria-label="Remove from queue"
                      className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>

                {/* Upload progress bar */}
                <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                  <div
                    className={`h-full transition-all ${
                      item.status === "completed"
                        ? "bg-teal-600"
                        : item.status === "error"
                        ? "bg-rose-500"
                        : "bg-indigo-600"
                    }`}
                    style={{ width: `${item.progressPercent}%` }}
                  />
                </div>

                {item.error && (
                  <p className="mt-1.5 flex items-center gap-1.5 text-xs text-rose-600 dark:text-rose-400">
                    <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                    <span>{item.error}</span>
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
