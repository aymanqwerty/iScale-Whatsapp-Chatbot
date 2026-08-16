import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface RowMenuTarget {
  phone: string;
  name: string;
  pinned: boolean;
  /** Where the chevron was, so the menu opens against it. */
  x: number;
  y: number;
}

/**
 * The chevron menu on a conversation row.
 *
 * Rendered with `position: fixed` against coordinates captured from the
 * chevron, NOT absolutely inside the row. The conversation list is an
 * `overflow-y: auto` container, so an absolutely positioned menu is clipped by
 * it the moment it extends past a row - which for the last row in the list is
 * always. Fixed positioning escapes the scroll container entirely.
 */
export function RowMenu({
  target,
  onPin,
  onRename,
  onClose,
}: {
  target: RowMenuTarget;
  onPin: () => void;
  onRename: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: target.x, top: target.y });

  // Measured after paint, because the menu's height is not known until it
  // exists - and flipping it above the chevron is only needed when it would
  // otherwise run off the bottom of the window.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    const margin = 8;
    let { x: left, y: top } = target;
    if (left + box.width > window.innerWidth - margin) {
      left = window.innerWidth - box.width - margin;
    }
    if (top + box.height > window.innerHeight - margin) {
      top = target.y - box.height - 26; // flip above the chevron
    }
    setPos({ left: Math.max(margin, left), top: Math.max(margin, top) });
  }, [target]);

  // Closing on any outside interaction, on Escape, and on scroll. Scroll
  // matters: the menu is fixed while the row underneath it is not, so leaving
  // it open during a scroll would leave it pointing at a different contact.
  useEffect(() => {
    function onDown(event: MouseEvent) {
      if (!ref.current?.contains(event.target as Node)) onClose();
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onClose);
    document.querySelector(".list")?.addEventListener("scroll", onClose);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onClose);
      document.querySelector(".list")?.removeEventListener("scroll", onClose);
    };
  }, [onClose]);

  return (
    <div
      className="rowmenu"
      ref={ref}
      role="menu"
      style={{ left: pos.left, top: pos.top }}
    >
      <button type="button" role="menuitem" onClick={onPin}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 17v5" />
          <path d="M9 10.8V4h6v6.8l2 3.2H7l2-3.2Z" />
        </svg>
        {target.pinned ? "Unpin" : "Pin to top"}
      </button>
      <button type="button" role="menuitem" onClick={onRename}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 20h9" />
          <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
        </svg>
        Rename
      </button>
    </div>
  );
}

/**
 * Rename dialog.
 *
 * A real field rather than `window.prompt`, mostly so the placeholder can show
 * the customer's own name - that is the thing being overridden, and it is the
 * first question anyone renaming a contact asks.
 */
export function RenameDialog({
  phone,
  current,
  fallback,
  onSave,
  onClose,
}: {
  phone: string;
  current: string;
  fallback: string;
  onSave: (name: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(current);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
    input.current?.select();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="scrim" onMouseDown={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2>Rename contact</h2>
        <p className="sub">
          Shown here in the console only — {phone} still sees their own name
          from the bot.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSave(value.trim());
          }}
        >
          <input
            ref={input}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={fallback || "Name for this contact"}
            maxLength={120}
            spellCheck={false}
          />
          <div className="actions">
            {/* Only offered when there is something to clear - a disabled or
                no-op button is just another thing to read. */}
            {current && (
              <button type="button" className="ghost" onClick={() => onSave("")}>
                Clear
              </button>
            )}
            <div className="spacer" />
            <button type="button" className="ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary">
              Save
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
