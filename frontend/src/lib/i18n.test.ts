import { describe, expect, it } from "vitest";
import { t, TRANSLATIONS } from "./i18n";
import { CHUNK_SIZE_BYTES, MAX_UPLOAD_BYTES } from "./types";

describe("Tamil English Document Studio frontend core", () => {
  it("enforces exact 400 MB (400,000,000 bytes) upload limit", () => {
    expect(MAX_UPLOAD_BYTES).toBe(400_000_000);
    expect(CHUNK_SIZE_BYTES).toBe(5_242_880);
  });

  it("provides matching English and Tamil translations for all keys", () => {
    const enKeys = Object.keys(TRANSLATIONS.en) as Array<keyof typeof TRANSLATIONS.en>;
    for (const k of enKeys) {
      expect(t("en", k).length).toBeGreaterThan(0);
      expect(t("ta", k).length).toBeGreaterThan(0);
    }
    expect(t("en", "heroTitle")).toBe("Convert documents. Keep your words.");
  });
});
