/** Presentation helpers for the inferred country/gender fields. */

/** ISO 3166-1 alpha-2 code -> flag emoji (regional indicator letters). */
export function flagEmoji(code: string): string {
  const cc = (code || "").toUpperCase();
  if (!/^[A-Z]{2}$/.test(cc)) return "";
  return String.fromCodePoint(
    ...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65),
  );
}

/** A "🇮🇳 India" style string, or "" when country is unknown/empty. */
export function countryDisplay(country: string, countryCode: string): string {
  const name = country && country.toLowerCase() !== "unknown" ? country : "";
  if (!name) return "";
  const flag = flagEmoji(countryCode);
  return flag ? `${flag} ${name}` : `🌍 ${name}`;
}

/** "Male" / "Female" label (no icon), or "" when gender is unknown/empty. */
export function genderDisplay(gender: string): string {
  if (gender === "male") return "Male";
  if (gender === "female") return "Female";
  return "";
}

/** A "Sent Jun 19, 2026" tooltip for a contact we've emailed (count-aware). */
export function sentLabel(messagedAt: string, count: number): string {
  let when = "";
  if (messagedAt) {
    const d = new Date(messagedAt);
    if (!Number.isNaN(d.getTime())) {
      when = d.toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    }
  }
  const times = count > 1 ? ` (${count}×)` : "";
  return when ? `Last emailed ${when}${times}` : `Email sent${times}`;
}
