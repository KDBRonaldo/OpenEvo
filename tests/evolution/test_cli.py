from __future__ import annotations

from openevo.evolution.cli import build_parser


def test_evolution_cli_defaults_use_openevo_state_root() -> None:
    parser = build_parser()

    cases = [
        ["serve"],
        ["worker"],
        ["terminal-bench-dataset", "--input", "trial", "--name", "tb"],
        ["terminal-bench-agent-system-job", "--reflector-model", "gpt-5.5"],
        ["terminal-bench-text-memory-job", "--reflector-model", "gpt-5.5"],
        [
            "terminal-bench-parametric-memory-job",
            "--base-model",
            "Qwen/Qwen3.6-27B",
            "--trainer-command",
            "train.sh",
        ],
    ]

    for argv in cases:
        args = parser.parse_args(argv)
        if hasattr(args, "db"):
            assert args.db == ".openevo/evolution/evolution.db"
        if hasattr(args, "artifact_root"):
            assert args.artifact_root == ".openevo/evolution"
