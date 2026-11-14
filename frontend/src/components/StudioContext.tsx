"use client";

import React, { createContext, useContext, useEffect, useState } from "react";
import { Locale } from "@/lib/types";

interface StudioContextValue {
  locale: Locale;
  setLocale: (loc: Locale) => void;
  theme: "light" | "dark";
  toggleTheme: () => void;
}

const StudioContext = createContext<StudioContextValue>({
  locale: "en",
  setLocale: () => {},
  theme: "light",
  toggleTheme: () => {},
});

export function StudioProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>("en");
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    const savedLoc = localStorage.getItem("teds_locale") as Locale | null;
    if (savedLoc === "en" || savedLoc === "ta") setLocaleState(savedLoc);
    const savedTheme = localStorage.getItem("teds_theme") as "light" | "dark" | null;
    if (savedTheme === "dark") {
      setTheme("dark");
      document.documentElement.classList.add("dark");
    }
  }, []);

  const setLocale = (loc: Locale) => {
    setLocaleState(loc);
    localStorage.setItem("teds_locale", loc);
  };

  const toggleTheme = () => {
    setTheme((prev) => {
      const next = prev === "light" ? "dark" : "light";
      localStorage.setItem("teds_theme", next);
      if (next === "dark") document.documentElement.classList.add("dark");
      else document.documentElement.classList.remove("dark");
      return next;
    });
  };

  return (
    <StudioContext.Provider value={{ locale, setLocale, theme, toggleTheme }}>
      {children}
    </StudioContext.Provider>
  );
}

export function useStudio() {
  return useContext(StudioContext);
}
