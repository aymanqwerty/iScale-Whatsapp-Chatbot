import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  api,
  SessionExpired,
  type Conversation,
  type Message,
  type Thread as ThreadData,
} from "../lib/api";
import { avatarStyle, initials, prettyPhone } from "../lib/format";
import {
  handoverReducer,
  initialHandover,
  initialToggle,
  isBusy,
  isPaused,
  toggleBusy,
  toggleReducer,
  toggleValue,
} from "../lib/handover";
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
  const [canReply, setCanReply] = useState(false);
  const [payment, setPayment] = useState(false);
  const [toast, setToast] = useState("");

  const cache = useRef(new Map<string, Cached>());
  // Guards against two fetches for the same thread overlapping: the second
  // would leave with a stale `after_id` and re-deliver messages already drawn.
  const fetching = useRef(false);
  const currentRef = useRef<string | null>(null);
  currentRef.current = current;

  // One owner for the handover state. While the user has an unconfirmed
  // intent, the server cannot write it - which makes the flip-back impossible
  // by construction rather than unlikely by timing. See lib/handover.ts.
  const [handover, dispatch] = useReducer(handoverReducer, initialHandover);
  const paused = isPaused(handover);
  const handoverBusy = isBusy(handover);
  // Read inside async callbacks, where `handover` itself would be captured
  // stale by the closure.
  const handoverRef = useRef(handover);
  handoverRef.current = handover;

  // Blocking is a second boolean with the same two-writer race, so it runs on
  // the same machine. See the note at the foot of lib/handover.ts.
  const [block, dispatchBlock] = useReducer(toggleReducer, initialToggle);
  const blocked = toggleValue(block);
  const blockBusy = toggleBusy(block);
  const blockRef = useRef(block);
  blockRef.current = block;

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
      // Both epochs are read before the request leaves, so a response that
      // crosses a click can be recognised as older than it.
      const epoch = handoverRef.current.epoch;
      const blockEpoch = blockRef.current.epoch;
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

        // The reducer decides whether this is still current. Messages are
        // always applied - they cannot be stale, only incomplete.
        dispatch({ type: "observed", paused: data.bot_paused, epoch });
        dispatchBlock({
          type: "observed",
          paused: data.blocked,
          epoch: blockEpoch,
        });
        setCanReply(data.can_reply);
        setPayment(data.payment_pending);
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
      dispatch({ type: "reset", paused: row?.bot_paused ?? false });
      dispatchBlock({ type: "reset", paused: row?.blocked ?? false });
      setPayment(row?.payment_pending ?? false);

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
    if (!current || isBusy(handoverRef.current)) return;
    const phone = current;
    const want = !isPaused(handoverRef.current);

    // Displayed immediately; the reducer refuses every server update until this
    // settles, so nothing can revert it in between.
    dispatch({ type: "click" });
    setConversations((rows) =>
      rows.map((c) => (c.phone === phone ? { ...c, bot_paused: want } : c)),
    );

    try {
      const result = await api.handover(phone, want);
      // The server's value, not the guess - if another tab already handed this
      // conversation back, the server is right and the button follows it.
      dispatch({ type: "confirmed", paused: result.bot_paused });
      setConversations((rows) =>
        rows.map((c) =>
          c.phone === phone ? { ...c, bot_paused: result.bot_paused } : c,
        ),
      );
    } catch (err) {
      dispatch({ type: "failed" });
      setConversations((rows) =>
        rows.map((c) => (c.phone === phone ? { ...c, bot_paused: !want } : c)),
      );
      notify("Could not change the handover. Try again.");
      guard(err);
    }
  }

  /* ---------------- blocking ---------------- */
  async function toggleBlock() {
    if (!current || toggleBusy(blockRef.current)) return;
    const phone = current;
    const want = !toggleValue(blockRef.current);

    // Confirmed only on the way in. Blocking silently drops everything the
    // number sends afterwards, which is not something to do on a misclick;
    // unblocking is harmless and asking twice would just be in the way.
    if (
      want &&
      !window.confirm(
        `Block ${header.name || prettyPhone(phone)}?\n\n` +
          "The bot will stop replying and anything they send from now on is " +
          "ignored — it will not appear here. The messages already in this " +
          "thread are kept, and you can unblock them at any time.",
      )
    ) {
      return;
    }

    dispatchBlock({ type: "click" });
    setConversations((rows) =>
      rows.map((c) => (c.phone === phone ? { ...c, blocked: want } : c)),
    );

    try {
      const result = await api.block(phone, want);
      dispatchBlock({ type: "confirmed", paused: result.blocked });
      setConversations((rows) =>
        rows.map((c) =>
          c.phone === phone ? { ...c, blocked: result.blocked } : c,
        ),
      );
      notify(want ? "Contact blocked." : "Contact unblocked.");
    } catch (err) {
      dispatchBlock({ type: "failed" });
      setConversations((rows) =>
        rows.map((c) => (c.phone === phone ? { ...c, blocked: !want } : c)),
      );
      notify(
        want ? "Could not block. Try again." : "Could not unblock. Try again.",
      );
      guard(err);
    }
  }

  /* ---------------- payment verification ---------------- */
  async function clearPayment() {
    if (!current) return;
    const phone = current;
    // No optimistic toggle and no reducer here: this is a one-way action with
    // no opposite, so there is no state for a poll to fight over.
    setPayment(false);
    setConversations((rows) =>
      rows.map((c) => (c.phone === phone ? { ...c, payment_pending: false } : c)),
    );
    try {
      await api.clearPayment(phone);
    } catch (err) {
      setPayment(true);
      setConversations((rows) =>
        rows.map((c) => (c.phone === phone ? { ...c, payment_pending: true } : c)),
      );
      notify("Could not clear the payment flag. Try again.");
      guard(err);
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

  const closeThread = useCallback(() => {
    setCurrent(null);
    currentRef.current = null;
    setMessages([]);
  }, []);

  // Escape closes the pane, but not while someone is mid-sentence in the
  // composer or the search box - there it should mean "clear this field",
  // which is what the browser already does.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      const tag = (document.activeElement?.tagName ?? "").toLowerCase();
      if (tag === "textarea" || tag === "input") return;
      closeThread();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeThread]);

  async function signOut() {
    await api.logout();
    onSignedOut();
  }

  const note = blocked
    ? "Blocked. Nothing this number sends reaches us, and nobody replies — unblock to resume."
    : !paused
      ? "The bot is handling this conversation. Take it over to reply yourself."
      : !canReply
        ? "Outside the 24-hour window — WhatsApp will not deliver a reply until the customer messages again."
        : "You are replying. The bot stays silent until you hand it back.";

  return (
    <div className={`shell${current ? " viewing" : ""}`}>
      <aside>
        <div className="brand">
          <img className="mark" src="/logo.png" alt="" width={34} height={34} />
          <div className="t">
            <b>The iScale</b>
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
                onClick={closeThread}
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
              {/* Hidden rather than disabled while blocked: the handover has no
                  meaning there, and the server refuses it anyway. */}
              <button
                className={`btn ${paused ? "give" : "take"}${handoverBusy ? " busy" : ""}`}
                type="button"
                onClick={toggleHandover}
                disabled={handoverBusy}
                aria-busy={handoverBusy}
                hidden={blocked}
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
              <button
                className={`btn block${blocked ? " on" : ""}${blockBusy ? " busy" : ""}`}
                type="button"
                onClick={toggleBlock}
                disabled={blockBusy}
                aria-busy={blockBusy}
                title={
                  blocked
                    ? "Let this contact reach us again"
                    : "Stop interacting with this contact entirely"
                }
              >
                {blockBusy ? (
                  <span className="spin dark" />
                ) : blocked ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="9" />
                  </svg>
                ) : (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                       strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="9" /><path d="m5.6 5.6 12.8 12.8" />
                  </svg>
                )}
                {blockBusy
                  ? blocked
                    ? "Blocking…"
                    : "Unblocking…"
                  : blocked
                    ? "Unblock"
                    : "Block"}
              </button>
              {/* Closes the VIEW, not the conversation. Nothing is ended, no
                  handover is undone and the customer sees nothing - it just
                  clears the pane, which desktop otherwise had no way to do. */}
              <button
                className="icon close"
                type="button"
                onClick={closeThread}
                title="Close this conversation (Esc)"
                aria-label="Close this conversation"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2.2" strokeLinecap="round">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </header>

            {/* Above the transcript, not in it: an agent opening this thread
                needs to know there is money waiting before they read a word. */}
            {payment && (
              <div className="paybar">
                <span>
                  📸 <b>Payment screenshot received.</b> Check it in WhatsApp,
                  then confirm their seat.
                </span>
                <button type="button" onClick={clearPayment}>
                  Mark verified
                </button>
              </div>
            )}

            <Thread messages={messages} loading={threadLoading} />

            <footer>
              <div className={`note${blocked ? " warn" : !paused ? "" : canReply ? "" : " warn"}`}>
                {note}
              </div>
              <Composer disabled={blocked || !paused || !canReply} onSend={send} />
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
