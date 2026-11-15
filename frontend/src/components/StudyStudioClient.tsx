"use client";

import React, { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  BookOpen,
  CheckCircle2,
  Download,
  Eye,
  Flag,
  HelpCircle,
  Lock,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import {
  createStudyProject,
  exportStudyProjectBlob,
  getStudyProject,
  getStudyProviderStatus,
  listJobs,
  listStudyProjects,
  regenerateStudyProject,
  updateStudyItem,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { JobRecord, StudyItemRecord, StudyProjectRecord } from "@/lib/types";
import { useStudio } from "./StudioContext";

type StudyTab = "guide" | "bank" | "practice" | "answers" | "coverage";

export function StudyStudioClient() {
  const { locale } = useStudio();
  const searchParams = useSearchParams();
  const initialJobId = searchParams.get("jobId") || "";

  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [selectedJobId, setSelectedJobId] = useState(initialJobId);
  const [projects, setProjects] = useState<Array<{ project_id: string; title: string }>>([]);
  const [activeProject, setActiveProject] = useState<StudyProjectRecord | null>(null);
  const [activeTab, setActiveTab] = useState<StudyTab>("guide");

  // Generation settings
  const [language, setLanguage] = useState("bilingual");
  const [difficulty, setDifficulty] = useState("mixed");
  const [selectedMarks, setSelectedMarks] = useState<number[]>([1, 2, 3, 5, 10]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [providerStatus, setProviderStatus] = useState<{ configured: boolean; provider: string | null; instructions: string } | null>(null);
  const [studyError, setStudyError] = useState<string | null>(null);

  // Filters
  const [filterMarks, setFilterMarks] = useState<string>("all");
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [grammarMode, setGrammarMode] = useState<"original" | "corrected" | "compare">("compare");

  // Reveal states for hints & answers
  const [revealedHints, setRevealedHints] = useState<Record<string, boolean>>({});
  const [revealedAnswers, setRevealedAnswers] = useState<Record<string, boolean>>({});
  const [editingItemId, setEditingItemId] = useState<string | null>(null);
  const [editPrompt, setEditPrompt] = useState("");
  const [editAnswer, setEditAnswer] = useState("");

  // Export state
  const [exportFormat, setExportFormat] = useState("docx");
  const [exportLayout, setExportLayout] = useState("complete_study_guide");

  useEffect(() => {
    listJobs(1, 30)
      .then((res) => {
        const completed = res.items.filter((j) => j.status === "completed");
        setJobs(completed);
        if (!selectedJobId && completed.length > 0) setSelectedJobId(completed[0].job_id);
      })
      .catch((err) => {
        setStudyError(err instanceof Error ? err.message : "Backend API is currently unavailable.");
      });

    listStudyProjects()
      .then((res) => {
        setProjects(res.items);
        if (res.items.length > 0 && !activeProject) {
          getStudyProject(res.items[0].project_id).then(setActiveProject).catch(() => {});
        }
      })
      .catch(() => {});

    getStudyProviderStatus().then(setProviderStatus).catch(() => {});
  }, []);

  const handleGenerate = async () => {
    if (!selectedJobId) return;
    setIsGenerating(true);
    setStudyError(null);
    try {
      const proj = await createStudyProject({
        job_id: selectedJobId,
        language,
        difficulty,
        mark_categories: selectedMarks,
      });
      setActiveProject(proj);
      setProjects((prev) => [{ project_id: proj.project_id, title: proj.title }, ...prev]);
    } catch (err) {
      setStudyError(err instanceof Error ? err.message : "Study generation failed");
    } finally {
      setIsGenerating(false);
    }
  };

  const handleExportStudy = async () => {
    if (!activeProject) return;
    const blob = await exportStudyProjectBlob(activeProject.project_id, exportFormat, exportLayout);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `study_studio_${exportLayout}.${exportFormat}`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleSaveItemEdit = async (itemId: string) => {
    const updated = await updateStudyItem(itemId, {
      prompt_text: editPrompt,
      answer_text: editAnswer,
    });
    if (activeProject) {
      setActiveProject({
        ...activeProject,
        items: activeProject.items.map((it) => (it.id === itemId ? updated : it)),
      });
    }
    setEditingItemId(null);
  };

  const handleApproveLock = async (item: StudyItemRecord) => {
    const updated = await updateStudyItem(item.id, {
      verification_status: "user_approved",
      is_locked: !item.is_locked,
    });
    if (activeProject) {
      setActiveProject({
        ...activeProject,
        items: activeProject.items.map((it) => (it.id === item.id ? updated : it)),
      });
    }
  };

  const toggleMark = (m: number) => {
    setSelectedMarks((prev) => (prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m].sort((a, b) => a - b)));
  };

  const guideItems = (activeProject?.items || []).filter((i) => i.item_type === "guide_topic");
  const questionItems = (activeProject?.items || []).filter((i) => {
    if (i.item_type !== "question") return false;
    if (filterMarks !== "all" && String(i.marks) !== filterMarks) return false;
    if (filterStatus !== "all" && i.verification_status !== filterStatus) return false;
    return true;
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{t(locale, "studyTitle")}</h1>
          <p className="text-sm text-slate-600 dark:text-slate-300">{t(locale, "studySubtitle")}</p>
        </div>

        {projects.length > 0 && (
          <select
            value={activeProject?.project_id || ""}
            onChange={(e) => {
              if (e.target.value) getStudyProject(e.target.value).then(setActiveProject);
            }}
            className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
          >
            {projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.title}
              </option>
            ))}
          </select>
        )}
      </div>

      {/* AI Provider Status & Error Banner */}
      {providerStatus && !providerStatus.configured && (
        <div className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <p className="font-semibold">AI Provider Unconfigured</p>
          <p className="mt-1">{providerStatus.instructions}</p>
        </div>
      )}
      {studyError && (
        <div className="rounded-2xl border border-rose-200 bg-rose-50 p-4 text-xs text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
          {studyError}
        </div>
      )}

      {/* Generation Settings Card */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900 space-y-4">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">Source Document</label>
            <select
              value={selectedJobId}
              onChange={(e) => setSelectedJobId(e.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
            >
              {jobs.length === 0 && <option value="">No completed documents yet</option>}
              {jobs.map((j) => (
                <option key={j.job_id} value={j.job_id}>
                  {j.original_filename} ({j.page_count}p)
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">Language</label>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
            >
              <option value="bilingual">Bilingual (Tamil + English)</option>
              <option value="tam">Tamil</option>
              <option value="eng">English</option>
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">Difficulty</label>
            <select
              value={difficulty}
              onChange={(e) => setDifficulty(e.target.value)}
              className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-xs dark:border-slate-700 dark:bg-slate-800"
            >
              <option value="mixed">Mixed</option>
              <option value="foundational">Foundational</option>
              <option value="intermediate">Intermediate</option>
              <option value="advanced">Advanced</option>
            </select>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">Mark Categories</label>
            <div className="flex flex-wrap gap-1.5">
              {[1, 2, 3, 5, 10].map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => toggleMark(m)}
                  className={`rounded-lg border px-2.5 py-1 text-xs font-semibold ${
                    selectedMarks.includes(m)
                      ? "border-indigo-600 bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"
                      : "border-slate-200 text-slate-600 dark:border-slate-700"
                  }`}
                >
                  {m}M
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <button
            type="button"
            disabled={!selectedJobId || isGenerating}
            onClick={handleGenerate}
            className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            <Sparkles className="h-4 w-4" />
            <span>{isGenerating ? "Generating..." : "Generate Study Guide & Question Bank"}</span>
          </button>

          {activeProject && (
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={exportLayout}
                onChange={(e) => setExportLayout(e.target.value)}
                className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
              >
                <option value="complete_study_guide">Complete Study Guide</option>
                <option value="questions_with_answers">Questions with Answers</option>
                <option value="questions_only">Questions Only</option>
                <option value="answer_key">Separate Answer Key</option>
                <option value="chapter_wise">Chapter-wise Collection</option>
                <option value="by_marks">By Mark Category</option>
                <option value="mock_exam">Mock Exam</option>
              </select>
              <select
                value={exportFormat}
                onChange={(e) => setExportFormat(e.target.value)}
                className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
              >
                <option value="docx">Word (.docx)</option>
                <option value="pdf">PDF (.pdf)</option>
                <option value="txt">TXT (.txt)</option>
                <option value="md">Markdown (.md)</option>
                <option value="xlsx">Excel (.xlsx)</option>
                <option value="json">JSON (.json)</option>
                <option value="zip">All Formats (.zip)</option>
              </select>
              <button
                type="button"
                onClick={handleExportStudy}
                className="inline-flex items-center gap-1.5 rounded-lg bg-teal-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-teal-700"
              >
                <Download className="h-3.5 w-3.5" /> Export
              </button>
            </div>
          )}
        </div>
      </div>

      {/* 5 Study Studio Tabs */}
      {activeProject && (
        <>
          <div className="flex flex-wrap gap-2 border-b border-slate-200 pb-2 dark:border-slate-800">
            {[
              { id: "guide" as StudyTab, label: t(locale, "tabGuide") },
              { id: "bank" as StudyTab, label: t(locale, "tabBank") },
              { id: "practice" as StudyTab, label: t(locale, "tabPractice") },
              { id: "answers" as StudyTab, label: t(locale, "tabAnswers") },
              { id: "coverage" as StudyTab, label: t(locale, "tabCoverage") },
            ].map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={`rounded-xl px-4 py-2 text-sm font-semibold transition-colors ${
                  activeTab === tab.id
                    ? "bg-indigo-600 text-white"
                    : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Study Guide Tab */}
          {activeTab === "guide" && (
            <div className="space-y-4">
              {guideItems.map((g) => (
                <div key={g.id} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold uppercase tracking-wide text-indigo-600">{g.chapter}</span>
                    <span className="text-xs text-slate-500">{g.source_ref}</span>
                  </div>
                  <h3 className="mt-1 text-lg font-bold text-slate-900 dark:text-slate-100">{g.topic}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-slate-700 dark:text-slate-300">{g.answer_text}</p>
                  {g.scoring_points.length > 0 && (
                    <ul className="mt-3 list-disc pl-5 text-xs text-slate-600 dark:text-slate-400 space-y-1">
                      {g.scoring_points.map((sp, i) => (
                        <li key={i}>{sp}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Question Bank & Practice Test Tabs */}
          {(activeTab === "bank" || activeTab === "practice") && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={filterMarks}
                    onChange={(e) => setFilterMarks(e.target.value)}
                    className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
                  >
                    <option value="all">All Marks</option>
                    <option value="1">1 Mark</option>
                    <option value="2">2 Marks</option>
                    <option value="3">3 Marks</option>
                    <option value="5">5 Marks</option>
                    <option value="10">10 Marks</option>
                  </select>
                  <select
                    value={filterStatus}
                    onChange={(e) => setFilterStatus(e.target.value)}
                    className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
                  >
                    <option value="all">All Review Status</option>
                    <option value="ai_draft">AI Draft</option>
                    <option value="auto_checked">Auto-Checked</option>
                    <option value="user_approved">User Approved</option>
                  </select>
                </div>

                <button
                  type="button"
                  onClick={async () => {
                    const updated = await regenerateStudyProject(activeProject.project_id);
                    setActiveProject(updated);
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
                >
                  <RefreshCw className="h-3.5 w-3.5" /> Regenerate Unlocked
                </button>
              </div>

              {questionItems.map((q, idx) => (
                <div key={q.id} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <div className="flex items-center gap-2">
                      <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 font-semibold text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
                        {q.marks} Mark{q.marks === 1 ? "" : "s"}
                      </span>
                      <span className="text-slate-500">{q.topic} · {q.difficulty}</span>
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {q.verification_status}
                      </span>
                      {q.is_locked && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-teal-50 px-2 py-0.5 font-semibold text-teal-700">
                          <Lock className="h-3 w-3" /> {t(locale, "lockedBadge")}
                        </span>
                      )}
                    </div>
                    <span className="text-slate-400">{q.source_ref}</span>
                  </div>

                  {editingItemId === q.id ? (
                    <div className="mt-3 space-y-2">
                      <textarea
                        value={editPrompt}
                        onChange={(e) => setEditPrompt(e.target.value)}
                        rows={2}
                        className="w-full rounded-lg border border-slate-300 p-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                      />
                      <textarea
                        value={editAnswer}
                        onChange={(e) => setEditAnswer(e.target.value)}
                        rows={3}
                        className="w-full rounded-lg border border-slate-300 p-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                      />
                      <div className="flex gap-2">
                        <button
                          type="button"
                          onClick={() => handleSaveItemEdit(q.id)}
                          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white"
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditingItemId(null)}
                          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <p className="mt-2.5 text-sm font-semibold text-slate-900 dark:text-slate-100">
                        Q{idx + 1}. {q.prompt_text}
                      </p>
                      {q.options.length > 0 && (
                        <ul className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2 text-xs text-slate-700 dark:text-slate-300">
                          {q.options.map((opt, oi) => (
                            <li key={oi} className="rounded-lg border border-slate-200 px-3 py-1.5 dark:border-slate-800">
                              {String.fromCharCode(65 + oi)}) {opt}
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  )}

                  {revealedHints[q.id] && (
                    <p className="mt-2 rounded-lg bg-amber-50 p-2.5 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                      Hint: {q.hint_text}
                    </p>
                  )}

                  {(activeTab === "bank" && revealedAnswers[q.id]) || (activeTab === "practice" && revealedAnswers[q.id]) ? (
                    <div className="mt-2 rounded-xl bg-teal-50/70 p-3 text-xs text-slate-800 dark:bg-teal-950/30 dark:text-slate-200">
                      <p className="font-semibold text-teal-800 dark:text-teal-300">Answer:</p>
                      <p className="mt-1 whitespace-pre-wrap">{q.answer_text}</p>
                      {q.explanation_text && <p className="mt-1.5 text-slate-600 dark:text-slate-400">{q.explanation_text}</p>}
                    </div>
                  ) : null}

                  <div className="mt-3 flex flex-wrap items-center gap-2 pt-1">
                    <button
                      type="button"
                      onClick={() => setRevealedHints((p) => ({ ...p, [q.id]: !p[q.id] }))}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
                    >
                      <HelpCircle className="h-3.5 w-3.5" /> {t(locale, "revealHint")}
                    </button>
                    <button
                      type="button"
                      onClick={() => setRevealedAnswers((p) => ({ ...p, [q.id]: !p[q.id] }))}
                      className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
                    >
                      <Eye className="h-3.5 w-3.5" /> {t(locale, "revealAnswer")}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setEditingItemId(q.id);
                        setEditPrompt(q.prompt_text);
                        setEditAnswer(q.answer_text);
                      }}
                      className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium hover:bg-slate-50 dark:border-slate-700"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => handleApproveLock(q)}
                      className="inline-flex items-center gap-1 rounded-lg border border-teal-200 bg-teal-50 px-2.5 py-1 text-xs font-medium text-teal-700"
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" /> {t(locale, "approveLock")}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Answers Key Tab */}
          {activeTab === "answers" && (
            <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900 space-y-3">
              <h3 className="text-sm font-semibold">Answer Key ({questionItems.length} questions)</h3>
              {questionItems.map((q, idx) => (
                <div key={q.id} className="border-b border-slate-100 pb-3 last:border-0 dark:border-slate-800">
                  <p className="text-xs font-semibold text-slate-900 dark:text-slate-100">
                    {idx + 1}. {q.prompt_text} [{q.marks}M]
                  </p>
                  <p className="mt-1 text-xs text-slate-700 dark:text-slate-300 whitespace-pre-wrap">{q.answer_text}</p>
                </div>
              ))}
            </div>
          )}

          {/* Coverage & Grammar Diff Tab */}
          {activeTab === "coverage" && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
                  <p className="text-xs text-slate-500">Processed Pages</p>
                  <p className="text-xl font-bold">{activeProject.coverage_report.processed_pages ?? 0}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
                  <p className="text-xs text-slate-500">Topics Covered</p>
                  <p className="text-xl font-bold">{activeProject.coverage_report.covered_topics_count ?? 0}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
                  <p className="text-xs text-slate-500">Questions Generated</p>
                  <p className="text-xl font-bold">{activeProject.coverage_report.generated_questions_count ?? 0}</p>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
                  <p className="text-xs text-slate-500">Duplicates Removed</p>
                  <p className="text-xl font-bold">{activeProject.coverage_report.duplicates_removed ?? 0}</p>
                </div>
              </div>

              {/* Grammar Modes */}
              <div className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
                <div className="mb-3 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Grammar & Spelling Review</h3>
                  <div className="flex gap-1.5">
                    {(["original", "corrected", "compare"] as const).map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => setGrammarMode(m)}
                        className={`rounded-lg px-2.5 py-1 text-xs font-medium capitalize ${
                          grammarMode === m ? "bg-indigo-600 text-white" : "border border-slate-200 dark:border-slate-700"
                        }`}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                </div>
                {activeProject.grammar_diffs.map((gd) => (
                  <div key={gd.page_number} className="mb-3 rounded-xl bg-slate-50 p-3 text-xs dark:bg-slate-800/50">
                    <p className="font-semibold mb-1">Page {gd.page_number}</p>
                    {grammarMode === "original" && <pre className="whitespace-pre-wrap">{gd.original_text}</pre>}
                    {grammarMode === "corrected" && <pre className="whitespace-pre-wrap">{gd.corrected_text}</pre>}
                    {grammarMode === "compare" && (
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        <pre className="whitespace-pre-wrap rounded bg-white p-2 dark:bg-slate-900">{gd.original_text}</pre>
                        <pre className="whitespace-pre-wrap rounded bg-white p-2 dark:bg-slate-900">{gd.corrected_text}</pre>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
