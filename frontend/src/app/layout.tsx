import type { Metadata } from "next";
import "./globals.css";
import { StudioProvider } from "@/components/StudioContext";
import { Header } from "@/components/Header";

export const metadata: Metadata = {
  title: "Tamil English Document Studio",
  description: "Extract Tamil and English text, edit it, and download in your preferred format.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Noto+Sans+Tamil:wght@400;500;600;700&family=Noto+Sans:wght@400;500;600;700&display=swap"
        />
      </head>
      <body className="min-h-screen font-sans antialiased bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <StudioProvider>
          <Header />
          <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:py-8">{children}</main>
        </StudioProvider>
      </body>
    </html>
  );
}
