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
};

export default nextConfig;
