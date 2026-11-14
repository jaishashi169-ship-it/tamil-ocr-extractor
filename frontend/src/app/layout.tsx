import type { Metadata } from "next";
import { Noto_Sans, Noto_Sans_Tamil } from "next/font/google";
import "./globals.css";
import { StudioProvider } from "@/components/StudioContext";
import { Header } from "@/components/Header";

const notoSans = Noto_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-noto-sans",
});

const notoTamil = Noto_Sans_Tamil({
  subsets: ["tamil"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-noto-tamil",
});

export const metadata: Metadata = {
  title: "Tamil English Document Studio",
  description: "Extract Tamil and English text, edit it, and download in your preferred format.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${notoSans.variable} ${notoTamil.variable}`}>
      <body className="min-h-screen font-sans antialiased bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <StudioProvider>
          <Header />
          <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:py-8">{children}</main>
        </StudioProvider>
      </body>
    </html>
  );
}
