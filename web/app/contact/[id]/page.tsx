import Link from "next/link";
import { notFound } from "next/navigation";
import { fetchEmailDetail } from "@/lib/emails";
import ContactDetail from "@/components/ContactDetail";
import ThemeToggle from "@/components/ThemeToggle";
import Toaster from "@/components/Toaster";

export default async function ContactPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const record = await fetchEmailDetail(decodeURIComponent(id));
  if (!record) notFound();

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link href="/" className="brand brand-link">
            <div className="logo">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2 2 7l10 5 10-5-10-5Z" />
                <path d="m2 17 10 5 10-5" />
                <path d="m2 12 10 5 10-5" />
              </svg>
            </div>
            <div>
              <h1>Contact Directory</h1>
              <p>Contact detail</p>
            </div>
          </Link>
          <ThemeToggle />
        </div>
      </header>

      <main className="wrap">
        <Link href="/" className="detail-back">‹ Back to list</Link>
        <ContactDetail record={record} />
      </main>

      <Toaster />
    </>
  );
}
