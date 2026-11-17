import type { NextConfig } from "next";
import path from "path";

const isCloudDeploy = Boolean(
  process.env.VERCEL === "1" ||
  process.env.NETLIFY === "true" ||
  process.env.NETLIFY ||
  process.env.CI
);

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname, ".."),
  outputFileTracingExcludes: {
    "*": [
      "../backend/**",
      "../private_storage/**",
      "../**/*.db",
      "../**/*.sqlite*",
      "../**/*.pyc",
    ],
  },
  ...(isCloudDeploy ? {} : { output: "standalone" }),
  async rewrites() {
    const backendUrl = (
      process.env.BACKEND_URL ||
      process.env.NEXT_PUBLIC_BACKEND_URL ||
      "http://127.0.0.1:8000"
    ).replace(/\/+$/, "");
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
      {
        source: "/languages",
        destination: `${backendUrl}/languages`,
      },
      {
        source: "/extract",
        destination: `${backendUrl}/extract`,
      },
      {
        source: "/export",
        destination: `${backendUrl}/export`,
      },
    ];
  },
};

export default nextConfig;
