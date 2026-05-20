"""Prefix-merging trajectory builder.

Reconstructs a single token-level training trace out of the many independent
LLM completions an agent emits during one rollout.  A harness (claude_code,
codex, pi, ...) drives the agent and each turn hits the gateway as a separate
completion request; this builder stitches those completions back into the
``prompt + response_1 + interstitial + response_2 + ...`` stream that an RL
trainer needs, without introducing tokenization drift.

Design in two stages:

1. **Grouping** — detect which completions belong to the same append-only
   agent chain.  A cheap message-level key is used as an O(1) index, and a
   strict token-prefix check (``C_{k+1}.prompt_ids`` must start with
   ``C_k.prompt_ids``) is the final arbiter.  Completions whose tokens
   diverge start a fresh chain instead of silently polluting an existing one.

2. **Finalization** — walk each chain and build a merged token stream:

   - Assistant bodies come from the **raw** ``response_ids`` actually sampled
     by the model.  Their logprobs are real and we never decode→re-encode,
     so BPE non-canonicality cannot bite.
   - Interstitials (tool results, chat-template glue, intermediate user
     turns) come from ``C_{i+1}.prompt_ids`` — the server's **canonical**
     tokenization.  The boundary between "canonical copy of the previous
     assistant body" and the actual interstitial is the first end-of-turn
     token (``<|im_end|>`` on Qwen / ChatML; auto-detected or configurable).
   - Interstitial slots get synthesized logprobs and a zero ``loss_mask``;
     sampled assistant slots keep their real logprobs and a one ``loss_mask``.

See ``docs/prefix_merging_algorithm.md`` for a full walkthrough with
examples, invariants, and edge cases.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from copy import deepcopy
from typing import Any

from polar.trajectory.builder.base import BaseTrajectoryBuilder
from polar.trajectory.builder.record_utils import build_trace_from_completion
from polar.trajectory.models import CompletionRecord, CompletionSession, Trace, Trajectory

logger = logging.getLogger(__name__)

# finish_reasons where the model emitted the natural end-of-turn token itself.
_NATURAL_STOP_REASONS = frozenset({"stop", "tool_calls", "stop_sequence"})



# ---------------------------------------------------------------------------
# Message-level grouping helpers — used to detect which completions belong
# to the same agentic chain (C_{i+1}'s prompt == C_i's prompt + response).
# ---------------------------------------------------------------------------


def _flatten_message_content(content: Any) -> str:
    """Extract text from a message content field (string or content-part array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content) if content is not None else ""


