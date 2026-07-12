# Terminal Bench 2.1 Codex GPT-5.5 Failed Tasks

Last updated: 2026-06-28.

This file records the failed task set from the local Terminal Bench 2.1
`codex + gpt-5.5` baseline. It is the canonical failed-task reference for
follow-up per-task evolution experiments.

## Source Run

- Baseline local run label:
  `tb21-full-codex-gpt55-subscription-cache-20260624-085451`
- Evidence status:
  `historical_local_path_redacted_non_release_evidence`
- Started: `2026-06-24T08:54:51.924148`
- Finished: `2026-06-24T17:11:45.734819`
- Total trials: 89
- Passed trials: 64
- Failed/non-pass trials: 25
- Pass rate: 71.9%

## Failure Definition

A task is counted as failed if its baseline verifier reward was not `1.0`.
Tasks with verifier timeout or missing verifier reward are included in the
failed/non-pass set.

`tune-mjcf` is not included here: it had an `AgentTimeoutError`, but the
verifier reward was `1.0`.

## Failed Task List

```text
chess-best-move
compile-compcert
configure-git-webserver
dna-insert
filter-js-from-html
gcode-to-text
large-scale-text-editing
make-doom-for-mips
make-mips-interpreter
mteb-retrieve
overfull-hbox
password-recovery
protein-assembly
pypi-server
pytorch-model-cli
pytorch-model-recovery
qemu-alpine-ssh
query-optimize
raman-fitting
regex-chess
sam-cell-seg
torch-pipeline-parallelism
train-fasttext
video-processing
vulnerable-secret
```

## Baseline Failure Details

| Task | Baseline reward | Baseline exception |
| --- | ---: | --- |
| `chess-best-move` | 0.0 |  |
| `compile-compcert` | 0.0 | `AgentTimeoutError` |
| `configure-git-webserver` | 0.0 |  |
| `dna-insert` | 0.0 |  |
| `filter-js-from-html` | 0.0 |  |
| `gcode-to-text` | 0.0 |  |
| `large-scale-text-editing` | 0.0 |  |
| `make-doom-for-mips` | 0.0 | `AgentTimeoutError` |
| `make-mips-interpreter` | 0.0 |  |
| `mteb-retrieve` | null | `VerifierTimeoutError` |
| `overfull-hbox` | 0.0 |  |
| `password-recovery` | 0.0 | `NonZeroAgentExitCodeError` |
| `protein-assembly` | 0.0 |  |
| `pypi-server` | 0.0 |  |
| `pytorch-model-cli` | null | `VerifierTimeoutError` |
| `pytorch-model-recovery` | 0.0 |  |
| `qemu-alpine-ssh` | 0.0 |  |
| `query-optimize` | 0.0 |  |
| `raman-fitting` | 0.0 |  |
| `regex-chess` | 0.0 |  |
| `sam-cell-seg` | 0.0 |  |
| `torch-pipeline-parallelism` | null | `VerifierTimeoutError` |
| `train-fasttext` | 0.0 |  |
| `video-processing` | 0.0 |  |
| `vulnerable-secret` | 0.0 | `NonZeroAgentExitCodeError` |
