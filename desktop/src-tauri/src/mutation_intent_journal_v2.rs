use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

use super::{
    is_digest_v2, is_valid_opaque_id_v2, parse_utc_timestamp_v2, HostResult, NativeHostError,
    MAX_JAVASCRIPT_SAFE_INTEGER,
};

pub(crate) const MUTATION_INTENT_JOURNAL_MAX_BYTES: usize = 1024 * 1024;
const MUTATION_INTENT_MAX_ENTRY_BYTES: usize = 64 * 1024;
const MUTATION_INTENT_MAX_ENTRIES: usize = 16;
const MUTATION_INTENT_MAX_COMPLETED_OPERATIONS: usize = 2;
const MUTATION_INTENT_MAX_ACTION_BYTES: usize = 256;
const MUTATION_INTENT_MAX_SCOPE_BYTES: usize = 512;

const MUTATION_KINDS: &[&str] = &[
    "ssh_catalog_rescan",
    "profile_create",
    "profile_update",
    "profile_delete",
    "profile_rebind",
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "native_workspace_select",
    "native_workspace_cancel",
    "native_workspace_settle",
    "project_create",
    "project_update",
    "project_activate",
    "project_validate",
    "task_submit",
    "task_cancel",
    "task_retry",
    "transition_retry",
    "transition_replace",
    "transition_abandon",
    "service_restart",
    "diagnostic_create",
    "cache_cleanup",
];