def _expand_messages_for_grouping(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    if role != "assistant" or not message.get("tool_calls"):
        return [message]

    expanded: list[dict[str, Any]] = []
    content = message.get("content")
    if content not in (None, "", []):
        expanded.append({"role": role, "content": content})
    expanded.append(
        {"role": role, "content": None, "tool_calls": message.get("tool_calls")}
    )
    return expanded


def _is_grouping_noise_message(message: dict[str, Any]) -> bool:
    role = message.get("role")
    if role in ["tool"]:
        return True
    if role == "assistant" and message.get("tool_calls"):
        return False
    content = _flatten_message_content(message.get("content")).strip()
    if role == "assistant" and not content and not message.get("tool_calls"):
        return True
    return False


def _normalize_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten a message list into a deterministic key string.

    Format: ``role:content<SEP>role:content<SEP>...``
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            content = ""
        else:
            content = _flatten_message_content(msg.get("content"))
        parts.append(f"{role}:{content}")
    return "<SEP>".join(parts)


def _grouping_key(messages: list[dict[str, Any]]) -> str:
    """Normalize the structural conversation context used for chaining.

    Tool-result messages are omitted because they are harness artifacts that
    appear between assistant turns in the next request prompt.
    """
    return _normalize_messages(
        [
            expanded_message
            for message in messages
            for expanded_message in _expand_messages_for_grouping(message)
            if not _is_grouping_noise_message(expanded_message)
        ],
    )


class PrefixMergingBuilder(BaseTrajectoryBuilder):
    """Rebuild a chain's merged token stream using raw + canonical-interstitial.

    Parameters
    ----------
    end_of_turn_token_id:
        Explicit end-of-turn (EOT) token id used to locate the
        canonical-tail split between the prior assistant body and the
        interstitial.  When None (default), the builder auto-detects it
        from the last token of the first completion with a natural stop
        reason.  For Qwen / ChatML templates this is the
        ``<|im_end|>`` token id.
    """

    def __init__(
        self,
        *,
        end_of_turn_token_id: int | None = None,
    ) -> None:
        self._configured_eot_id = end_of_turn_token_id

    async def build(self, session: CompletionSession) -> Trajectory:
        if not session.completions:
            return Trajectory(
                status="ERROR",
                metadata={
                    "builder": "prefix_merging",
                    "session_id": session.session_id,
                    "task_metadata": dict(session.metadata),
                    "record_count": 0,
                    **_top_level_scheduler_metadata(session.metadata),
                },
                traces=[],
                error="no completions",
            )

        chains: list[list[CompletionRecord]] = []
        waiting_chains: dict[str, deque[int]] = defaultdict(deque)

        for completion in session.completions:
            trace = build_trace_from_completion(completion)
            prompt_key = _grouping_key(trace.prompt_messages)
            chain_idx = self._pop_compatible_chain(
                prompt_key=prompt_key,
                prompt_ids=trace.prompt_ids,
                chains=chains,
                waiting_chains=waiting_chains,
            )

            if chain_idx is not None:
                chains[chain_idx].append(completion)
            else:
                chain_idx = len(chains)
                chains.append([completion])

            next_key = _grouping_key(trace.prompt_messages + trace.response_messages)
            waiting_chains[next_key].append(chain_idx)

        stats: dict[str, int] = {
            "chains_total": len(chains),
            "chains_reconstructed_full": 0,
            "chains_reconstructed_truncated": 0,
            "completions_total": len(session.completions),
            "completions_merged": 0,
        }
        final_traces = [self._finalize_chain(chain, stats) for chain in chains]

        return Trajectory(
            status="COMPLETED",
            metadata={
                "builder": "prefix_merging",
                "session_id": session.session_id,
                "task_id": session.task_id,
                "api_type": session.api_type,
                "model_requested": session.model_requested,
                "model_used": session.model_used,
                "record_count": len(session.completions),
                "task_metadata": dict(session.metadata),
                "trace_count": len(chains),
                "reconstruction_stats": stats,
                **_top_level_scheduler_metadata(session.metadata),
            },
            traces=final_traces,
        )

    # ------------------------------------------------------------------
    # Chain finalization
    # ------------------------------------------------------------------

    def _finalize_chain(
        self,
        chain: list[CompletionRecord],
        stats: dict[str, int],
    ) -> Trace:
        # Everything in C_1.prompt_ids is the non-trainable
        # prompt; C_1.response_ids plus every subsequent raw response +
        # canonical interstitial becomes the trainable response.  No role-shape
        # constraint on the initial conversation — a harness preamble like
        # codex's [system, user, user, assistant, tool, ...] is treated as
        # static context.
        first_trace = build_trace_from_completion(chain[0])
        eot_id = self._resolve_eot_id(chain)

        prompt_ids = list(first_trace.prompt_ids)
        stream_ids: list[int] = list(prompt_ids)
        response_slots: list[dict[str, Any] | None] = []
        loss_mask: list[int] = []
        response_messages: list[dict[str, Any]] = []

        # Track the canonical prompt_ids of the most recently merged
        # completion — used for the canonical-vs-canonical prefix check.
        prev_prompt_ids: list[int] = list(first_trace.prompt_ids)
        prev_raw_response: list[int] = list(first_trace.response_ids)

        # Running count of messages consumed = prompt_messages + all response_messages emitted.
        msg_acc = len(first_trace.prompt_messages)

        self._append_response_tokens(first_trace, stream_ids, response_slots, loss_mask)
        response_messages.extend(deepcopy(m) for m in first_trace.response_messages)
        msg_acc += len(first_trace.response_messages)
        kept = 1

        for i in range(1, len(chain)):
            Ci_trace = build_trace_from_completion(chain[i])
            Ci_prompt_ids = list(Ci_trace.prompt_ids)

            # Canonical-vs-canonical prefix check: both sides are server-side
            # tokenizations of the same message prefix — matches reliably
            # unless the harness rewrote prior messages.
            if (
                len(Ci_prompt_ids) < len(prev_prompt_ids)
                or Ci_prompt_ids[: len(prev_prompt_ids)] != prev_prompt_ids
            ):
                logger.debug(
                    "prefix_merging: canonical prefix break at step %d/%d",
                    i,
                    len(chain),
                )
                break

            # canonical_tail = canonical tokens for [prev assistant msg + new interstitials].
            canonical_tail = Ci_prompt_ids[len(prev_prompt_ids):]
            interstitial = self._slice_interstitial(
                canonical_tail=canonical_tail,
                prev_raw_response=prev_raw_response,
                eot_id=eot_id,
            )
            if interstitial is None:
                logger.debug(
                    "prefix_merging: interstitial split failed at step %d/%d "
                    "(eot_id=%r, tail_len=%d)",
                    i,
                    len(chain),
                    eot_id,
                    len(canonical_tail),
                )
                break

            if interstitial:
                stream_ids.extend(interstitial)
                response_slots.extend([None] * len(interstitial))
                loss_mask.extend([0] * len(interstitial))

            # Message-level interstitial bookkeeping.
            if len(Ci_trace.prompt_messages) > msg_acc:
                interstitial_msgs = Ci_trace.prompt_messages[msg_acc:]
                response_messages.extend(deepcopy(m) for m in interstitial_msgs)
                msg_acc += len(interstitial_msgs)

            self._append_response_tokens(Ci_trace, stream_ids, response_slots, loss_mask)
            response_messages.extend(deepcopy(m) for m in Ci_trace.response_messages)
            msg_acc += len(Ci_trace.response_messages)

            prev_prompt_ids = Ci_prompt_ids
            prev_raw_response = list(Ci_trace.response_ids)
            kept += 1

        stats["completions_merged"] += kept
        if kept == len(chain):
            stats["chains_reconstructed_full"] += 1
        else:
            stats["chains_reconstructed_truncated"] += 1

        response_ids = stream_ids[len(prompt_ids):]
        response_logprobs = self._finalize_logprobs(response_slots, response_ids)
        last_kept_trace = build_trace_from_completion(chain[kept - 1])

        return Trace(
            prompt_ids=prompt_ids,
            response_ids=response_ids,
            loss_mask=loss_mask,
            prompt_messages=[deepcopy(m) for m in first_trace.prompt_messages],
            response_messages=response_messages,
            tools=deepcopy(first_trace.tools),
            finish_reason=last_kept_trace.finish_reason,
            response_logprobs=response_logprobs,
            metadata=self._chain_metadata(chain[:kept]),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_eot_id(self, chain: list[CompletionRecord]) -> int | None:
        """Return configured EOT id, else auto-detect from the chain.

        Auto-detection uses the last token of the first completion whose
        ``finish_reason`` indicates the model emitted the natural stop
        marker itself (stop / tool_calls / stop_sequence).
        """
        if self._configured_eot_id is not None:
            return self._configured_eot_id
        for completion in chain:
            trace = build_trace_from_completion(completion)
            if (
                trace.finish_reason in _NATURAL_STOP_REASONS
                and trace.response_ids
            ):
                return trace.response_ids[-1]
        return None

    @staticmethod
    def _slice_interstitial(
        *,
        canonical_tail: list[int],
        prev_raw_response: list[int],
        eot_id: int | None,
    ) -> list[int] | None:
        """Extract the canonical interstitial from C_{i+1}'s prompt tail.

        ``canonical_tail`` = canonical tokens for [prev assistant msg +
        harness-inserted messages + generation-prompt glue].  The first
        occurrence of ``eot_id`` marks the end of the prev assistant
        body; everything after is interstitial.

        If ``prev_raw_response`` already ends with ``eot_id`` (natural
        stop / tool_calls), skip it in the canonical tail to avoid
        duplication; otherwise (truncation) include it so the stream
        still closes the assistant turn.

        Returns None if ``eot_id`` is unknown or not present — caller
        should treat this as a break.
        """
        if eot_id is None:
            return None
        try:
            k = canonical_tail.index(eot_id)
        except ValueError:
            return None
        if prev_raw_response and prev_raw_response[-1] == eot_id:
            return canonical_tail[k + 1 :]
        return canonical_tail[k:]

    @staticmethod
    def _append_response_tokens(
        trace: Trace,
        stream_ids: list[int],
        response_slots: list[dict[str, Any] | None],
        loss_mask: list[int],
    ) -> None:
        """Append a completion's response_ids and parallel logprob slots."""
        response_ids = list(trace.response_ids)
        stream_ids.extend(response_ids)
        trace_loss_mask = list(trace.loss_mask) or [1] * len(response_ids)
        if len(trace_loss_mask) != len(response_ids):
            raise ValueError("trace loss_mask length must match response_ids length")
        loss_mask.extend(trace_loss_mask)
        logprobs = trace.response_logprobs or []
        for pos in range(len(response_ids)):
            entry = logprobs[pos] if pos < len(logprobs) else None
            response_slots.append(deepcopy(entry) if isinstance(entry, dict) else None)

    @staticmethod
    def _finalize_logprobs(
        slots: list[dict[str, Any] | None],
        response_ids: list[int],
    ) -> list[dict[str, Any]] | None:
        if not any(slot is not None for slot in slots):
            return None
        return [
            slot if slot is not None else {"token_id": response_ids[i], "logprob": 0.0}
            for i, slot in enumerate(slots)
        ]

    @staticmethod
    def _chain_metadata(chain: list[CompletionRecord]) -> dict[str, Any]:
        completion_metadata = [dict(completion.metadata) for completion in chain]
        merged = dict(completion_metadata[0]) if completion_metadata else {}
        merged["completion_metadata"] = completion_metadata
        return merged

    @staticmethod
    def _pop_compatible_chain(
        *,
        prompt_key: str,
        prompt_ids: list[int],
        chains: list[list[CompletionRecord]],
        waiting_chains: dict[str, deque[int]],
    ) -> int | None:
        """Pop a waiting chain that matches both at message-key and token levels.

        The message-level key (produced by ``_grouping_key``) is only a
        *necessary* condition for joining a chain.  Its normalization drops
        tool messages and empty/``<think>`` assistants — both of which can
        hide genuine token-level divergence (cache-control shifts, tools
        schema rewrites, ``<system-reminder>`` injections).

        The *sufficient* condition is the strict append-only token-prefix
        invariant: ``C_{k+1}.prompt_ids`` must start with ``C_k.prompt_ids``.
        Enforcing this at chain-join time means a completion whose raw
        tokenization diverges from the waiting chain's tail starts its own
        new chain, instead of being silently appended (only to be dropped
        later in finalization).

        Scans candidates in FIFO order; returns the first compatible index
        and pops it.  Returns None if no candidate passes the token check.
        """
        queue = waiting_chains.get(prompt_key)
        if not queue:
            return None
        for pos, chain_idx in enumerate(queue):
            last_trace = build_trace_from_completion(chains[chain_idx][-1])
            last_pids = last_trace.prompt_ids
            if (
                not prompt_ids
                or not last_pids
                or len(prompt_ids) < len(last_pids)
                or prompt_ids[: len(last_pids)] != last_pids
            ):
                continue
            del queue[pos]
            if not queue:
                waiting_chains.pop(prompt_key, None)
            return chain_idx
        return None


def _top_level_scheduler_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = {"group_id", "policy_version", "rollout_step"}
    return {key: metadata[key] for key in keys if key in metadata}
