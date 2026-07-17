/** Contact Directory Design System v1.0 — spacing & shape tokens (8px grid). */

export const spacing = [4, 8, 12, 16, 24, 32, 48, 64] as const;

/** Named anchors used throughout the app. */
export const layout = {
  cardPadding: 24,
  sectionGap: 32,
  buttonHeight: 40,
  inputHeight: 40,
  sidebarWidth: 240,
  sidebarRailWidth: 64, // icon rail below the tablet breakpoint
} as const;

export const radius = {
  sm: 6, // --radius-sm
  md: 10, // --radius
  lg: 16, // --radius-lg
  pill: 999, // --radius-pill
} as const;

/** Elevation: 0 flat / 1 cards / 2 dropdowns / 3 modal & drawer. */
export const shadow = {
  1: "0 4px 12px rgba(0,0,0,.25)", // --shadow-md
  2: "0 6px 20px rgba(0,0,0,.30)",
  3: "0 18px 50px rgba(0,0,0,.50)", // --shadow-lg
} as const;

export const breakpoints = {
  mobile: 768, // <768px
  tablet: 1440, // 768–1439px; desktop is 1440+
} as const;
