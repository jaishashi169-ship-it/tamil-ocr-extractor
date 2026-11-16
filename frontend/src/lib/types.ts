export type WorkflowStep = "upload" | "options" | "review" | "download";
export type WorkflowAction = "extract" | "convert" | "compress";
export type OcrLanguage = "tam" | "eng" | "tam+eng";
export type DocumentType = "printed" | "textbook" | "handwritten";
export type OcrEngine = "auto" | "tesseract" | "gemini";
export type QualityPreset = "original" | "high" | "balanced" | "smallest" | "custom";
export type ConversionMode = "clean_editable" | "layout_preserved";
export type Locale = "en" | "ta";

export const MAX_UPLOAD_BYTES = 500_000_000;
export const CHUNK_SIZE_BYTES = 5_242_880;

export interface UploadQueueItem {
  localId: string;
  file: File;
  sessionId?: string;
  status: "pending" | "uploading" | "paused" | "completed" | "error" | "cancelled";
  progressPercent: number;
  uploadedBytes: number;
  error?: string;
  formatType?: string;
  sha256?: string;
}

export interface JobSettings {
  language: OcrLanguage;
  document_type?: DocumentType;
  engine?: OcrEngine;
  conversion_mode: ConversionMode;
  workflow_action: WorkflowAction;
  target_output_format: string;
  quality_preset: QualityPreset;
  auto_rotate: boolean;
  deskew: boolean;
  denoise: boolean;
  enhance_contrast: boolean;
  multi_column: boolean;
  force_ocr?: boolean;
}

export interface JobRecord {
  job_id: string;
  batch_id?: string | null;
  original_filename: string;
  format_type: string;
  file_size_bytes: number;
  file_sha256?: string | null;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress_percent: number;
  stage_label: string;
  page_count: number;
  processed_pages: number;
  settings: Partial<JobSettings>;
  metadata?: Record<string, unknown>;
  error_message?: string | null;
  needs_password: boolean;
  duration_ms?: number | null;
  created_at?: string | null;
}

export interface TextBlockModel {
  id: string;
  block_type: string;
  text: string;
  raw_ocr_text?: string | null;
  language: string;
  extraction_method: string;
  confidence?: number | null;
  source_ref?: {
    filename: string;
    page_number: number;
    layer_name?: string | null;
  } | null;
}

export interface PageResultRecord {
  page_number: number;
  source_type: string;
  extraction_method: string;
  language_detected: string;
  confidence_avg?: number | null;
  raw_text: string;
  edited_text: string;
  has_user_edits: boolean;
  draft_version?: number;
  has_preview: boolean;
  has_thumbnail: boolean;
  model?: {
    blocks?: TextBlockModel[];
    warnings?: string[];
    metadata?: Record<string, unknown>;
  };
}

export interface ExportArtifactRecord {
  artifact_id: string;
  job_id: string;
  export_format: string;
  quality_preset: string;
  file_size_bytes: number;
  uncompressed_size_bytes?: number | null;
  savings_percent?: number | null;
  warnings: string[];
  status: string;
  created_at?: string | null;
}

export interface StudyItemRecord {
  id: string;
  project_id: string;
  item_type: "guide_topic" | "question";
  chapter: string;
  topic: string;
  marks?: number | null;
  difficulty: string;
  language: string;
  question_type?: string | null;
  prompt_text: string;
  answer_text: string;
  explanation_text: string;
  hint_text: string;
  scoring_points: string[];
  options: string[];
  study_support?: Record<string, unknown>;
  source_page?: number | null;
  source_ref?: string | null;
  verification_status: "ai_draft" | "auto_checked" | "user_approved";
  is_locked: boolean;
  issue_note?: string | null;
}

export interface StudyProjectRecord {
  project_id: string;
  job_id: string;
  title: string;
  status: string;
  coverage_report: {
    processed_pages?: number;
    total_document_pages?: number;
    covered_topics_count?: number;
    covered_topics?: string[];
    generated_questions_count?: number;
    by_marks?: Record<string, number>;
    duplicates_removed?: number;
    passages_needing_review?: Array<{ page_number: number; reason: string }>;
    verification_warnings?: string[];
  };
  grammar_diffs: Array<{
    page_number: number;
    original_text: string;
    corrected_text: string;
    has_changes: boolean;
    notes: Array<{ type: string; description: string }>;
    diff: string[];
  }>;
  items: StudyItemRecord[];
}
