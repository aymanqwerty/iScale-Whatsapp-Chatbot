/**
 * Console API client.
 *
 * Auth is a bearer token rather than the cookie the same-origin console uses.
 * A separately hosted frontend makes that cookie third-party, and Safari and
 * Brave block those outright - so the backend returns the same signed token at
 * login and accepts it in an Authorization header.
 *
 * The token is held in localStorage so a refresh does not sign you out. That is
 * more exposed to XSS than an httpOnly cookie, which is the honest cost of
 * hosting the frontend elsewhere; React escapes rendered content by default and
 * nothing here uses dangerouslySetInnerHTML, so the surface is small.
 */

const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const ROOT = `${BASE}/api/v1/console`;
const TOKEN_KEY = "iscale_console_token";

export type Sender = "USER" | "BOT" | "AGENT" | "SYSTEM";

export interface Conversation {
  phone: string;
  name: string;
  last_activity: string;
  last_message: string;
  last_sender: Sender | "";
  bot_paused: boolean;
  blocked: boolean;
  payment_pending: boolean;
}

export interface Message {
  id: number;
  sender: Sender;
  text: string;
  at: string;
  /** Client-only: drawn immediately, before the server confirms. */
  pending?: boolean;
  failed?: boolean;
}

export interface Thread {
  phone: string;
  name: string;
  bot_paused: boolean;
  blocked: boolean;
  blocked_at: string;
  payment_pending: boolean;
  payment_proof_at: string;
  can_reply: boolean;
  window_expires: string;
  messages: Message[];
}

/** Thrown when the session is gone, so the UI can route to login rather than
 *  showing an error the user can do nothing about. */
export class SessionExpired extends Error {
  constructor() {
    super("Session expired");
  }
}

export const token = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (value: string) => localStorage.setItem(TOKEN_KEY, value),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const stored = token.get();
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (stored) headers.set("Authorization", `Bearer ${stored}`);

  const response = await fetch(`${ROOT}${path}`, {
    ...init,
    headers,
    // Sends the cookie too when the frontend happens to be same-origin, so one
    // client covers both deployments.
    credentials: "include",
  });

  if (response.status === 401) {
    token.clear();
    throw new SessionExpired();
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export const api = {
  async login(username: string, password: string): Promise<string> {
    const body = await request<{ token: string; username: string }>(
      "/api/login",
      { method: "POST", body: JSON.stringify({ username, password }) },
    );
    token.set(body.token);
    return body.username;
  },

  async logout(): Promise<void> {
    try {
      await request("/api/logout", { method: "POST" });
    } catch {
      // Signing out locally matters more than the server acknowledging it.
    }
    token.clear();
  },

  me: () => request<{ username: string }>("/api/me"),

  conversations: () =>
    request<{ conversations: Conversation[] }>("/api/conversations"),

  messages: (phone: string, afterId = 0) =>
    request<Thread>(
      `/api/messages/${encodeURIComponent(phone)}?after_id=${afterId}`,
    ),

  handover: (phone: string, paused: boolean) =>
    request<{ phone: string; bot_paused: boolean }>("/api/handover", {
      method: "POST",
      body: JSON.stringify({ phone, paused }),
    }),

  block: (phone: string, blocked: boolean) =>
    request<{ phone: string; blocked: boolean; bot_paused: boolean }>(
      "/api/block",
      { method: "POST", body: JSON.stringify({ phone, blocked }) },
    ),

  clearPayment: (phone: string) =>
    request<{ phone: string; payment_pending: boolean }>(
      "/api/payment-verified",
      { method: "POST", body: JSON.stringify({ phone }) },
    ),

  send: (phone: string, text: string) =>
    request<{ id: number; sender: Sender; text: string }>("/api/send", {
      method: "POST",
      body: JSON.stringify({ phone, text }),
    }),
};

/**
 * URL for the live-update socket.
 *
 * The token travels as a query parameter because a browser cannot set an
 * Authorization header on a WebSocket handshake. It is short-lived and signed,
 * and the socket only ever carries a nudge - the messages themselves still come
 * through the authenticated fetch above.
 */
export function streamUrl(): string {
  const base = BASE || window.location.origin;
  const url = new URL(`${base}/api/v1/console/api/stream`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const stored = token.get();
  if (stored) url.searchParams.set("token", stored);
  return url.toString();
}
