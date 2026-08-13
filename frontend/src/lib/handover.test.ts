import { describe, expect, it } from "vitest";
import {
  handoverReducer,
  initialHandover,
  initialToggle,
  isBusy,
  isPaused,
  toggleBusy,
  toggleReducer,
  toggleValue,
  type Handover,
  type HandoverEvent,
} from "./handover";

/** Apply a sequence of events and report what the button shows at each step. */
function play(events: HandoverEvent[], from: Handover = initialHandover) {
  const frames: boolean[] = [];
  let state = from;
  for (const event of events) {
    state = handoverReducer(state, event);
    frames.push(isPaused(state));
  }
  return { state, frames, paused: isPaused(state) };
}

describe("the reported bug", () => {
  it("does not flip back when a poll lands mid-request", () => {
    // Exactly what was reported: click shows state 2, then it reverted to
    // state 1, then returned to state 2 a second or two later.
    const { frames, paused } = play([
      { type: "click" }, //                    -> state 2
      { type: "observed", paused: false, epoch: 0 }, //   poll issued before the click
      { type: "confirmed", paused: true }, //   POST returns
    ]);

    expect(frames).toEqual([true, true, true]); // never dips to false
    expect(paused).toBe(true);
  });

  it("does not flip back when a poll lands just after settling", () => {
    // The window the earlier guards missed: a poll issued while the POST was in
    // flight, landing after it completed, still carrying the old value. It is
    // recognised by the epoch it was issued at.
    const { frames, paused } = play([
      { type: "click" }, //                              epoch 0 -> 1
      { type: "confirmed", paused: true }, //             epoch 1 -> 2
      { type: "observed", paused: false, epoch: 0 }, //   issued before either
    ]);

    expect(frames).toEqual([true, true, true]);
    expect(paused).toBe(true);
  });

  it("still accepts a genuinely current observation", () => {
    // The epoch check must not deafen the console to real changes made
    // elsewhere - another agent handing a conversation back, say.
    const { paused } = play([
      { type: "click" },
      { type: "confirmed", paused: true },
      { type: "observed", paused: false, epoch: 2 }, // issued after settling
    ]);

    expect(paused).toBe(false);
  });

  it("survives a burst of stale polls during the request", () => {
    const { frames, paused } = play([
      { type: "click" },
      { type: "observed", paused: false, epoch: 0 },
      { type: "observed", paused: false, epoch: 0 },
      { type: "observed", paused: false, epoch: 0 },
      { type: "confirmed", paused: true },
    ]);

    expect(frames.every(Boolean)).toBe(true);
    expect(paused).toBe(true);
  });
});

describe("clicking", () => {
  it("shows the new state immediately", () => {
    const state = handoverReducer(initialHandover, { type: "click" });
    expect(isPaused(state)).toBe(true);
    expect(isBusy(state)).toBe(true);
  });

  it("ignores a second click while one is in flight", () => {
    const { paused, state } = play([{ type: "click" }, { type: "click" }]);
    // Two clicks must not cancel out into the original state.
    expect(paused).toBe(true);
    expect(isBusy(state)).toBe(true);
  });

  it("toggles again once the first has settled", () => {
    const { paused } = play([
      { type: "click" },
      { type: "confirmed", paused: true },
      { type: "click" },
    ]);
    expect(paused).toBe(false);
  });
});

describe("the server has the last word", () => {
  it("follows the server when it disagrees with the guess", () => {
    // Someone else handed the conversation back from another tab.
    const { paused } = play([
      { type: "click" },
      { type: "confirmed", paused: false },
    ]);
    expect(paused).toBe(false);
  });

  it("rolls back to the pre-click value on failure", () => {
    const { paused, state } = play([{ type: "click" }, { type: "failed" }]);
    expect(paused).toBe(false);
    expect(isBusy(state)).toBe(false);
  });

  it("rolls back correctly from the paused side too", () => {
    const from: Handover = { kind: "settled", paused: true, epoch: 0 };
    const { paused } = play([{ type: "click" }, { type: "failed" }], from);
    expect(paused).toBe(true);
  });

  it("accepts observations once nothing is in flight", () => {
    const { paused } = play([{ type: "observed", paused: true, epoch: 0 }]);
    expect(paused).toBe(true);
  });
});

describe("switching conversation", () => {
  it("abandons an in-flight intent from the previous thread", () => {
    const { paused, state } = play([
      { type: "click" }, // pending on conversation A
      { type: "reset", paused: false }, // opened conversation B
    ]);
    expect(paused).toBe(false);
    expect(isBusy(state)).toBe(false);
  });
});

describe("the invariant", () => {
  it("never lets the server overwrite an unconfirmed intent", () => {
    // Brute force: every interleaving of one click with up to three
    // observations must display the clicked value throughout.
    const observations: HandoverEvent[] = [
      { type: "observed", paused: false, epoch: 0 },
      { type: "observed", paused: false, epoch: 0 },
      { type: "observed", paused: false, epoch: 0 },
    ];


    for (let position = 0; position <= observations.length; position++) {
      const events: HandoverEvent[] = [
        ...observations.slice(0, position),
        { type: "click" },
        ...observations.slice(position),
      ];
      const { state, frames } = play(events);
      const afterClick = frames.slice(position);
      expect(
        afterClick.every(Boolean),
        `reverted with ${position} observations before the click`,
      ).toBe(true);
      expect(isBusy(state)).toBe(true);
    }
  });
});

describe("the block toggle", () => {
  it("is the same machine, not a copy of it", () => {
    // The handover bug took three attempts to kill. A second, forked
    // implementation of the same optimistic toggle is precisely how it would
    // come back, so blocking is aliased to this reducer and this test fails
    // the moment someone quietly gives it its own.
    expect(toggleReducer).toBe(handoverReducer);
    expect(toggleValue).toBe(isPaused);
    expect(toggleBusy).toBe(isBusy);
    expect(initialToggle).toBe(initialHandover);
  });

  it("keeps showing Blocked while a poll insists otherwise", () => {
    // The exact sequence that broke the handover button: click, then a poll
    // that left before it and still carries the old value.
    const { frames, paused } = play([
      { type: "click" },
      { type: "observed", paused: false, epoch: 0 },
      { type: "observed", paused: false, epoch: 0 },
    ]);
    expect(frames).toEqual([true, true, true]);
    expect(paused).toBe(true);
  });

  it("rolls back to unblocked when the server refuses", () => {
    const { paused } = play([{ type: "click" }, { type: "failed" }]);
    expect(paused).toBe(false);
  });
});
