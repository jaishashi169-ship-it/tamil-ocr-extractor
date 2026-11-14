import React, { Suspense } from "react";
import { StudyStudioClient } from "@/components/StudyStudioClient";

export default function StudyPage() {
  return (
    <Suspense fallback={<div className="p-8 text-sm text-slate-500">Loading Study Studio...</div>}>
      <StudyStudioClient />
    </Suspense>
  );
}
