import { useEffect, useRef } from "react";
import type { Message } from "../lib/api";
import { clock, dayLabel } from "../lib/format";

function Skeleton() {
  return (
    <>
      {[46, 62, 38, 70, 52].map((width, i) => (
        <div
          key={i}
          className="sk"
          style={{
            height: 34 + (i % 2) * 16,
            width: `${width}%`,
            borderRadius: 14,
            marginBottom: 9,
            alignSelf: i % 2 ? "flex-end" : "flex-start",
          }}
        />
      ))}
    </>
  );
}

function Bubble({ message }: { message: Message }) {
  const classes = [
    "msg",
    message.sender,
    message.pending ? "pending" : "",
    message.failed ? "failed" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes}>
      {message.sender === "AGENT" && <span className="who">You</span>}
      {/* React escapes this by default. A customer can put anything in a
          WhatsApp message, and this is where staff read it. */}
      {message.text}
      <span className="meta">
        {message.failed
          ? "Not delivered"
          : message.pending
            ? "Sending…"
            : clock(message.at)}
      </span>
    </div>
  );
}

export function Thread({
  messages,
  loading,
}: {
  messages: Message[];
  loading: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Follow new messages, but only when the reader is already at the bottom -
  // yanking the view down while someone scrolls back through history is worse
  // than making them scroll once.
  const pinned = useRef(true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (pinned.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  function onScroll() {
    const el = ref.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  if (loading) {
    return (
      <div className="thread" ref={ref}>
        <Skeleton />
      </div>
    );
  }

  let lastDay = "";
  return (
    <div className="thread" ref={ref} onScroll={onScroll}>
      {messages.map((message) => {
        const day = message.at ? dayLabel(message.at) : "";
        const separator = day && day !== lastDay ? day : "";
        if (separator) lastDay = day;
        return (
          <div key={message.id} style={{ display: "contents" }}>
            {separator && <div className="daysep">{separator}</div>}
            <Bubble message={message} />
          </div>
        );
      })}
    </div>
  );
}

export function EmptyThread() {
  return (
    <div className="thread">
      <div className="empty">
        <div className="big">
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="#5c6673"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.9-.9L3 20.5l1.6-4.9A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4z" />
          </svg>
        </div>
        <h3>No conversation selected</h3>
        <p>Pick a number on the left to read the thread and take it over.</p>
      </div>
    </div>
  );
}
