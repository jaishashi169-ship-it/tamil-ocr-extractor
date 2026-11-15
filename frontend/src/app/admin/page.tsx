"use client";

import React, { useEffect, useState } from "react";
import { Activity, Cpu, Database, HardDrive, Languages } from "lucide-react";
import { formatBytes, getCapabilities } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useStudio } from "@/components/StudioContext";

export default function AdminPage() {
  const { locale } = useStudio();
  const [backendError, setBackendError] = useState<string | null>(null);
  const [caps, setCaps] = useState<{
    max_upload_bytes: number;
    installed_ocr_languages: string[];
    supported_imports: string[];
    supported_exports: string[];
  } | null>(null);

  useEffect(() => {
    getCapabilities()
      .then((data) => {
        setCaps(data);
        setBackendError(null);
      })
      .catch((err) => {
        setBackendError(err instanceof Error ? err.message : "Backend API is currently unavailable.");
      });
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{t(locale, "adminTitle")}</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          System capabilities, installed OCR language packs, and privacy-preserving operational limits.
        </p>
      </div>

      {backendError && (
        <div
          role="alert"
          className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-200"
        >
          <strong>Backend Unavailable:</strong> {backendError}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
            <HardDrive className="h-4 w-4 text-indigo-600" /> Max File Limit
          </div>
          <p className="mt-2 text-xl font-bold">{caps ? formatBytes(caps.max_upload_bytes) : "500 MB"}</p>
          <p className="text-xs text-slate-400">500,000,000 bytes strict cap</p>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
            <Languages className="h-4 w-4 text-teal-600" /> Installed OCR Packs
          </div>
          <p className="mt-2 text-xl font-bold">
            {caps?.installed_ocr_languages?.length ? caps.installed_ocr_languages.join(", ") : "tam, eng (Docker)"}
          </p>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
            <Cpu className="h-4 w-4 text-indigo-600" /> Worker Isolation
          </div>
          <p className="mt-2 text-xl font-bold text-teal-600">Isolated Internal Net</p>
          <p className="text-xs text-slate-400">No external egress; document text never logged</p>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
            <Database className="h-4 w-4 text-indigo-600" /> Supported Formats
          </div>
          <p className="mt-2 text-sm font-semibold">
            {caps ? `${caps.supported_imports.length} imports · ${caps.supported_exports.length} exports` : "15 imports · 13 exports"}
          </p>
        </div>
      </div>

      {caps && (
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900 space-y-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Activity className="h-4 w-4 text-indigo-600" /> Live Capability Matrix
          </h2>
          <p className="text-xs text-slate-600 dark:text-slate-300">
            <strong>Imports:</strong> {caps.supported_imports.join(", ")}
          </p>
          <p className="text-xs text-slate-600 dark:text-slate-300">
            <strong>Exports:</strong> {caps.supported_exports.join(", ")}
          </p>
        </div>
      )}
    </div>
  );
}
