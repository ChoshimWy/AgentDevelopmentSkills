use super::LifecycleError;
use agent_contracts::{MAX_CONTRACT_JSON_BYTES, canonical_json};
use std::cmp::Ordering;
use std::fmt::Write as _;
use toml::{Table, Value};

const ROOT_SCALAR_PRIORITY: &[&str] = &[
    "model",
    "image_model",
    "model_reasoning_effort",
    "plan_mode_reasoning_effort",
    "service_tier",
    "model_instructions_file",
];
const ROOT_TABLE_PRIORITY: &[&str] = &[
    "features",
    "agents",
    "projects",
    "mcp_servers",
    "notice",
    "tui",
    "plugins",
    "marketplaces",
];
const LOCAL_RUNTIME_KEYS: &[&str] = &[
    "model",
    "model_reasoning_effort",
    "plan_mode_reasoning_effort",
    "model_verbosity",
    "service_tier",
];
#[cfg(windows)]
const OUTPUT_NEWLINE: &str = "\r\n";
#[cfg(not(windows))]
const OUTPUT_NEWLINE: &str = "\n";

/// Render Codex shared configuration without executing the installed Python
/// package script.
///
/// This preserves the current `sync_codex_shared_config.py` merge and ordering
/// contract: local runtime choices survive, shared named children replace only
/// names owned by the shared config, unmanaged plugins are retained but
/// disabled, retired global MCP defaults are removed only on exact equality,
/// and `model_instructions_file` is bound to the supplied path.
///
/// # Errors
/// Fails when either input is oversized or invalid UTF-8 TOML, the TOML root is
/// not a table, or the compatibility formatter encounters a value shape that
/// the Python source contract also does not support.
pub fn render_codex_config(
    existing: Option<&[u8]>,
    shared: &[u8],
    agents_path: &str,
) -> Result<Vec<u8>, LifecycleError> {
    let mut existing = parse_root(existing.unwrap_or_default(), "existing Codex config")?;
    let shared = parse_root(shared, "shared Codex config")?;
    merge_shared_config(&mut existing, &shared, agents_path)?;
    Ok(dump_toml(&existing)?.into_bytes())
}

fn parse_root(bytes: &[u8], label: &str) -> Result<Table, LifecycleError> {
    if bytes.len() > MAX_CONTRACT_JSON_BYTES {
        return invalid(format!(
            "{label} has more than {MAX_CONTRACT_JSON_BYTES} bytes"
        ));
    }
    let text = std::str::from_utf8(bytes)
        .map_err(|_| invalid_error(format!("{label} must be valid UTF-8 TOML")))?;
    if text.trim().is_empty() {
        return Ok(Table::new());
    }
    let value = text
        .parse::<Value>()
        .map_err(|_| invalid_error(format!("{label} must be valid UTF-8 TOML")))?;
    value
        .as_table()
        .cloned()
        .ok_or_else(|| invalid_error(format!("{label} root must be a TOML table")))
}

fn merge_shared_config(
    existing: &mut Table,
    shared: &Table,
    agents_path: &str,
) -> Result<(), LifecycleError> {
    let explicit_fast_mode = existing
        .get("features")
        .and_then(Value::as_table)
        .and_then(|features| features.get("fast_mode"))
        .and_then(Value::as_bool)
        == Some(true);
    if existing.get("service_tier").and_then(Value::as_str) == Some("fast") && !explicit_fast_mode {
        existing.remove("service_tier");
    }
    remove_retired_mcp_servers(existing)?;

    for (key, value) in shared {
        if LOCAL_RUNTIME_KEYS.contains(&key.as_str()) && existing.contains_key(key) {
            continue;
        }
        if key == "plugins"
            && let Value::Table(shared_plugins) = value
        {
            merge_plugins(existing, shared_plugins);
            continue;
        }
        if matches!(key.as_str(), "mcp_servers" | "plugins")
            && let Value::Table(shared_children) = value
        {
            replace_named_children(existing, key, shared_children);
            continue;
        }
        let merged = deep_overlay(existing.get(key), value);
        existing.insert(key.clone(), merged);
    }
    existing.insert(
        "model_instructions_file".to_owned(),
        Value::String(agents_path.to_owned()),
    );
    Ok(())
}

