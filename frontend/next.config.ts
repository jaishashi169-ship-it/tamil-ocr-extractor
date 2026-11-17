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
    return [
      {
        source: "/api/v1/:path*",
        destination: "http://127.0.0.1:8000/api/v1/:path*",
      },
      {
        source: "/languages",
        destination: "http://127.0.0.1:8000/languages",
      },
      {
        source: "/extract",
        destination: "http://127.0.0.1:8000/extract",
      },
      {
        source: "/export",
        destination: "http://127.0.0.1:8000/export",
      },
    ];
  },
};

export default nextConfig;
