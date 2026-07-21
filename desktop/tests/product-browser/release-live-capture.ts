type Deferred = {
  promise: Promise<void>;
  resolve: () => void;
};

export class InFlightCaptureCutoff<Key> {
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

  async close(timeoutMs: number): Promise<readonly Key[]> {
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
      throw new Error("Capture cutoff timeout must be a positive safe integer");
    }
    this.#accepting = false;
    const snapshot = [...this.#inFlight.entries()];
    if (snapshot.length === 0) return [];
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const timedOut = await Promise.race([
      Promise.all(snapshot.map(([, deferred]) => deferred.promise)).then(() => false),
      new Promise<true>((resolve) => {
        timeout = setTimeout(() => resolve(true), timeoutMs);
      }),
    ]);
    if (timeout !== undefined) clearTimeout(timeout);
    if (!timedOut) return [];
    const unresolved = snapshot
      .filter(([key]) => this.#inFlight.has(key))
      .map(([key]) => key);
    for (const key of unresolved) this.finish(key);
    return unresolved;
  }
}

type ArtifactPredecessorCandidate = {
  id: string;
  project_id: string;
  target_id: string;
  artifact_type: string;
  created_at: string;
  produced_revision: { generation: number };
};

export function selectLatestArtifactPredecessor<T extends ArtifactPredecessorCandidate>(
  current: T,
  sources: readonly T[],
): T | undefined {
  const candidates = sources.filter((source) => (
    source.id !== current.id
    && source.project_id === current.project_id
    && source.target_id === current.target_id
    && source.artifact_type === current.artifact_type
    && source.produced_revision.generation < current.produced_revision.generation
  ));
  candidates.sort((left, right) => (
    left.produced_revision.generation - right.produced_revision.generation
    || pythonStringCompare(left.created_at, right.created_at)
    || pythonStringCompare(left.id, right.id)
  ));
  return candidates.at(-1);
}

function pythonStringCompare(left: string, right: string): number {
  const leftCodePoints = [...left].map((character) => character.codePointAt(0)!);
  const rightCodePoints = [...right].map((character) => character.codePointAt(0)!);
  const length = Math.min(leftCodePoints.length, rightCodePoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = leftCodePoints[index]! - rightCodePoints[index]!;
    if (difference !== 0) return difference;
  }
  return leftCodePoints.length - rightCodePoints.length;
}

export async function drainPendingSnapshot(
  pending: ReadonlySet<Promise<void>>,
): Promise<void> {
  await Promise.all([...pending]);
}
