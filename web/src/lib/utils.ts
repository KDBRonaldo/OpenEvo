export function shortId(value: string | null | undefined, maxLength = 12): string {
  const text = String(value ?? "");
  if (text.length <= maxLength) return text;
  if (maxLength <= 1) return text.slice(0, maxLength);
  return `${text.slice(0, maxLength - 1)}...`;
}

export function formatReward(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value.toFixed(3);
}

export function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  if (value < 1000) return `${value.toFixed(0)}ms`;
  return `${(value / 1000).toFixed(2)}s`;
}

export function relativeTime(timestampSeconds: number | null | undefined): string {
  if (typeof timestampSeconds !== "number" || Number.isNaN(timestampSeconds)) {
    return "-";
  }
  const deltaMs = Date.now() - timestampSeconds * 1000;
  const absMs = Math.abs(deltaMs);
  const suffix = deltaMs >= 0 ? "ago" : "from now";

  if (absMs < 60_000) return `${Math.max(0, Math.round(absMs / 1000))}s ${suffix}`;
  if (absMs < 3_600_000) return `${Math.round(absMs / 60_000)}m ${suffix}`;
  if (absMs < 86_400_000) return `${Math.round(absMs / 3_600_000)}h ${suffix}`;
  return `${Math.round(absMs / 86_400_000)}d ${suffix}`;
}

export function statusClass(status: string | null | undefined): string {
  switch ((status ?? "").toLowerCase()) {
    case "completed":
    case "ok":
      return "bg-emerald-100 text-emerald-800";
    case "running":
    case "registered":
    case "init":
    case "ready":
      return "bg-blue-100 text-blue-800";
    case "timeout":
      return "bg-amber-100 text-amber-800";
    case "failed":
    case "error":
    case "unreachable":
      return "bg-red-100 text-red-800";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

export async function copyToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "absolute";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}
