import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  SessionExpired,
  type Conversation,
  type Message,
  type Thread as ThreadData,
} from "../lib/api";
import { avatarStyle, initials, prettyPhone } from "../lib/format";
import { useInterval, useLiveUpdates } from "../lib/useLiveUpdates";
import { ConversationList } from "./ConversationList";
import { Composer } from "./Composer";
import { EmptyThread, Thread } from "./Thread";

/** One cached thread. Re-opening paints from here instantly while the delta
 *  fetch runs behind it, so the pane is never blank while the network works. */
interface Cached {
  messages: Message[];
  lastId: number;
  seen: Set<number>;
}

export function Inbox({ onSignedOut }: { onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [query, setQuery] = useState("");

  const [current, setCurrent] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [header, setHeader] = useState({ name: "", phone: "" });
  const [paused, setPaused] = useState(false);
  const [canReply, setCanReply] = useState(false);
  const [toast, setToast] = useState("");

  const cache = useRef(new Map<string, Cached>());
  // Guards against two fetches for the same thread overlapping: the second
  // would leave with a stale `after_id` and re-deliver messages already drawn.
  const fetching = useRef(false);
  const currentRef = useRef<string | null>(null);
  currentRef.current = current;

  // Bumped every time the handover is changed. A poll that was already in
  // flight when the button was pressed carries the OLD `bot_paused`, and
  // applying it flipped the button back a moment after the user clicked - then
  // forward again on the next poll. Comparing this counter across the await
  // lets a stale response be recognised and its handover value discarded.
  const handoverSeq = useRef(0);
  const [handoverBusy, setHandoverBusy] = useState(false);

  const notify = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 3200);
  }, []);

  const guard = useCallback(
    (err: unknown) => {
      if (err instanceof SessionExpired) onSignedOut();
      return err;
    },
    [onSignedOut],
  );

  function slot(phone: string): Cached {
    let entry = cache.current.get(phone);
    if (!entry) {
      entry = { messages: [], lastId: 0, seen: new Set() };
      cache.current.set(phone, entry);
    }
    return entry;
  }

  /* ---------------- conversations ---------------- */
  const loadConversations = useCallback(async () => {
    try {
      const body = await api.conversations();
      setConversations(body.conversations);
      setListLoading(false);
    } catch (err) {
      guard(err);
    }
  }, [guard]);

  /* ---------------- one thread ---------------- */
  const refresh = useCallback(
    async (phone: string, full: boolean) => {
      if (fetching.current && !full) return;
      fetching.current = true;
      const seq = handoverSeq.current;
      try {
        const entry = slot(phone);
        const data: ThreadData = await api.messages(
          phone,
          full ? 0 : entry.lastId,
        );
        // The user may have clicked another conversation mid-flight.
        if (currentRef.current !== phone) return;

        if (full) {
          entry.messages = [];
          entry.seen = new Set();
          entry.lastId = 0;
        }
        let added = 0;
        for (const message of data.messages) {
          if (entry.seen.has(message.id)) continue; // never draw one twice
          entry.seen.add(message.id);
          entry.messages.push(message);
          entry.lastId = Math.max(entry.lastId, message.id);
          added++;
        }

        // Only trust this response's handover value if nothing changed it while
        // the request was in flight. Messages are always applied - they cannot
        // be stale, only incomplete.
        if (handoverSeq.current === seq) setPaused(data.bot_paused);
        setCanReply(data.can_reply);
        setHeader({ name: data.name, phone: data.phone });
        if (full || added) setMessages([...entry.messages]);
        setThreadLoading(false);
      } catch (err) {
        guard(err);
      } finally {
        fetching.current = false;
      }
    },
    [guard],
  );

  const open = useCallback(
    async (phone: string) => {
      setCurrent(phone);
      currentRef.current = phone;
      const entry = slot(phone);
      const row = conversations.find((c) => c.phone === phone);
      setHeader({ name: row?.name ?? "", phone });
      if (row) setPaused(row.bot_paused);

      if (entry.messages.length) {
        setMessages([...entry.messages]); // instant, from cache
        setThreadLoading(false);
      } else {
        setMessages([]);
        setThreadLoading(true);
      }
      await refresh(phone, true);
    },
    [conversations, refresh],
  );

  /* ---------------- handover, applied optimistically ---------------- */
  async function toggleHandover() {
    // Locked while in flight. Without this, a second click queued a contrary
    // request and whichever finished last won - so the button could settle on
    // the opposite of what was asked for.
    if (!current || handoverBusy) return;
    const phone = current;
    const want = !paused;
    const seq = ++handoverSeq.current;

    // Flip first, confirm after. Waiting on a POST plus two follow-up fetches
    // before anything moved is what made this button feel broken.
    setHandoverBusy(true);
    setPaused(want);
    setConversations((rows) =>
      rows.map((c) => (c.phone === phone ? { ...c, bot_paused: want } : c)),
    );

    try {
      const result = await api.handover(phone, want);
      // The server's answer is the truth, but only while it is still the most
      // recent intent - a slower earlier request must not undo a later click.
      if (handoverSeq.current === seq) {
        setPaused(result.bot_paused);
        setConversations((rows) =>
          rows.map((c) =>
            c.phone === phone ? { ...c, bot_paused: result.bot_paused } : c,
          ),
        );
      }
    } catch (err) {
      if (handoverSeq.current === seq) {
        setPaused(!want);
        setConversations((rows) =>
          rows.map((c) => (c.phone === phone ? { ...c, bot_paused: !want } : c)),
        );
        notify("Could not change the handover. Try again.");
      }
      guard(err);
    } finally {
      if (handoverSeq.current === seq) setHandoverBusy(false);
    }
  }

  /* ---------------- sending, applied optimistically ---------------- */
  const tempId = useRef(-1);

  async function send(text: string) {
    if (!current) return;
    const phone = current;
    const entry = slot(phone);

    const optimistic: Message = {
      id: tempId.current--,
      sender: "AGENT",
      text,
      at: new Date().toISOString(),
      pending: true,
    };
    entry.messages.push(optimistic);
    setMessages([...entry.messages]);

    try {
      const saved = await api.send(phone, text);
      optimistic.pending = false;
      entry.seen.add(saved.id);
      entry.lastId = Math.max(entry.lastId, saved.id);
      optimistic.id = saved.id;
      if (currentRef.current === phone) setMessages([...entry.messages]);
      loadConversations();
    } catch (err) {
      optimistic.pending = false;
      optimistic.failed = true;
      if (currentRef.current === phone) setMessages([...entry.messages]);
      guard(err);
      notify(err instanceof Error ? err.message : "Could not send.");
    }
  }

  /* ---------------- live updates ---------------- */
  const connected = useLiveUpdates(
    useCallback(
      (phone: string) => {
        if (phone === currentRef.current) refresh(phone, false);
        loadConversations();
      },
      [refresh, loadConversations],
    ),
  );

  useEffect(() => {
    loadConversations();
  }, [loadConversations]);

  // Fallback polling stays on even while the socket is healthy, so a silently
  // dropped connection costs a few seconds of latency rather than a dead page.
  useInterval(() => {
    if (currentRef.current) refresh(currentRef.current, false);
  }, 4000);
  useInterval(() => loadConversations(), 20000);
  // Keeps the relative timestamps in the list honest.
  useInterval(() => setConversations((rows) => [...rows]), 60000);

  async function signOut() {
    await api.logout();
    onSignedOut();
  }

  const note = !paused
    ? "The bot is handling this conversation. Take it over to reply yourself."
    : !canReply
      ? "Outside the 24-hour window — WhatsApp will not deliver a reply until the customer messages again."
      : "You are replying. The bot stays silent until you hand it back.";

  return (
    <div className={`shell${current ? " viewing" : ""}`}>
      <aside>
        <div className="brand">
          <div className="mark">iS</div>
          <div className="t">
            <b>Console</b>
            <span>
              <i className={`dot${connected ? "" : " off"}`} />
              {connected ? "Live" : "Reconnecting…"}
            </span>
          </div>
          <button className="icon" type="button" onClick={signOut} title="Sign out">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
          </button>
        </div>

        <div className="searchwrap">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2.2" strokeLinecap="round">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.2-3.2" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search name or number"
            spellCheck={false}
          />
        </div>

        <ConversationList
          conversations={conversations}
          loading={listLoading}
          current={current}
          query={query}
          onSelect={open}
        />
      </aside>

      <main>
        {current ? (
          <>
            <header>
              <button
                className="icon backbtn"
                type="button"
                onClick={() => setCurrent(null)}
                aria-label="Back to list"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m15 18-6-6 6-6" />
                </svg>
              </button>
              <div className="av" style={avatarStyle(header.phone || current)}>
                {initials(header.name, header.phone || current)}
              </div>
              <div>
                <div className="hname">{header.name || "Unknown"}</div>
                <div className="hsub">{prettyPhone(header.phone || current)}</div>
              </div>
              <div className="spacer" />
              <button
                className={`btn ${paused ? "give" : "take"}${handoverBusy ? " busy" : ""}`}
                type="button"
                onClick={toggleHandover}
                disabled={handoverBusy}
                aria-busy={handoverBusy}
              >
                {handoverBusy ? (
                  <span className="spin dark" />
                ) : paused ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 19V5" /><path d="m5 12 7-7 7 7" />
                  </svg>
                ) : (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 6H9a4 4 0 0 0 0 8h6a4 4 0 0 1 0 8H4" />
                  </svg>
                )}
                {handoverBusy
                  ? paused
                    ? "Taking over…"
                    : "Handing back…"
                  : paused
                    ? "Hand back to bot"
                    : "Take over from bot"}
              </button>
            </header>

            <Thread messages={messages} loading={threadLoading} />

            <footer>
              <div className={`note${!paused ? "" : canReply ? "" : " warn"}`}>{note}</div>
              <Composer disabled={!paused || !canReply} onSend={send} />
            </footer>
          </>
        ) : (
          <EmptyThread />
        )}
      </main>

      <div className={`toast${toast ? " show" : ""}`}>{toast}</div>
    </div>
  );
}