fn remove_retired_mcp_servers(existing: &mut Table) -> Result<(), LifecycleError> {
    let Some(Value::Table(mcp_servers)) = existing.get_mut("mcp_servers") else {
        return Ok(());
    };
    let retired = [
        (
            "codegraph",
            parse_inline_table(
                r#"command = "codegraph"
args = ["serve", "--mcp"]
"#,
            )?,
        ),
        (
            "openaiDeveloperDocs",
            parse_inline_table(
                r#"url = "https://developers.openai.com/mcp"
[tools.search_openai_docs]
approval_mode = "approve"
"#,
            )?,
        ),
        (
            "appleDeveloperDocs",
            parse_inline_table(
                r#"command = "npx"
args = ["-y", "@kimsungwhee/apple-docs-mcp@latest"]
"#,
            )?,
        ),
    ];
    for (name, value) in retired {
        if mcp_servers.get(name) == Some(&Value::Table(value)) {
            mcp_servers.remove(name);
        }
    }
    if mcp_servers.is_empty() {
        existing.remove("mcp_servers");
    }
    Ok(())
}

fn parse_inline_table(source: &str) -> Result<Table, LifecycleError> {
    source
        .parse::<Value>()
        .ok()
        .and_then(|value| value.as_table().cloned())
        .ok_or_else(|| invalid_error("native Codex config baseline is invalid"))
}

fn merge_plugins(existing: &mut Table, shared_plugins: &Table) {
    let mut merged = Table::new();
    if let Some(Value::Table(current)) = existing.get("plugins") {
        for (key, value) in current {
            let mut disabled = value.as_table().cloned().unwrap_or_default();
            disabled.insert("enabled".to_owned(), Value::Boolean(false));
            merged.insert(key.clone(), Value::Table(disabled));
        }
    }
    for (key, value) in shared_plugins {
        merged.insert(key.clone(), value.clone());
    }
    existing.insert("plugins".to_owned(), Value::Table(merged));
}

fn replace_named_children(existing: &mut Table, key: &str, shared: &Table) {
    let mut merged = existing
        .get(key)
        .and_then(Value::as_table)
        .cloned()
        .unwrap_or_default();
    for (child, value) in shared {
        merged.insert(child.clone(), value.clone());
    }
    existing.insert(key.to_owned(), Value::Table(merged));
}

fn deep_overlay(existing: Option<&Value>, shared: &Value) -> Value {
    if let (Some(Value::Table(existing)), Value::Table(shared)) = (existing, shared) {
        let mut merged = existing.clone();
        for (key, value) in shared {
            let value = deep_overlay(merged.get(key), value);
            merged.insert(key.clone(), value);
        }
        Value::Table(merged)
    } else {
        shared.clone()
    }
}

fn dump_toml(data: &Table) -> Result<String, LifecycleError> {
    let mut lines = Vec::new();
    for (key, value) in ordered_root_scalars(data) {
        lines.push(format!(
            "{} = {}",
            format_key_segment(key),
            format_value(value)?
        ));
    }

    if let Some(Value::Table(memories)) = data.get("memories") {
        let mut dotted = Vec::new();
        emit_dotted_assignments(&["memories"], memories, &mut dotted)?;
        if !dotted.is_empty() {
            if !lines.is_empty() {
                lines.push(String::new());
            }
            lines.extend(dotted);
        }
    }

    let root_tables = ordered_root_tables(data);
    if !root_tables.is_empty() {
        if !lines.is_empty() {
            lines.push(String::new());
        }
        for (index, (key, table)) in root_tables.into_iter().enumerate() {
            if index > 0 && lines.last().is_some_and(|line| !line.is_empty()) {
                lines.push(String::new());
            }
            emit_table(&[key], table, &mut lines)?;
        }
    }
    while lines.last().is_some_and(String::is_empty) {
        lines.pop();
    }
    lines.push(String::new());
    Ok(lines.join(OUTPUT_NEWLINE))
}

fn ordered_root_scalars(data: &Table) -> Vec<(&str, &Value)> {
    let mut values = data
        .iter()
        .filter(|(_, value)| !value.is_table())
        .map(|(key, value)| (key.as_str(), value))
        .collect::<Vec<_>>();
    values.sort_by(|left, right| priority_order(left.0, right.0, ROOT_SCALAR_PRIORITY, data));
    values
}

fn ordered_root_tables(data: &Table) -> Vec<(&str, &Table)> {
    let mut values = data
        .iter()
        .filter(|(key, value)| key.as_str() != "memories" && value.is_table())
        .map(|(key, value)| {
            (
                key.as_str(),
                value.as_table().expect("filtered TOML table value"),
            )
        })
        .collect::<Vec<_>>();
    values.sort_by(|left, right| priority_order(left.0, right.0, ROOT_TABLE_PRIORITY, data));
    values
}

