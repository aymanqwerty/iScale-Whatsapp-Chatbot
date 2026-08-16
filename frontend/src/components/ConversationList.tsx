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
}

export function ConversationList({
  conversations,
  loading,
  current,
  query,
  onSelect,
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
        <button
          key={c.phone}
          type="button"
          className={`row${c.phone === current ? " active" : ""}${c.blocked ? " blocked" : ""}`}
          onClick={() => onSelect(c.phone)}
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
            {/* Name and number both: staff recognise regulars by name, but the
                number is what they cross-check against the leads sheet. */}
            <div className="rnum">{prettyPhone(c.phone)}</div>
            <div className="rprev">
              {(c.last_sender === "USER" ? "" : "↩ ") + c.last_message}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}
