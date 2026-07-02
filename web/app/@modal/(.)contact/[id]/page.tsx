import { fetchEmailDetail } from "@/lib/emails";
import ContactDetail from "@/components/ContactDetail";
import DetailDrawer from "@/components/DetailDrawer";

/**
 * Intercepted route: when navigating to /contact/[id] from within the app,
 * render the detail as a right-side slide-over instead of a full page.
 */
export default async function ContactModal({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const record = await fetchEmailDetail(decodeURIComponent(id));

  return (
    <DetailDrawer>
      {record ? (
        <ContactDetail record={record} />
      ) : (
        <div className="empty">Contact not found.</div>
      )}
    </DetailDrawer>
  );
}
