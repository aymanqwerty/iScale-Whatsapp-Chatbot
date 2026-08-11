import { useEffect, useRef, useState } from "react";
import { streamUrl } from "./api";

/**
 * Live updates over a WebSocket, with polling as a fallback.
 *
 * The socket carries only a nudge - `{type:"activity", phone}` - never message
 * content. The delta still comes through the authenticated fetch, so there is
 * one code path for the transcript and the socket can never become a second,
 * weaker way to read customer data.
 *
 * Polling stays on at a slower cadence rather than being switched off when the
 * socket connects. A dropped socket then costs a few seconds of latency instead
 * of a silently dead page, which matters on an instance that sleeps.
 */
export function useLiveUpdates(onActivity: (phone: string) => void): boolean {
  const [connected, setConnected] = useState(false);
  // Held in a ref so reconnecting does not depend on the callback's identity;
  // otherwise every parent render would tear the socket down and rebuild it.
  const handler = useRef(onActivity);
  handler.current = onActivity;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry = 1000;
    let timer: number | undefined;
    let closed = false;

    const connect = () => {
      if (closed) return;
      try {
        socket = new WebSocket(streamUrl());
      } catch {
        schedule();
        return;
      }

      socket.onopen = () => {
        retry = 1000;
        setConnected(true);
      };
      socket.onmessage = (event) => {
        let payload: { type?: string; phone?: string };
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        if (payload.type !== "activity" || !payload.phone) return; // keepalive
        handler.current(payload.phone);
      };
      socket.onclose = () => {
        setConnected(false);
        schedule();
      };
      socket.onerror = () => socket?.close();
    };

    const schedule = () => {
      if (closed) return;
      socket = null;
      timer = window.setTimeout(connect, retry);
      // Back off rather than hammering an instance that may be asleep.
      retry = Math.min(retry * 2, 30000);
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return connected;
}

/** Run `fn` every `ms`, without restarting the timer on every render. */
export function useInterval(fn: () => void, ms: number): void {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    const id = window.setInterval(() => saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}
