# Runtime Injection

> Target contract: staging exists in Core, while complete release manifests and
> packaged science/benchmark E2E coverage remain workstream B/E work.

Runtime injection is the Core-owned path that makes promoted evolution
artifacts available to later agent sessions. It is shared by ordinary science
runs and benchmark automation; Desktop displays Core-provided artifacts and the
method-owned promotion decision.

## Text Memory

`text_memory` artifacts are natural-language memory files. Core stages the
selected memory to:

```text
/openevo/session/evolution/memory.md
```

and sets:

```text
OPENEVO_MEMORY_FILE=/openevo/session/evolution/memory.md
```

Harnesses may prepend the rendered memory to agent instructions.

## Skill Bundle

`skill_bundle` artifacts are directories containing at least `SKILL.md`. Core
stages selected skills under:

```text
/openevo/session/evolution/skills/
```

and sets:

```text
OPENEVO_SKILLS_DIR=/openevo/session/evolution/skills
```

Copy-based harnesses load from that directory. Path-based harnesses must prefer
the evolution skill path over static skill paths when both exist.

## Agent-System Staging Paths

`agent_system` artifacts contain evolved harness instructions. Core writes the
canonical file:

```text
/openevo/session/evolution/agent_system.md
```

It may also write a safe relative target such as `AGENTS.md`, `CLAUDE.md`,
`GEMINI.md`, or `.openhands/microagents/*.md` into the runtime workdir.

## Env Vars

Core sets:

```text
OPENEVO_AGENT_SYSTEM_FILE=/openevo/session/evolution/agent_system.md
OPENEVO_AGENT_SYSTEM_TARGET=<target-path>
OPENEVO_AGENT_SYSTEM_TARGETS=<json-or-delimited-target-list>
```

The target path must be validated before writing into the runtime workdir.

## Injection Manifest

Each release gate must produce a runtime injection manifest that records:

- selected artifact IDs and types;
- source URIs and payload hashes;
- staged runtime paths;
- environment variables;
- harness arguments or instruction targets;
- pre-task probe result;
- compatibility match evidence.

The public release manifest may aggregate per-task manifests, but it must avoid
raw secrets, private paths, and hidden benchmark answers.

## Context Resolver Boundary

The evolution method owns candidate evaluation, best-result selection, and
promotion. Generic context resolution filters for promoted, compatible artifacts
and keeps its existing fallback ordering. A Core product run instead passes the
ordered artifact membership from its pinned revision. Exact resolution validates
and consumes that list in order, without requiring or rewriting each member's
`promoted` flag; Core's successor membership, not a guessed score or promotion
bit, is authoritative for the next session.

Generic fallback ordering for old artifacts is an implementation detail of
`src/openevo/evolution/context.py`; productization does not redefine it. Release
science paths pass the exact revision artifact IDs explicitly and reject
duplicates, omissions, incompatible members, or reordered resolver output.
Changing fallback ranking requires a separate issue, regression tests, and
algorithm-impact review.

Core rejects artifacts incompatible with the task, harness, model, execution
mode, or base model. It also validates payload and lineage references so a run
does not silently inject an unrelated or stale artifact. Injection evidence
records the selected IDs, rejected incompatible IDs, staged paths, and payload
integrity.

## Runtime Readback Receipt

Gateway publishes a runtime injection receipt only after harness postprocessing
and Core-owned verification of the runtime readback. The public
`BaseRuntime.download_file(remote_path, local_path) -> None` and
`download_dir(remote_path, local_path) -> None` contracts are unchanged:
Docker/Apptainer keep bind-copy plus `docker cp`/tar fallback, and third-party
runtime plugins may override them.

For the canonical `/openevo/session/evolution` product target on a supported
Linux Core runtime, Gateway uses a separate private primitive rather than the
public download methods. Its runtime walk is no-follow and FD-relative below
the pinned session bind.
Every directory carries strong inode/ctime/mtime identity plus Linux mutation
generation evidence, is enumerated in sorted order before copying, and is
enumerated again after all descendants have been streamed. File bytes are
hashed while copied in bounded chunks. Source additions, removals, rename or
replace/restore ABA, in-place changes, growth, symlinks, hardlinks, and special
files reject the receipt. Host output uses a private staging tree and atomic
no-replace publication so a failed attempt cannot overwrite or delete a raced
replacement.

Readback cleanup never verifies one pathname and then deletes that original
name. After binding the initially observed inode, it atomically renames the
original name to a random no-replace quarantine and accepts cleanup authority
only after the held FD and quarantined pathname still match. If a replacement
wins the pre-rename race, the identity check retains the quarantine and displaced
object and fails closed instead of deleting either pathname.
The rule is recursive: each file or directory child receives its own random
no-replace quarantine and held-FD identity check before deletion. The Gateway
readback temporary root is created and cleaned by the same Core authority rather
than `TemporaryDirectory` pathname recursion.

One private non-refundable readback budget covers the canonical evolution tree
and the separate agent-system target scan. Its closed maxima exactly match the
receipt payload limits of 4096 files and 64 MiB; the 16384 node-attempt bound
covers both mandatory directory enumerations. Failed or cancelled remote target
inventory pessimistically exhausts the remaining authority when exact progress
cannot be recovered. All synchronous traversal and cleanup runs in controlled
worker threads, and cancellation waits for the worker to stop before runtime
teardown proceeds.

Gateway builds receipt v3 from the source-stream inventory. The Core run owner
still reconstructs the expected rendering from the pinned context/revision and
authoritative artifact bytes, then compares every ordered file digest,
instruction digest, artifact runtime path, and tree digest before allowing the
session to succeed.

For a custom target directory, non-Linux Core host, or third-party runtime,
Gateway preserves the public backend download path. It ignores backend-returned
inventory and downloads below a held private temporary root with a concurrent
logical file/node/byte quota. On Linux destinations, a cumulative event ledger
charges created files/nodes and closed-write bytes even when the entry is later
deleted. A surviving entry gives the scanner credit only while pathname and
inode still match, so download and scan share one non-refundable accounting
authority without double charging the same receipt object. Download failure,
event loss, monitor/inspection errors, and unprovable work exhaust that
authority before agent-system target readback can run.

A runtime-private no-follow scanner then performs the exact receipt walk with
the same 4096-file, 16384-node, and 64-MiB limits. The temporary root and every
recursive child are removed only through FD-bound random quarantine. A public
downloader that does not accept cancellation has a one-second hard join bound;
afterward Core quarantines its root, reports unresolved ownership, and schedules
cleanup only after the task exits. Non-Linux compatibility retains the final
bounded scanner but does not assert Linux event-generation evidence. This path
does not reuse the evolution payload scanner's 256-file and 16-GiB defaults.

## Benchmark/Science Consumers

Benchmark/science consumers share the same Core runtime injection contract.

### Benchmark Consumers

Benchmark automation calls Core APIs and consumes the same runtime injection
contract as science runs. Benchmark-specific adapters and scorers live outside
Core and Desktop.

### Science Consumers

OpenEvo Desktop lets ordinary users inspect text memory, skill bundle, and
agent-system artifacts plus method-owned evidence. A follow-up product run uses
the artifact membership committed in the pinned successor revision; Core performs
the final ordered compatibility, payload, rendering, and runtime-readback checks.
