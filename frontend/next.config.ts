import type { NextConfig } from "next";
import path from "path";

const PLACEHOLDER_API_PATTERNS = [
  "example.com",
  "example.org",
  "example.net",
  ".example",
  "your-domain",
  "placeholder",
  "localhost",
  "127.0.0.1",
];

if (process.env.VERCEL === "1") {
  const apiUrl = (process.env.NEXT_PUBLIC_API_URL?.trim() || "/api/v1").replace(/\/+$/, "");
  if (!apiUrl) {
    throw new Error(
      "NEXT_PUBLIC_API_URL must be set to /api/v1 (same-origin service routing) or your HTTPS backend API URL before building on Vercel."
    );
  }
  if (apiUrl !== "/api/v1" && apiUrl !== "/api") {
    let parsed: URL;
    try {
      parsed = new URL(apiUrl);
    } catch {
      throw new Error(`NEXT_PUBLIC_API_URL is not a valid URL: "${apiUrl}"`);
    }
    if (parsed.protocol !== "https:") {
      throw new Error("NEXT_PUBLIC_API_URL must use HTTPS for Vercel deployments.");
    }
    const host = parsed.hostname.toLowerCase();
    if (PLACEHOLDER_API_PATTERNS.some((p) => host === p || host.includes(p))) {
      throw new Error(
        `NEXT_PUBLIC_API_URL cannot use a placeholder or local host ("${host}") on Vercel.`
      );
    }
  }
}

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname),
  outputFileTracingExcludes: {
    "*": [
      "../backend/**",
      "../private_storage/**",
      "../**/*.db",
      "../**/*.sqlite*",
      "../**/*.pyc",
    ],
  },
  ...(process.env.VERCEL === "1" ? {} : { output: "standalone" }),
};

export default nextConfig;
