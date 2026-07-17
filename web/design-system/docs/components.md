# Contact Directory Design System v1.0 — component map

The design system is implemented as CSS custom properties + semantic classes
in `web/app/globals.css`. TS token mirrors live in `../tokens/`. This file
maps each spec component to where it lives in the codebase.

## Tokens

| Token group | CSS variables | TS mirror |
|---|---|---|
| Primary 500/600/700 | `--accent`, `--accent-strong`, `--accent-deep` | `tokens/colors.ts` |
| Background / Surface / Card / Border | `--bg`, `--surface`, `--surface-2`, `--border` | `tokens/colors.ts` |
| Semantic (success/warning/error/info) | `--good`, `--warn`, `--error`, `--info` | `tokens/colors.ts` |
| Radius 6/10/16/pill | `--radius-sm`, `--radius`, `--radius-lg`, `--radius-pill` | `tokens/spacing.ts` |
| Elevation 1/2/3 | `--shadow-sm/md/lg` | `tokens/spacing.ts` |
| Type scale (Inter) | `next/font` in `app/layout.tsx` → `--font-sans` | `tokens/typography.ts` |

Both themes are supported: dark is the default `:root`, light overrides via
`:root[data-theme="light"]` (toggled by `components/ThemeToggle.tsx`).

## Layout (§7)

- **App shell** — `app/layout.tsx`: `.app-shell` = `Sidebar` (240px, sticky)
  + `.app-content`. Collapses to a 64px icon rail ≤1024px.
- **Sidebar** — `components/Sidebar.tsx`. Live sections: Contacts, Slack
  Users. Spec sections without a feature yet (Dashboard, Tags, Campaigns,
  Imports, Exports, Settings) render disabled with a "Soon" chip.
- **Top navigation** — `.topbar` per page: page title + subtitle + actions.

## Components (§8–13)

| Spec component | Implementation |
|---|---|
| Button / Primary | `.btn-primary`, `.rescrape-btn` (40px height) |
| Button / Secondary | `.btn-secondary`, `.export-btn` |
| Button / Danger | `.stop-btn` (uses `--error`) |
| Search input | `SearchControls.tsx` → `.search` (icon left, 10px radius) |
| Dropdown | `.sort select`, `.facet-op` |
| Tags / badges | `.check-pill`, `.src-badge`, `.run-badge`, `.age-badge` |
| Contact table | `EmailTable.tsx` (sort, filter, pagination, sent toggle) |
| Tabs | `CategoryTabs.tsx` / Slack workspace tabs → `.tabs .tab` |
| Toast | `Toaster.tsx` (listens to `toast` CustomEvents) |
| Modal | `.modal-overlay > .modal` (`MessageButton`, `ExportButton`) |
| Drawer | `app/@modal/(.)contact/[id]` → `DetailDrawer.tsx` (right side) |
| Empty state | `.empty` blocks on both list pages |
| Error state | `.banner` (API down), `.msg-status[data-state="error"]` |
| Loading | preview `aria-busy`, `.spin-mini-inline` spinners |

## Responsive (§15)

- Desktop 1440+: full layout.
- ≤1024px: sidebar → icon rail (labels hidden, tooltips remain).
- <768px: existing table/card breakpoints in `globals.css` continue to apply.
