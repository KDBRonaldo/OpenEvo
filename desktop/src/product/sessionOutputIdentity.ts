export type SessionOutputIdentity = {
  readonly runId: string;
  readonly attemptId: string | null;
};

export function sessionOutputIdentity(
  run: { readonly id: string; readonly current_attempt_id: string | null },
): SessionOutputIdentity {
  return { runId: run.id, attemptId: run.current_attempt_id };
}

export function sameSessionOutputIdentity(
  left: SessionOutputIdentity,
  right: SessionOutputIdentity,
): boolean {
  return left.runId === right.runId && left.attemptId === right.attemptId;
}
