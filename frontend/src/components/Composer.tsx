import { useRef, useState, type KeyboardEvent } from "react";

/**
 * Auto-growing message box. Enter sends, Shift+Enter makes a new line.
 *
 * The send button locks while a message is in flight. Enter is easy to hit
 * twice on a slow connection, and each press was queueing another request -
 * two identical messages arriving at the customer, seconds apart.
 */
export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => Promise<void> | void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  const empty = text.trim().length === 0;
  const blocked = disabled || busy;

  async function submit() {
    const trimmed = text.trim();
    if (!trimmed || blocked) return;

    setBusy(true);
    // Cleared immediately: the message is drawn optimistically upstream, so
    // leaving it in the box would show it twice.
    setText("");
    if (ref.current) ref.current.style.height = "auto";
    try {
      await onSend(trimmed);
    } finally {
      setBusy(false);
      ref.current?.focus();
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="composer">
      <textarea
        ref={ref}
        rows={1}
        value={text}
        disabled={disabled}
        placeholder={disabled ? "Take over the conversation to reply" : "Type a message"}
        onChange={(e) => {
          setText(e.target.value);
          const el = e.target;
          el.style.height = "auto";
          el.style.height = `${Math.min(el.scrollHeight, 132)}px`;
        }}
        onKeyDown={onKeyDown}
      />
      <button
        className={`btn give send${busy ? " busy" : ""}`}
        type="button"
        disabled={blocked || empty}
        aria-busy={busy}
        onClick={submit}
      >
        {busy ? (
          <span className="spin dark" />
        ) : (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m4 12 15-8-6 8 6 8z" />
          </svg>
        )}
        <span className="label">{busy ? "Sending" : "Send"}</span>
      </button>
    </div>
  );
}
