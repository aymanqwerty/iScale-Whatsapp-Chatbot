/** Presentation helpers shared by the list and the thread. */

export function prettyPhone(raw: string): string {
  const digits = String(raw).replace(/[^0-9]/g, "");
  if (digits.length === 12 && digits.startsWith("91")) {
    return `+91 ${digits.slice(2, 7)} ${digits.slice(7)}`;
  }
  return `+${digits}`;
}

export function timeAgo(iso: string): string {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d`;
  return new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });
}

export function clock(iso: string): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function dayLabel(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  if (sameDay(date, today)) return "Today";
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(date, yesterday)) return "Yesterday";
  return date.toLocaleDateString([], { day: "numeric", month: "long" });
}

export function initials(name: string, phone: string): string {
  const trimmed = (name || "").trim();
  if (trimmed) {
    const parts = trimmed.split(/\s+/);
    const first = parts[0]?.[0] ?? "";
    const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
    return (first + last).toUpperCase();
  }
  return String(phone).replace(/[^0-9]/g, "").slice(-2);
}

/**
 * A stable colour per number, so the same person keeps the same avatar across
 * reloads and staff start recognising threads without reading them.
 */
export function avatarStyle(phone: string): { background: string } {
  let hue = 0;
  for (const char of String(phone)) hue = (hue * 31 + char.charCodeAt(0)) % 360;
  return {
    background: `linear-gradient(145deg,hsl(${hue} 55% 46%),hsl(${(hue + 38) % 360} 58% 34%))`,
  };
}
