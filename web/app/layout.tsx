import type { Metadata } from "next";
import { Inter } from "next/font/google";
import Sidebar from "@/components/Sidebar";
import "./globals.css";

// Inter with tabular numerals — the workhorse UI face; the `--font-sans`
// variable is consumed by `body` in globals.css, with a system fallback.
const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});

export const metadata: Metadata = {
  title: "Contact Directory",
  description: "Browse and reach the contacts your scrapers have found.",
};

// Set the theme before paint to avoid a flash of the wrong theme.
const themeScript = `
(function () {
  try {
    var t = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
})();
`;

export default function RootLayout({
  children,
  modal,
}: {
  children: React.ReactNode;
  modal: React.ReactNode;
}) {
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        {/* App shell: fixed 240px nav rail + fluid content column. Overlays
            (drawer/modals) are position:fixed, so they can stay outside. */}
        <div className="app-shell">
          <Sidebar />
          <div className="app-content">{children}</div>
        </div>
        {modal}
      </body>
    </html>
  );
}
