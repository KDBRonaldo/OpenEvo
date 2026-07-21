type Deferred = {
  promise: Promise<void>;
  resolve: () => void;
};

export class InFlightCaptureWindow<Key> {
  readonly #inFlight = new Map<Key, Deferred>();
  #accepting = true;

  begin(key: Key): boolean {
    if (!this.#accepting) return false;
    if (this.#inFlight.has(key)) throw new Error("Capture request was started twice");
    let resolve!: () => void;
    const promise = new Promise<void>((settle) => {
      resolve = settle;
    });
    this.#inFlight.set(key, { promise, resolve });
    return true;
  }

  accepts(key: Key): boolean {
    return this.#inFlight.has(key);
  }

  finish(key: Key): void {
    const deferred = this.#inFlight.get(key);
    if (!deferred) return;
    this.#inFlight.delete(key);
    deferred.resolve();
  }

  async close(): Promise<void> {
    this.#accepting = false;
    await Promise.all([...this.#inFlight.values()].map(({ promise }) => promise));
  }
}

export async function drainPendingSnapshot(
  pending: ReadonlySet<Promise<void>>,
): Promise<void> {
  await Promise.all([...pending]);
}
