import { fetchEventSource } from "@microsoft/fetch-event-source";

export type OpenEvoEvent = {
  type: string;
  ts: string;
  data: Record<string, any>;
};

export function subscribeOpenEvoEvents(
  onEvent: (event: OpenEvoEvent) => void,
  controller: AbortController,
): void {
  fetchEventSource("/api/events", {
    signal: controller.signal,
    openWhenHidden: true,
    async onopen(response) {
      if (!response.ok) {
        throw new Error(`SSE open failed: ${response.status}`);
      }
    },
    onmessage(event) {
      if (!event.data) return;
      try {
        const parsed = JSON.parse(event.data) as OpenEvoEvent;
        onEvent(parsed);
      } catch {
        // ignore malformed payloads
      }
    },
    onerror() {
      // The library auto-reconnects with backoff; swallow per-error noise.
    },
  }).catch(() => {
    // Ignore — the AbortController controls shutdown.
  });
}
