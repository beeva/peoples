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

/** "Mar 2011" — the month an account was opened. "" if the date is unusable. */
export function joinedDisplay(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

/** "14 yrs" / "8 mo" — how old an account is, in the coarsest useful unit.
 *
 * Months rather than days because that is the granularity anyone reads it at:
 * what matters for outreach is "been here for years" vs "signed up in spring".
 * A future date (clock skew, a bad record) yields "" rather than a negative age.
 */
export function accountAge(iso: string, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  let months =
    (now.getUTCFullYear() - then.getUTCFullYear()) * 12 +
    (now.getUTCMonth() - then.getUTCMonth());
  if (now.getUTCDate() < then.getUTCDate()) months -= 1; // the month isn't up yet
  if (months < 0) return "";
  if (months < 12) return `${months} mo`;
  const years = Math.floor(months / 12);
  return `${years} yr${years === 1 ? "" : "s"}`;
}

/** "Joined Mar 3, 2011 · 14 yrs on GitHub" — the long form, for a tooltip. */
export function joinedLabel(iso: string, source = "GitHub"): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const full = d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const age = accountAge(iso);
  return age ? `Joined ${full} · ${age} on ${source}` : `Joined ${full}`;
}

/** A "Sent Jun 19, 2026" tooltip for a contact we've emailed (count-aware). */
export function sentLabel(messagedAt: string, count: number, manual = false): string {
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
  if (manual) return when ? `Marked as sent on ${when}` : "Marked as sent";
  const times = count > 1 ? ` (${count}×)` : "";
  return when ? `Last emailed ${when}${times}` : `Email sent${times}`;
}