const FORBIDDEN_VALUE_NAMES: &[&str] = &[
    "token",
    "password",
    "credential",
    "environment",
    "command",
    "host_path",
    "core_url",
    "secret_ref",
];

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PendingMutationJournalV2 {
    schema_version: String,
    revision: u64,
    entries: Vec<PendingMutationIntentV2>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct PendingMutationIntentV2 {
    action_id: String,
    mutation_kind: String,
    resource_scope: String,
    request_sha256: String,
    authority_sha256: String,
    provider_stream_instance: String,
    provider_stream_epoch: u64,
    chain_step: String,
    accepted_operation_id: Option<String>,
    completed_operation_ids: Vec<String>,
    state: String,
    created_at: String,
    updated_at: String,
}

pub(crate) fn validate_mutation_intent_journal_value(
    value: &str,
) -> HostResult<PendingMutationJournalV2> {
    if value.len() > MUTATION_INTENT_JOURNAL_MAX_BYTES {
        return Err(too_large_error());
    }
    let document: PendingMutationJournalV2 =
        serde_json::from_str(value).map_err(|_| invalid_error())?;
    validate_document(&document)?;
    Ok(document)
}

pub(crate) fn validate_mutation_intent_journal_transition(
    expected_value: Option<&str>,
    new_value: Option<&str>,
) -> HostResult<()> {
    let expected = expected_value
        .map(validate_mutation_intent_journal_value)
        .transpose()?;
    let new = new_value
        .map(validate_mutation_intent_journal_value)
        .transpose()?;
    match (expected.as_ref(), new.as_ref()) {
        (None, None) => Ok(()),
        (None, Some(new)) => {
            if new.revision != 1 || new.entries.iter().any(|entry| entry.state != "reserved") {
                return Err(invalid_error());
            }
            Ok(())
        }
        (Some(expected), None) => {
            if expected
                .entries
                .iter()
                .any(|entry| entry.state == "accepted")
            {
                return Err(invalid_error());
            }
            Ok(())
        }
        (Some(expected), Some(new)) => validate_document_transition(expected, new),
    }
}

fn validate_document(document: &PendingMutationJournalV2) -> HostResult<()> {
    if document.schema_version != "2"
        || document.revision == 0
        || document.revision > MAX_JAVASCRIPT_SAFE_INTEGER
        || document.entries.len() > MUTATION_INTENT_MAX_ENTRIES
        || serde_json::to_vec(document)
            .map_err(|_| invalid_error())?
            .len()
            > MUTATION_INTENT_JOURNAL_MAX_BYTES
    {
        return Err(invalid_error());
    }

    let mut action_ids = HashSet::new();
    let mut logical_intents = HashSet::new();
    let mut operation_ids = HashSet::new();
    for entry in &document.entries {
        validate_entry(entry)?;
        if !action_ids.insert(entry.action_id.as_str())
            || !logical_intents.insert((
                entry.mutation_kind.as_str(),
                entry.resource_scope.as_str(),
                entry.request_sha256.as_str(),
                entry.authority_sha256.as_str(),
                entry.provider_stream_instance.as_str(),
                entry.provider_stream_epoch,
            ))
        {
            return Err(invalid_error());
        }
        if entry
            .accepted_operation_id
            .as_deref()
            .is_some_and(|operation_id| !operation_ids.insert(operation_id))
            || entry
                .completed_operation_ids
                .iter()
                .any(|operation_id| !operation_ids.insert(operation_id.as_str()))
        {
            return Err(invalid_error());
        }
    }
    Ok(())
}

fn validate_entry(entry: &PendingMutationIntentV2) -> HostResult<()> {
    let completed = &entry.completed_operation_ids;
    let unique_completed = completed.iter().collect::<HashSet<_>>();
    let created_at = parse_utc_timestamp_v2(&entry.created_at).ok_or_else(invalid_error)?;
    let updated_at = parse_utc_timestamp_v2(&entry.updated_at).ok_or_else(invalid_error)?;
    if !(16..=MUTATION_INTENT_MAX_ACTION_BYTES).contains(&entry.action_id.len())
        || !is_safe_text(&entry.action_id, MUTATION_INTENT_MAX_ACTION_BYTES)
        || !MUTATION_KINDS.contains(&entry.mutation_kind.as_str())
        || !is_safe_scope(&entry.resource_scope)
        || !is_digest_v2(&entry.request_sha256)
        || !is_digest_v2(&entry.authority_sha256)
        || !is_valid_journal_operation_id(&entry.provider_stream_instance)
        || entry.provider_stream_epoch == 0
        || entry.provider_stream_epoch > MAX_JAVASCRIPT_SAFE_INTEGER
        || !matches!(
            entry.chain_step.as_str(),
            "single" | "native_workspace_prepare" | "project_create"
        )
        || !matches!(
            entry.state.as_str(),
            "reserved" | "accepted" | "terminal_observed" | "deterministic_rejection"
        )
        || completed.len() > MUTATION_INTENT_MAX_COMPLETED_OPERATIONS
        || completed.len() != unique_completed.len()
        || completed
            .iter()
            .any(|operation_id| !is_valid_journal_operation_id(operation_id))
        || entry
            .accepted_operation_id
            .as_deref()
            .is_some_and(|operation_id| {
                !is_valid_journal_operation_id(operation_id)
                    || completed
                        .iter()
                        .any(|completed_id| completed_id == operation_id)
            })
        || created_at > updated_at
        || serde_json::to_vec(entry)
            .map_err(|_| invalid_error())?
            .len()
            > MUTATION_INTENT_MAX_ENTRY_BYTES
    {
        return Err(invalid_error());
    }

    let has_current_operation = entry.accepted_operation_id.is_some();
    if has_current_operation != matches!(entry.state.as_str(), "accepted" | "terminal_observed") {
        return Err(invalid_error());
    }
    match entry.chain_step.as_str() {
        "single" if !completed.is_empty() => return Err(invalid_error()),
        "native_workspace_prepare"
            if entry.mutation_kind != "project_create" || !completed.is_empty() =>
        {
            return Err(invalid_error());
        }
        "project_create" if entry.mutation_kind != "project_create" || completed.len() != 1 => {
            return Err(invalid_error());
        }
        _ => {}
    }
    if entry.mutation_kind != "project_create" && entry.chain_step != "single" {
        return Err(invalid_error());
    }
    Ok(())
}

fn validate_document_transition(
    expected: &PendingMutationJournalV2,
    new: &PendingMutationJournalV2,
) -> HostResult<()> {
    if new.revision != expected.revision.checked_add(1).ok_or_else(invalid_error)? {
        return Err(invalid_error());
    }
    let expected_entries = expected
        .entries
        .iter()
        .map(|entry| (entry.action_id.as_str(), entry))
        .collect::<HashMap<_, _>>();
    let new_entries = new
        .entries
        .iter()
        .map(|entry| (entry.action_id.as_str(), entry))
        .collect::<HashMap<_, _>>();

    for entry in &new.entries {
        match expected_entries.get(entry.action_id.as_str()) {
            Some(previous) => validate_entry_transition(previous, entry)?,
            None if entry.state == "reserved" && entry.created_at == entry.updated_at => {}
            None => return Err(invalid_error()),
        }
    }
    for previous in &expected.entries {
        if !new_entries.contains_key(previous.action_id.as_str()) && previous.state == "accepted" {
            return Err(invalid_error());
        }
    }
    Ok(())
}

fn validate_entry_transition(
    previous: &PendingMutationIntentV2,
    next: &PendingMutationIntentV2,
) -> HostResult<()> {
    if previous.action_id != next.action_id
        || previous.mutation_kind != next.mutation_kind
        || previous.resource_scope != next.resource_scope
        || previous.request_sha256 != next.request_sha256
        || previous.authority_sha256 != next.authority_sha256
        || previous.provider_stream_instance != next.provider_stream_instance
        || previous.provider_stream_epoch != next.provider_stream_epoch
        || previous.created_at != next.created_at
        || parse_utc_timestamp_v2(&next.updated_at) < parse_utc_timestamp_v2(&previous.updated_at)
    {
        return Err(invalid_error());
    }

    if is_native_project_chain_advance(previous, next) {
        return Ok(());
    }
    if previous.chain_step != next.chain_step
        || previous.completed_operation_ids != next.completed_operation_ids
    {
        return Err(invalid_error());
    }
    match (previous.state.as_str(), next.state.as_str()) {
        ("reserved", "reserved") | ("deterministic_rejection", "deterministic_rejection") => {
            if previous.accepted_operation_id != next.accepted_operation_id {
                return Err(invalid_error());
            }
        }
        ("reserved", "accepted") => {
            if previous.accepted_operation_id.is_some() || next.accepted_operation_id.is_none() {
                return Err(invalid_error());
            }
        }
        ("reserved", "deterministic_rejection") => {
            if next.accepted_operation_id.is_some() {
                return Err(invalid_error());
            }
        }
        ("accepted", "accepted") | ("accepted", "terminal_observed")
            if previous.accepted_operation_id == next.accepted_operation_id => {}
        ("terminal_observed", "terminal_observed")
            if previous.accepted_operation_id == next.accepted_operation_id => {}
        _ => return Err(invalid_error()),
    }
    Ok(())
}

fn is_native_project_chain_advance(
    previous: &PendingMutationIntentV2,
    next: &PendingMutationIntentV2,
) -> bool {
    previous.mutation_kind == "project_create"
        && previous.chain_step == "native_workspace_prepare"
        && previous.state == "terminal_observed"
        && previous.accepted_operation_id.is_some()
        && previous.completed_operation_ids.is_empty()
        && next.chain_step == "project_create"
        && next.state == "reserved"
        && next.accepted_operation_id.is_none()
        && next.completed_operation_ids == [previous.accepted_operation_id.clone().unwrap()]
}

fn is_valid_journal_operation_id(value: &str) -> bool {
    is_valid_opaque_id_v2(value) && !contains_forbidden_value_name(value)
}

fn is_safe_scope(value: &str) -> bool {
    is_safe_text(value, MUTATION_INTENT_MAX_SCOPE_BYTES)
        && !value.starts_with('/')
        && !value.starts_with('\\')
        && !value.contains("://")
}

fn is_safe_text(value: &str, max_bytes: usize) -> bool {
    !value.is_empty()
        && value.len() <= max_bytes
        && value.trim() == value
        && !value.chars().any(|character| character.is_control())
        && !contains_forbidden_value_name(value)
}

fn contains_forbidden_value_name(value: &str) -> bool {
    let lowercase = value.to_ascii_lowercase();
    FORBIDDEN_VALUE_NAMES
        .iter()
        .any(|forbidden| lowercase.contains(forbidden))
}

fn invalid_error() -> NativeHostError {
    NativeHostError::new(
        "mutation_intent_journal_invalid",
        "OpenEvo Desktop rejected invalid saved mutation retry state.",
    )
}

fn too_large_error() -> NativeHostError {
    NativeHostError::new(
        "mutation_intent_journal_too_large",
        "OpenEvo Desktop could not save mutation retry state because it is too large.",
    )
}

#[cfg(test)]
mod tests {
    use serde_json::{json, Value};

    use super::*;

    const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const OTHER_DIGEST: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn entry(action_id: &str) -> Value {
        json!({
            "action_id": action_id,
            "mutation_kind": "profile_connect",
            "resource_scope": "profile:profile-lab",
            "request_sha256": DIGEST,
            "authority_sha256": OTHER_DIGEST,
            "provider_stream_instance": "provider-instance-1",
            "provider_stream_epoch": 1,
            "chain_step": "single",
            "accepted_operation_id": null,
            "completed_operation_ids": [],
            "state": "reserved",
            "created_at": "2026-07-27T08:00:00.000000001Z",
            "updated_at": "2026-07-27T08:00:00.000000001Z"
        })
    }

    fn document(revision: u64, entries: Vec<Value>) -> Value {
        json!({"schema_version": "2", "revision": revision, "entries": entries})
    }

    fn parse(value: Value) -> HostResult<PendingMutationJournalV2> {
        validate_mutation_intent_journal_value(&serde_json::to_string(&value).unwrap())
    }

    #[test]
    fn mutation_intent_journal_v2_strictly_decodes_root_and_entries() {
        assert_eq!(
            parse(document(1, vec![entry("mutation-action-0001")]))
                .unwrap()
                .revision,
            1
        );

        let mut open_root = document(1, vec![]);
        open_root["unexpected"] = json!(true);
        assert_eq!(
            parse(open_root).unwrap_err().code,
            "mutation_intent_journal_invalid"
        );
        let mut open_entry = entry("mutation-action-0001");
        open_entry["password"] = json!("canary");
        assert_eq!(
            parse(document(1, vec![open_entry])).unwrap_err().code,
            "mutation_intent_journal_invalid"
        );
        assert!(validate_mutation_intent_journal_value("{not-json").is_err());
    }

    #[test]
    fn mutation_intent_journal_v2_rejects_invalid_identity_time_and_secret_names() {
        let invalid_fields = [
            ("action_id", json!("short")),
            ("mutation_kind", json!("unknown_mutation")),
            ("resource_scope", json!("profile:password")),
            ("resource_scope", json!("/Users/researcher/private")),
            ("request_sha256", json!("A".repeat(64))),
            ("authority_sha256", json!("not-a-digest")),
            ("provider_stream_instance", json!("provider/instance")),
            ("provider_stream_epoch", json!(0)),
            ("provider_stream_epoch", json!(9_007_199_254_740_992_u64)),
            ("chain_step", json!("other")),
            ("state", json!("unknown")),
            ("created_at", json!("2026-02-31T08:00:00Z")),
            ("updated_at", json!("2026-07-27T07:59:59Z")),
        ];
        for (field, value) in invalid_fields {
            let mut candidate = entry("mutation-action-0001");
            candidate[field] = value;
            assert!(
                parse(document(1, vec![candidate])).is_err(),
                "accepted invalid {field}"
            );
        }
    }

    #[test]
    fn mutation_intent_journal_v2_rejects_duplicate_action_logical_and_operation_ids() {
        let first = entry("mutation-action-0001");
        let mut duplicate_action = first.clone();
        duplicate_action["request_sha256"] = json!(OTHER_DIGEST);
        assert!(parse(document(1, vec![first.clone(), duplicate_action])).is_err());

        let mut duplicate_logical = first.clone();
        duplicate_logical["action_id"] = json!("mutation-action-0002");
        assert!(parse(document(1, vec![first.clone(), duplicate_logical])).is_err());

        let mut accepted = first.clone();
        accepted["state"] = json!("accepted");
        accepted["accepted_operation_id"] = json!("operation-shared");
        let mut other = entry("mutation-action-0002");
        other["resource_scope"] = json!("profile:profile-other");
        other["state"] = json!("accepted");
        other["accepted_operation_id"] = json!("operation-shared");
        assert!(parse(document(1, vec![accepted, other])).is_err());
    }

    #[test]
    fn mutation_intent_journal_v2_enforces_capacity_and_byte_budgets() {
        let entries = (0..17)
            .map(|index| {
                let mut value = entry(&format!("mutation-action-{index:04}"));
                value["resource_scope"] = json!(format!("profile:profile-{index:04}"));
                value
            })
            .collect();
        assert!(parse(document(1, entries)).is_err());

        let oversized = format!(
            "{}{}",
            serde_json::to_string(&document(1, vec![])).unwrap(),
            " ".repeat(1024 * 1024)
        );
        assert!(validate_mutation_intent_journal_value(&oversized).is_err());
    }

    #[test]
    fn mutation_intent_journal_v2_binds_state_chain_and_current_operation() {
        for (state, operation, accepted) in [
            ("reserved", Some("operation-1"), false),
            ("deterministic_rejection", Some("operation-1"), false),
            ("accepted", None, false),
            ("terminal_observed", None, false),
            ("accepted", Some("operation-1"), true),
            ("terminal_observed", Some("operation-1"), true),
        ] {
            let mut value = entry("mutation-action-0001");
            value["state"] = json!(state);
            value["accepted_operation_id"] = operation.map_or(Value::Null, |item| json!(item));
            assert_eq!(
                parse(document(1, vec![value])).is_ok(),
                accepted,
                "state {state}"
            );
        }

        let mut project_step = entry("mutation-action-0001");
        project_step["mutation_kind"] = json!("project_create");
        project_step["chain_step"] = json!("project_create");
        assert!(parse(document(1, vec![project_step.clone()])).is_err());
        project_step["completed_operation_ids"] = json!(["native-operation-1"]);
        assert!(parse(document(1, vec![project_step])).is_ok());
    }

    #[test]
    fn mutation_intent_journal_v2_allows_only_monotonic_cas_transitions() {
        let reserved = document(1, vec![entry("mutation-action-0001")]);
        let mut accepted_entry = entry("mutation-action-0001");
        accepted_entry["state"] = json!("accepted");
        accepted_entry["accepted_operation_id"] = json!("operation-1");
        accepted_entry["updated_at"] = json!("2026-07-27T08:00:01Z");
        let accepted = document(2, vec![accepted_entry.clone()]);
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&reserved).unwrap()),
            Some(&serde_json::to_string(&accepted).unwrap()),
        )
        .is_ok());

        let stale_revision = document(1, vec![accepted_entry.clone()]);
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&reserved).unwrap()),
            Some(&serde_json::to_string(&stale_revision).unwrap()),
        )
        .is_err());
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&accepted).unwrap()),
            None,
        )
        .is_err());

        accepted_entry["state"] = json!("terminal_observed");
        accepted_entry["updated_at"] = json!("2026-07-27T08:00:02Z");
        let terminal = document(3, vec![accepted_entry]);
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&accepted).unwrap()),
            Some(&serde_json::to_string(&terminal).unwrap()),
        )
        .is_ok());
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&terminal).unwrap()),
            None,
        )
        .is_ok());
    }

    #[test]
    fn mutation_intent_journal_v2_accepts_only_the_native_project_chain_advance() {
        let mut terminal_entry = entry("mutation-action-0001");
        terminal_entry["mutation_kind"] = json!("project_create");
        terminal_entry["chain_step"] = json!("native_workspace_prepare");
        terminal_entry["state"] = json!("terminal_observed");
        terminal_entry["accepted_operation_id"] = json!("native-operation-1");
        let terminal = document(1, vec![terminal_entry.clone()]);

        let mut advanced_entry = terminal_entry;
        advanced_entry["chain_step"] = json!("project_create");
        advanced_entry["state"] = json!("reserved");
        advanced_entry["accepted_operation_id"] = Value::Null;
        advanced_entry["completed_operation_ids"] = json!(["native-operation-1"]);
        advanced_entry["updated_at"] = json!("2026-07-27T08:00:01Z");
        let advanced = document(2, vec![advanced_entry]);
        assert!(validate_mutation_intent_journal_transition(
            Some(&serde_json::to_string(&terminal).unwrap()),
            Some(&serde_json::to_string(&advanced).unwrap()),
        )
        .is_ok());
    }
}
