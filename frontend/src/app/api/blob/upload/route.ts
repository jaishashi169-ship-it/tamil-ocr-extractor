import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import { NextResponse } from "next/server";

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = (await request.json()) as HandleUploadBody;

    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname: string, clientPayload: string | null) => {
        if (!clientPayload) {
          throw new Error("Missing upload authorization payload.");
        }
        let parsed: { session_id?: string; upload_authorization_token?: string };
        try {
          parsed = JSON.parse(clientPayload);
        } catch {
          throw new Error("Invalid clientPayload JSON.");
        }
        if (!parsed.session_id || !parsed.upload_authorization_token) {
          throw new Error("Missing session_id or upload_authorization_token.");
        }

        const origin = new URL(request.url).origin;
        const backendApiBase = (
          process.env.INTERNAL_BACKEND_API_URL ||
          `${origin}/api/v1`
        ).replace(/\/$/, "");

        const authRes = await fetch(
          `${backendApiBase}/uploads/${encodeURIComponent(parsed.session_id)}/authorize-blob`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              pathname,
              upload_authorization_token: parsed.upload_authorization_token,
            }),
          }
        );

        if (!authRes.ok) {
          const errBody = await authRes.json().catch(() => ({}));
          const detail =
            typeof errBody.detail === "string"
              ? errBody.detail
              : errBody.detail?.message || `Upload authorization failed (${authRes.status})`;
          throw new Error(detail);
        }

        const verified = await authRes.json();

        return {
          maximumSizeInBytes: Number(verified.expected_size_bytes),
          addRandomSuffix: false,
          allowOverwrite: false,
          tokenPayload: JSON.stringify({
            session_id: verified.upload_id,
            storage_key: verified.storage_key,
            user_id: verified.user_id,
          }),
        };
      },
      onUploadCompleted: async () => {
        // Finalization, magic-byte inspection, streaming SHA-256, and atomic reservation commit
        // are executed by POST /api/v1/uploads/{session_id}/complete in the backend worker.
      },
    });

    return NextResponse.json(jsonResponse);
  } catch (error) {
    return NextResponse.json(
      { error: (error as Error).message || "Vercel Blob handleUpload failed" },
      { status: 400 }
    );
  }
}
