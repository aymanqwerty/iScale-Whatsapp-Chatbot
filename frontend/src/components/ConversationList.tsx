import type { Conversation } from "../lib/api";
import { avatarStyle, initials, prettyPhone, timeAgo } from "../lib/format";

/** Shimmer placeholders for the first load. Repeat visits paint from state. */
function Skeleton() {
  return (
    <>
      {Array.from({ length: 7 }, (_, i) => (
        <div className="skrow" key={i}>
          <div className="sk circle" style={{ width: 40, height: 40, flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div className="sk" style={{ height: 11, width: `${48 + ((i * 7) % 26)}%` }} />
            <div className="sk" style={{ height: 9, width: "38%", marginTop: 7 }} />
            <div className="sk" style={{ height: 9, width: `${62 + ((i * 5) % 24)}%`, marginTop: 7 }} />
          </div>
        </div>
      ))}
    </>
  );
}

interface Props {
  conversations: Conversation[];
  loading: boolean;
  current: string | null;
  query: string;
  onSelect: (phone: string) => void;
  onMenu: (conversation: Conversation, x: number, y: number) => void;
}

export function ConversationList({
  conversations,
  loading,
  current,
  query,
  onSelect,
  onMenu,
}: Props) {
  if (loading) return <div className="list"><Skeleton /></div>;

  const needle = query.trim().toLowerCase();
  const rows = conversations.filter(
    (c) =>
      !needle ||
      c.phone.includes(needle) ||
      c.name.toLowerCase().includes(needle),
  );

  if (rows.length === 0) {
    return (
      <div className="list">
        <div
          style={{
            padding: "34px 18px",
            textAlign: "center",
            color: "var(--faint)",
            fontSize: 13,
          }}
        >
          {needle ? "No matches." : "No conversations yet."}
        </div>
      </div>
    );
  }

  return (
    <div className="list">
      {rows.map((c) => (
        <div
          key={c.phone}
          className={`row${c.phone === current ? " active" : ""}${c.blocked ? " blocked" : ""}${c.pinned ? " pinned" : ""}`}
          role="button"
          tabIndex={0}
          onClick={() => onSelect(c.phone)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(c.phone);
            }
          }}
        >
          <div className="av" style={avatarStyle(c.phone)}>
            {initials(c.name, c.phone)}
          </div>
          <div className="rbody">
            <div className="rtop">
              <span className="rname">
                {c.name || "Unknown"}
                {/* Marks a conversation a human has taken over, so nobody
                    wonders why the bot has gone quiet on it. A blocked contact
                    is quiet for a different reason, and says so instead - the
                    two must never be mistaken for each other. */}
                {c.blocked ? (
                  <span className="badge stop">BLOCKED</span>
                ) : (
                  c.bot_paused && <span className="badge">YOU</span>
                )}
                {/* Money waiting to be checked outranks both of the above, so
                    it is shown as well as them, not instead of them. */}
                {c.payment_pending && <span className="badge pay">PAYMENT</span>}
              </span>
              <span className="rtime">{timeAgo(c.last_activity)}</span>
            </div>
            {/* Pinned rows sit out of recency order, so they say why. Without
                this the top of the list just looks stale. */}
            {c.pinned && (
              <span className="pinmark" title="Pinned to the top">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 17v5" /><path d="M9 10.8V4h6v6.8l2 3.2H7l2-3.2Z" />
                </svg>
              </span>
            )}
            {/* Name and number both: staff recognise regulars by name, but the
                number is what they cross-check against the leads sheet. */}
            <div className="rnum">{prettyPhone(c.phone)}</div>
            <div className="rprev">
              {(c.last_sender === "USER" ? "" : "↩ ") + c.last_message}
            </div>
          </div>

          {/* Revealed on hover, like WhatsApp's own. Kept mounted rather than
              conditionally rendered so it is reachable by keyboard, and it
              stops the click so opening the menu does not also open the
              thread. Coordinates come from the chevron itself - the menu is
              positioned fixed to escape this list's overflow clipping. */}
          <button
            type="button"
            className="rowchev"
            aria-label={`Options for ${c.name || c.phone}`}
            onClick={(event) => {
              event.stopPropagation();
              const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
              onMenu(c, box.left - 150, box.bottom + 6);
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="m6 9 6 6 6-6" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
