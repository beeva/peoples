/** Contact Directory Design System v1.0 — color tokens.
 *
 *  Source of truth at runtime is the CSS custom properties in
 *  `web/app/globals.css` (`:root` / `[data-theme="light"]`); these TS mirrors
 *  exist for charts, emails, and any code that can't read CSS variables.
 */

export const primary = {
  500: "#7C5CFF", // --accent
  600: "#6546E8", // --accent-strong
  700: "#5234C7", // --accent-deep
} as const;

export const dark = {
  background: "#080A10", // --bg
  surface: "#111520", // --surface
  card: "#161B27", // --surface-2
  border: "#252B3A", // --border
  text: "#EDF0F6", // --text
  textDim: "#9BA3B4", // --text-dim
  textFaint: "#656D7E", // --text-faint
} as const;

export const semantic = {
  success: "#22C55E", // --good
  warning: "#F59E0B", // --warn
  error: "#EF4444", // --error
  info: "#3B82F6", // --info
} as const;
