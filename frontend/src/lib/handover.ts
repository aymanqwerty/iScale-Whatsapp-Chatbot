/**
 * Who owns the handover state.
 *
 * The bug this exists to kill: the button flipped to the clicked state, back to
 * the old one, then forward again a second later. The cause was two independent
 * writers - the click and the background poll - with no ordering between them.
 * Guards against "stale" responses kept missing a window, because the real
 * problem was never which response was stale; it was that both were allowed to
 * write at all.
 *
 * So: one reducer, and exactly one rule.
 *
 *   While the user has an unconfirmed intent, THE USER OWNS THE STATE.
 *   The server cannot write it, whatever it says and whenever it arrives.
 *
 * That makes the flip-back impossible by construction rather than unlikely by
 * timing, and it is a pure function, so the race can be tested rather than
 * reasoned about.
 */

export type Handover =
  /** Nobody is mid-click. Whatever the server last told us is the truth. */
  | { kind: "settled"; paused: boolean; epoch: number }
  /** A click is in flight. `want` is displayed and the server is ignored. */
  | { kind: "pending"; want: boolean; was: boolean; epoch: number };

export type HandoverEvent =
  /** The user pressed the button. */
  | { type: "click" }
  /** The server confirmed a change we asked for. */
  | { type: "confirmed"; paused: boolean }
  /** The change failed; fall back to what it was before the click. */
  | { type: "failed" }
  /**
   * A poll or socket update carrying the server's view.
   *
   * `epoch` is whatever the state held when the request was ISSUED. Anything
   * older describes a world before the most recent click or confirmation, so it
   * is discarded - which closes the last window: a poll that left before the
   * POST completed and lands a moment after it.
   */
  | { type: "observed"; paused: boolean; epoch: number }
  /** A different conversation was opened. */
  | { type: "reset"; paused: boolean };

export const initialHandover: Handover = {
  kind: "settled",
  paused: false,
  epoch: 0,
};

/** What the button should display. */
export function isPaused(state: Handover): boolean {
  return state.kind === "pending" ? state.want : state.paused;
}

/** Whether a request is in flight - drives the lock and the spinner. */
export function isBusy(state: Handover): boolean {
  return state.kind === "pending";
}

export function handoverReducer(
  state: Handover,
  event: HandoverEvent,
): Handover {
  switch (event.type) {
    case "click":
      // Ignored while one is already in flight: two clicks in the same tick
      // would otherwise both fire, and whichever response landed last would win.
      if (state.kind === "pending") return state;
      return {
        kind: "pending",
        want: !state.paused,
        was: state.paused,
        epoch: state.epoch + 1,
      };

    case "confirmed":
      // The server's value, not the optimistic guess - if it disagrees (someone
      // else handed the conversation back), the server is right.
      return { kind: "settled", paused: event.paused, epoch: state.epoch + 1 };

    case "failed":
      return {
        kind: "settled",
        paused: state.kind === "pending" ? state.was : state.paused,
        epoch: state.epoch + 1,
      };

    case "observed":
      // The entire fix. A poll that left before the click, or during it, or
      // that lands a moment after it settles, all say the same stale thing -
      // and none of them may speak while the user's intent is unconfirmed.
      if (state.kind === "pending") return state;
      // Issued before the last click or confirmation: it cannot know about it.
      if (event.epoch < state.epoch) return state;
      return { kind: "settled", paused: event.paused, epoch: state.epoch };

    case "reset":
      // Switching conversation abandons any in-flight intent: it belonged to
      // the previous thread and must not be displayed against this one.
      return { kind: "settled", paused: event.paused, epoch: state.epoch + 1 };

    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}

/*
 * The block toggle has exactly the same shape and exactly the same race - an
 * optimistic click competing with a background poll over one boolean - so it
 * runs on this machine rather than a second copy of it. Aliased instead of
 * duplicated: a divergent copy is how the original bug would come back, and
 * every test in handover.test.ts covers both callers as a result.
 *
 * Read `paused` as "the value the user owns while their click is in flight".
 */
export const toggleReducer = handoverReducer;
export const initialToggle = initialHandover;
export const toggleValue = isPaused;
export const toggleBusy = isBusy;