fn priority_order(left: &str, right: &str, priority: &[&str], data: &Table) -> Ordering {
    let rank = |key: &str| {
        priority
            .iter()
            .position(|candidate| *candidate == key)
            .map_or_else(
                || {
                    (
                        1,
                        data.keys()
                            .position(|candidate| candidate == key)
                            .expect("ordered TOML key must remain present"),
                    )
                },
                |index| (0, index),
            )
    };
    rank(left).cmp(&rank(right))
}

fn emit_dotted_assignments(
    path: &[&str],
    mapping: &Table,
    lines: &mut Vec<String>,
) -> Result<(), LifecycleError> {
    for (key, value) in mapping {
        let mut next = path.to_vec();
        next.push(key);
        if let Value::Table(table) = value {
            emit_dotted_assignments(&next, table, lines)?;
        } else {
            lines.push(format!("{} = {}", format_path(&next), format_value(value)?));
        }
    }
    Ok(())
}

fn emit_table(
    path: &[&str],
    mapping: &Table,
    lines: &mut Vec<String>,
) -> Result<(), LifecycleError> {
    let scalars = mapping
        .iter()
        .filter(|(_, value)| !value.is_table() && !is_array_of_tables(value))
        .collect::<Vec<_>>();
    if !path.is_empty() && !scalars.is_empty() {
        lines.push(format!("[{}]", format_path(path)));
        for (key, value) in scalars {
            lines.push(format!(
                "{} = {}",
                format_key_segment(key),
                format_value(value)?
            ));
        }
        lines.push(String::new());
    }

    for (key, value) in mapping {
        if let Value::Table(table) = value {
            let mut next = path.to_vec();
            next.push(key);
            if lines.last().is_some_and(|line| !line.is_empty()) {
                lines.push(String::new());
            }
            emit_table(&next, table, lines)?;
        }
    }
    for (key, value) in mapping {
        if !is_array_of_tables(value) {
            continue;
        }
        let Value::Array(entries) = value else {
            unreachable!("array-of-tables predicate requires an array");
        };
        let mut next = path.to_vec();
        next.push(key);
        for entry in entries {
            if lines.last().is_some_and(|line| !line.is_empty()) {
                lines.push(String::new());
            }
            lines.push(format!("[[{}]]", format_path(&next)));
            let table = entry
                .as_table()
                .expect("array-of-tables predicate requires table entries");
            for (child, child_value) in table {
                if child_value.is_table() || is_array_of_tables(child_value) {
                    return invalid(format!(
                        "nested tables inside arrays of tables are unsupported at {}.{}",
                        format_path(&next),
                        child
                    ));
                }
                lines.push(format!(
                    "{} = {}",
                    format_key_segment(child),
                    format_value(child_value)?
                ));
            }
            lines.push(String::new());
        }
    }
    Ok(())
}

fn format_path(path: &[&str]) -> String {
    path.iter()
        .map(|segment| format_key_segment(segment))
        .collect::<Vec<_>>()
        .join(".")
}

fn format_key_segment(key: &str) -> String {
    if !key.is_empty()
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        key.to_owned()
    } else {
        python_json_string(key)
    }
}

fn format_value(value: &Value) -> Result<String, LifecycleError> {
    match value {
        Value::String(value) => Ok(python_json_string(value)),
        Value::Integer(value) => Ok(value.to_string()),
        Value::Float(value) if value.is_nan() => Ok("nan".to_owned()),
        Value::Float(value) if value.is_infinite() && value.is_sign_positive() => {
            Ok("inf".to_owned())
        }
        Value::Float(value) if value.is_infinite() => Ok("-inf".to_owned()),
        Value::Float(value) => {
            let mut encoded = canonical_json(&serde_json::Value::from(*value))?;
            if encoded.last() == Some(&b'\n') {
                encoded.pop();
            }
            String::from_utf8(encoded)
                .map_err(|_| invalid_error("native Codex config float is invalid"))
        }
        Value::Boolean(value) => Ok(value.to_string()),
        Value::Datetime(value) => Ok(format_datetime(value)),
        Value::Array(values) if !is_array_of_tables(value) => values
            .iter()
            .map(format_value)
            .collect::<Result<Vec<_>, _>>()
            .map(|values| format!("[{}]", values.join(", "))),
        Value::Array(_) | Value::Table(_) => {
            invalid("unsupported TOML value in native Codex config renderer")
        }
    }
}

fn is_array_of_tables(value: &Value) -> bool {
    matches!(value, Value::Array(values) if !values.is_empty() && values.iter().all(Value::is_table))
}

