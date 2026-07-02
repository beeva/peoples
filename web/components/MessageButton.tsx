"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

type Status = { text: string; state: "" | "running" | "done" | "error" };

const TONES = [
  "friendly and professional",
  "casual",
  "formal",
  "enthusiastic",
];

// Preset goals for the email — pick any combination, then refine in the box.
const INTENT_PRESETS: { label: string; phrase: string }[] = [
  { label: "Introduce myself", phrase: "Briefly introduce myself and what I do." },
  { label: "Introduce our service", phrase: "Introduce our three.js consulting service." },
  { label: "Invite to a call", phrase: "Invite them to a short call." },
  { label: "Ask about their work", phrase: "Ask about their current project or work." },
  { label: "Propose collaboration", phrase: "Propose collaborating together." },
  { label: "Compliment their work", phrase: "Compliment their recent work." },
];

export default function MessageButton({
  id,
  to,
  name,
  variant = "card",
}: {
  id: string;
  to: string;
  name: string;
  variant?: "card" | "primary";
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [recipient, setRecipient] = useState(to);
  const [intent, setIntent] = useState("");
  const [presets, setPresets] = useState<Set<string>>(new Set());
  const [tone, setTone] = useState(TONES[0]);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [status, setStatus] = useState<Status>({ text: "", state: "" });
  const [generating, setGenerating] = useState(false);
  const [sending, setSending] = useState(false);

  function openModal() {
    setRecipient(to);
    setIntent("");
    setPresets(new Set());
    setSubject("");
    setBody("");
    setStatus({ text: "", state: "" });
    setOpen(true);
  }

  function togglePreset(label: string) {
    setPresets((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  /** Selected preset phrases + the free-text box, combined into one brief. */
  function composedIntent(): string {
    return [
      ...INTENT_PRESETS.filter((p) => presets.has(p.label)).map((p) => p.phrase),
      intent.trim(),
    ]
      .filter(Boolean)
      .join(" ");
  }

  async function generate() {
    setGenerating(true);
    setStatus({ text: "Generating with Claude…", state: "running" });
    try {
      const res = await fetch("/api/message/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id, intent: composedIntent(), tone }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "generation failed");
      setSubject(data.subject || "");
      setBody(data.body || "");
      setStatus({ text: "Draft ready — review and edit before sending.", state: "done" });
    } catch (err) {
      setStatus({ text: `Error: ${err instanceof Error ? err.message : String(err)}`, state: "error" });
    } finally {
      setGenerating(false);
    }
  }

  async function send() {
    if (!recipient.trim()) {
      setStatus({ text: "Enter a recipient address.", state: "error" });
      return;
    }
    if (!body.trim()) {
      setStatus({ text: "Write or generate a message first.", state: "error" });
      return;
    }
    setSending(true);
    setStatus({ text: "Sending…", state: "running" });
    try {
      const res = await fetch("/api/message/send", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id, to: recipient.trim(), subject, body }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "send failed");
      setStatus({ text: `✓ Sent to ${data.to}`, state: "done" });
      window.dispatchEvent(new CustomEvent("toast", { detail: `Email sent to ${data.to}` }));
      // Re-fetch the server components so the "Sent" badge shows up right away.
      router.refresh();
      setTimeout(() => setOpen(false), 1200);
    } catch (err) {
      setStatus({ text: `Error: ${err instanceof Error ? err.message : String(err)}`, state: "error" });
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <button
        className={`msg-btn${variant === "primary" ? " primary" : " icon"}`}
        onClick={openModal}
        title={`Send a message to ${to}`}
        aria-label={`Send a message to ${name}`}
      >
        {variant === "primary" ? (
          <>✉ Send message</>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="18" height="14" rx="2" />
            <path d="m3 7 9 6 9-6" />
          </svg>
        )}
      </button>

      {open && (
        <div
          className="modal-overlay"
          onClick={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-label={`Message ${name}`}>
            <div className="modal-head">
              <h2>Message {name}</h2>
              <button className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="modal-body">
              <label className="fld">
                <span>To</span>
                <input type="email" value={recipient} onChange={(e) => setRecipient(e.target.value)} placeholder="name@example.com" />
              </label>

              <div className="fld">
                <span>What should the email say?</span>
                <div className="intent-presets">
                  {INTENT_PRESETS.map((p) => {
                    const on = presets.has(p.label);
                    return (
                      <label key={p.label} className={`check-pill${on ? " on" : ""}`}>
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={() => togglePreset(p.label)}
                        />
                        <span>{p.label}</span>
                      </label>
                    );
                  })}
                </div>
                <textarea
                  rows={3}
                  value={intent}
                  onChange={(e) => setIntent(e.target.value)}
                  placeholder="Add any extra details (optional)…"
                />
              </div>

              <div className="fld-row">
                <label className="fld">
                  <span>Tone</span>
                  <select value={tone} onChange={(e) => setTone(e.target.value)}>
                    {TONES.map((t) => (
                      <option key={t} value={t}>
                        {t.charAt(0).toUpperCase() + t.slice(1)}
                      </option>
                    ))}
                  </select>
                </label>
                <button className="gen-btn" onClick={generate} disabled={generating}>
                  {generating ? "Generating…" : "✨ Generate with Claude"}
                </button>
              </div>

              <label className="fld">
                <span>Subject</span>
                <input type="text" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Subject line" />
              </label>

              <label className="fld">
                <span>Message — preview &amp; edit before sending</span>
                <textarea
                  rows={9}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  placeholder="Write here, or generate a draft above."
                />
              </label>

              <div className="msg-status" data-state={status.state}>
                {status.text}
              </div>
            </div>

            <div className="modal-foot">
              <button className="btn-secondary" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button className="btn-primary" onClick={send} disabled={sending}>
                {sending ? "Sending…" : "Send email"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
