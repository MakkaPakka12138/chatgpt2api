// Legacy server timestamps without an offset were written in UTC.
// Explicit offsets are preserved; display always uses the browser's timezone.
export function parseServerDate(value?: string | number | null): Date | null {
  if (value === undefined || value === null || value === "") return null;
  let normalized: string | number = value;
  if (typeof value === "string") {
    normalized = value.trim();
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(normalized)) {
      normalized = normalized.replace(" ", "T");
      if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized)) normalized += "Z";
    }
  }
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatBrowserDateTime(value?: string | number | null): string {
  const date = parseServerDate(value);
  if (!date) return value ? String(value) : "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function browserDateBounds(startDate?: string, endDate?: string) {
  const midnight = (value: string, nextDay: boolean) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match) throw new Error("日期格式无效");
    const [, year, month, day] = match.map(Number);
    const date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) throw new Error("日期无效");
    // Advance the calendar day rather than adding 24 hours (DST days may differ).
    if (nextDay) date.setDate(date.getDate() + 1);
    return date.toISOString();
  };
  return {
    ...(startDate ? { start_at: midnight(startDate, false) } : {}),
    ...(endDate ? { end_before: midnight(endDate, true) } : {}),
  };
}

export function browserTimeDetails(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(browserTimeDetails);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      typeof item === "string" && /(?:_at|_time)$|^(?:time|timestamp)$/.test(key) && /^\d{4}-\d{2}-\d{2}[ T]/.test(item)
        ? formatBrowserDateTime(item) : browserTimeDetails(item),
    ]));
  }
  return value;
}
