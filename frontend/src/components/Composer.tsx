import { useRef, useState, type KeyboardEvent } from "react";

/** Auto-growing message box. Enter sends, Shift+Enter makes a new line. */
export function Composer({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    // Cleared immediately: the message is drawn optimistically upstream, so
    // leaving it in the box would show it twice.
    setText("");
    if (ref.current) ref.current.style.height = "auto";
    onSend(trimmed);
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
        placeholder="Type a message"
        onChange={(e) => {
          setText(e.target.value);
          const el = e.target;
          el.style.height = "auto";
          el.style.height = `${Math.min(el.scrollHeight, 132)}px`;
        }}
        onKeyDown={onKeyDown}
      />
      <button className="btn give" type="button" disabled={disabled} onClick={submit}>
        Send
      </button>
    </div>
  );
}