fn format_datetime(value: &toml::value::Datetime) -> String {
    let mut output = String::new();
    if let Some(date) = value.date {
        write!(output, "{:04}-{:02}-{:02}", date.year, date.month, date.day)
            .expect("writing to String cannot fail");
    }
    if let Some(time) = value.time {
        if value.date.is_some() {
            output.push('T');
        }
        write!(
            output,
            "{:02}:{:02}:{:02}",
            time.hour, time.minute, time.second
        )
        .expect("writing to String cannot fail");
        let microsecond = time.nanosecond / 1_000;
        if microsecond != 0 {
            write!(output, ".{microsecond:06}").expect("writing to String cannot fail");
        }
    }
    if let Some(offset) = value.offset {
        match offset {
            toml::value::Offset::Z | toml::value::Offset::Custom { minutes: 0 } => {
                output.push('Z');
            }
            toml::value::Offset::Custom { minutes } => {
                let sign = if minutes < 0 { '-' } else { '+' };
                let minutes = minutes.unsigned_abs();
                write!(output, "{sign}{:02}:{:02}", minutes / 60, minutes % 60)
                    .expect("writing to String cannot fail");
            }
        }
    }
    output
}

fn python_json_string(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{0008}' => output.push_str("\\b"),
            '\u{000c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{0020}'..='\u{007e}' => output.push(character),
            character if u32::from(character) <= 0xffff => {
                write!(output, "\\u{:04x}", u32::from(character))
                    .expect("writing to String cannot fail");
            }
            character => {
                let value = u32::from(character) - 0x1_0000;
                let high = 0xd800 + (value >> 10);
                let low = 0xdc00 + (value & 0x3ff);
                write!(output, "\\u{high:04x}\\u{low:04x}").expect("writing to String cannot fail");
            }
        }
    }
    output.push('"');
    output
}

fn invalid<T>(message: impl Into<String>) -> Result<T, LifecycleError> {
    Err(invalid_error(message))
}

fn invalid_error(message: impl Into<String>) -> LifecycleError {
    LifecycleError::Invalid(message.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn platform_newlines(value: &str) -> String {
        value.replace('\n', OUTPUT_NEWLINE)
    }

    #[test]
    fn merge_and_format_match_the_source_contract() {
        let existing = r#"
service_tier = "fast"
model = "local"
unicode = "中文"

[features]
legacy = true

[plugins.local]
enabled = true
path = "/tmp/local"

[mcp_servers.codegraph]
command = "codegraph"
args = ["serve", "--mcp"]
"#;
        let shared = r#"
model = "shared"
service_tier = "flex"

[features]
shared = true

[plugins.managed]
enabled = true

[mcp_servers.shared]
url = "https://example.com"
"#;
        let rendered = render_codex_config(
            Some(existing.as_bytes()),
            shared.as_bytes(),
            "/tmp/AGENTS.md",
        )
        .expect("render config");
        assert_eq!(
            String::from_utf8(rendered).expect("UTF-8 output"),
            platform_newlines(concat!(
                "model = \"local\"\n",
                "service_tier = \"flex\"\n",
                "model_instructions_file = \"/tmp/AGENTS.md\"\n",
                "unicode = \"\\u4e2d\\u6587\"\n",
                "\n",
                "[features]\n",
                "legacy = true\n",
                "shared = true\n",
                "\n",
                "[mcp_servers.shared]\n",
                "url = \"https://example.com\"\n",
                "\n",
                "[plugins.local]\n",
                "enabled = false\n",
                "path = \"/tmp/local\"\n",
                "\n",
                "[plugins.managed]\n",
                "enabled = true\n",
            ))
        );
    }

    #[test]
    fn formatter_supports_dotted_memories_arrays_and_datetime_values() {
        let shared = r#"
date = 2026-07-18
float = 1.0
escaped = "line\n😀"
memories.enabled = true

[[agents.entries]]
name = "one"

[[agents.entries]]
name = "two"
"#;
        let rendered =
            render_codex_config(None, shared.as_bytes(), "/tmp/AGENTS.md").expect("render config");
        assert_eq!(
            String::from_utf8(rendered).expect("UTF-8 output"),
            platform_newlines(concat!(
                "model_instructions_file = \"/tmp/AGENTS.md\"\n",
                "date = 2026-07-18\n",
                "float = 1.0\n",
                "escaped = \"line\\n\\ud83d\\ude00\"\n",
                "\n",
                "memories.enabled = true\n",
                "\n",
                "[[agents.entries]]\n",
                "name = \"one\"\n",
                "\n",
                "[[agents.entries]]\n",
                "name = \"two\"\n",
            ))
        );
    }
}
