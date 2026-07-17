//! Deterministic JSON primitives shared by the native `AgentDevelopmentSkills` implementation.

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt::Write as _;
use std::path::Path;
use thiserror::Error;

/// Cross-implementation maximum for one decimal integer token.
pub const MAX_CANONICAL_INTEGER_DIGITS: usize = 4_300;
/// Cross-implementation maximum for nested JSON arrays and objects.
pub const MAX_CANONICAL_JSON_DEPTH: usize = 512;

/// Errors produced while reading or encoding a versioned contract.
#[derive(Debug, Error)]
pub enum ContractError {
    #[error("contract JSON is invalid: {0}")]
    InvalidJson(#[from] serde_json::Error),
    #[error("contract input cannot be read: {0}")]
    Io(#[from] std::io::Error),
    #[error("contract root must be an object")]
    RootMustBeObject,
    #[error("contract number is outside the finite JSON range: {0}")]
    NonFiniteNumber(String),
    #[error("contract integer has {digits} digits; maximum is {maximum}")]
    IntegerTooLong { digits: usize, maximum: usize },
    #[error("contract JSON nesting depth {depth} exceeds maximum {maximum}")]
    NestingTooDeep { depth: usize, maximum: usize },
    #[error("unsupported schema_version: {actual:?}; expected {expected:?}")]
    UnsupportedSchemaVersion {
        actual: Option<String>,
        expected: String,
    },
}

/// Parse JSON using the same last-key-wins object behavior as Python's `json.loads`.
///
/// # Errors
/// Returns [`ContractError::InvalidJson`] when the input is not valid JSON.
pub fn parse_json(bytes: &[u8]) -> Result<Value, ContractError> {
    validate_lexical_limits(bytes)?;
    let mut deserializer = serde_json::Deserializer::from_slice(bytes);
    deserializer.disable_recursion_limit();
    let value = Value::deserialize(serde_stacker::Deserializer::new(&mut deserializer))?;
    deserializer.end()?;
    Ok(value)
}

/// Load one JSON contract from disk.
///
/// # Errors
/// Returns an I/O or JSON parsing error when the artifact cannot be loaded.
pub fn load_json(path: impl AsRef<Path>) -> Result<Value, ContractError> {
    parse_json(&std::fs::read(path)?)
}

/// Encode compact UTF-8 JSON with sorted object keys and one trailing newline.
///
/// # Errors
/// Returns an encoding error if the JSON value cannot be serialized.
pub fn canonical_json(value: &Value) -> Result<Vec<u8>, ContractError> {
    validate_value_limits(value)?;
    let mut output = Vec::new();
    write_canonical_value(value, &mut output)?;
    output.push(b'\n');
    Ok(output)
}

fn validate_lexical_limits(bytes: &[u8]) -> Result<(), ContractError> {
    let mut depth = 0_usize;
    let mut index = 0_usize;
    let mut in_string = false;
    let mut escaped = false;
    while index < bytes.len() {
        let byte = bytes[index];
        if in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            index += 1;
            continue;
        }
        match byte {
            b'"' => in_string = true,
            b'{' | b'[' => {
                depth += 1;
                if depth > MAX_CANONICAL_JSON_DEPTH {
                    return Err(ContractError::NestingTooDeep {
                        depth,
                        maximum: MAX_CANONICAL_JSON_DEPTH,
                    });
                }
            }
            b'}' | b']' => depth = depth.saturating_sub(1),
            b'-' | b'0'..=b'9' => {
                let start = index;
                index += 1;
                while index < bytes.len()
                    && !matches!(
                        bytes[index],
                        b' ' | b'\t' | b'\r' | b'\n' | b',' | b']' | b'}'
                    )
                {
                    index += 1;
                }
                let token = &bytes[start..index];
                if !token.contains(&b'.') && !token.contains(&b'e') && !token.contains(&b'E') {
                    let digits = token.iter().filter(|byte| byte.is_ascii_digit()).count();
                    if digits > MAX_CANONICAL_INTEGER_DIGITS {
                        return Err(ContractError::IntegerTooLong {
                            digits,
                            maximum: MAX_CANONICAL_INTEGER_DIGITS,
                        });
                    }
                }
                continue;
            }
            _ => {}
        }
        index += 1;
    }
    Ok(())
}

fn validate_value_limits(value: &Value) -> Result<(), ContractError> {
    let mut stack = vec![(value, 0_usize)];
    while let Some((current, parent_depth)) = stack.pop() {
        match current {
            Value::Array(items) => {
                let depth = parent_depth + 1;
                if depth > MAX_CANONICAL_JSON_DEPTH {
                    return Err(ContractError::NestingTooDeep {
                        depth,
                        maximum: MAX_CANONICAL_JSON_DEPTH,
                    });
                }
                stack.extend(items.iter().map(|item| (item, depth)));
            }
            Value::Object(object) => {
                let depth = parent_depth + 1;
                if depth > MAX_CANONICAL_JSON_DEPTH {
                    return Err(ContractError::NestingTooDeep {
                        depth,
                        maximum: MAX_CANONICAL_JSON_DEPTH,
                    });
                }
                stack.extend(object.values().map(|item| (item, depth)));
            }
            Value::Number(number) => {
                let source = number.to_string();
                if !source.contains(['.', 'e', 'E']) {
                    let digits = source.bytes().filter(u8::is_ascii_digit).count();
                    if digits > MAX_CANONICAL_INTEGER_DIGITS {
                        return Err(ContractError::IntegerTooLong {
                            digits,
                            maximum: MAX_CANONICAL_INTEGER_DIGITS,
                        });
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn write_canonical_value(value: &Value, output: &mut Vec<u8>) -> Result<(), ContractError> {
    match value {
        Value::Null => output.extend_from_slice(b"null"),
        Value::Bool(true) => output.extend_from_slice(b"true"),
        Value::Bool(false) => output.extend_from_slice(b"false"),
        Value::Number(number) => {
            output.extend_from_slice(python_number(number)?.as_bytes());
        }
        Value::String(text) => serde_json::to_writer(output, text)?,
        Value::Array(items) => {
            output.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                write_canonical_value(item, output)?;
            }
            output.push(b']');
        }
        Value::Object(object) => {
            output.push(b'{');
            let mut entries = object.iter().collect::<Vec<_>>();
            entries.sort_unstable_by(|left, right| left.0.cmp(right.0));
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                serde_json::to_writer(&mut *output, key)?;
                output.push(b':');
                write_canonical_value(item, output)?;
            }
            output.push(b'}');
        }
    }
    Ok(())
}

fn python_number(number: &serde_json::Number) -> Result<String, ContractError> {
    let source = number.to_string();
    if !source.contains(['.', 'e', 'E']) {
        if source.bytes().all(|byte| matches!(byte, b'-' | b'0')) {
            return Ok("0".to_owned());
        }
        return Ok(source);
    }

    let value = source
        .parse::<f64>()
        .map_err(|_| ContractError::NonFiniteNumber(source.clone()))?;
    if !value.is_finite() {
        return Err(ContractError::NonFiniteNumber(source));
    }
    let shortest = serde_json::Number::from_f64(value)
        .ok_or(ContractError::NonFiniteNumber(source))?
        .to_string();
    Ok(python_float_from_shortest(&shortest))
}

fn python_float_from_shortest(shortest: &str) -> String {
    let (sign, unsigned) = shortest
        .strip_prefix('-')
        .map_or(("", shortest), |value| ("-", value));
    if unsigned == "0.0" || unsigned == "0" {
        return format!("{sign}0.0");
    }

    let (mantissa, explicit_exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, 0), |(value, exponent)| {
            (value, exponent.parse::<i32>().expect("valid ryu exponent"))
        });
    let (integer, fraction) = mantissa
        .split_once('.')
        .map_or((mantissa, ""), |parts| parts);
    let combined = format!("{integer}{fraction}");
    let leading_zeroes = combined.bytes().take_while(|byte| *byte == b'0').count();
    let significant = combined[leading_zeroes..].trim_end_matches('0');
    let scientific_exponent = explicit_exponent
        + i32::try_from(integer.len()).expect("number length fits i32")
        - i32::try_from(leading_zeroes).expect("number length fits i32")
        - 1;

    if !(-4..16).contains(&scientific_exponent) {
        let mut output = String::from(sign);
        output.push_str(&significant[..1]);
        if significant.len() > 1 {
            output.push('.');
            output.push_str(&significant[1..]);
        }
        output.push('e');
        if scientific_exponent >= 0 {
            output.push('+');
        } else {
            output.push('-');
        }
        write!(output, "{:02}", scientific_exponent.unsigned_abs())
            .expect("writing to String cannot fail");
        return output;
    }

    let decimal_position = scientific_exponent + 1;
    if decimal_position <= 0 {
        let zeroes = usize::try_from(-decimal_position).expect("decimal offset fits usize");
        return format!("{sign}0.{}{significant}", "0".repeat(zeroes));
    }
    let decimal_position =
        usize::try_from(decimal_position).expect("positive decimal position fits usize");
    if decimal_position >= significant.len() {
        let zeroes = decimal_position - significant.len();
        return format!("{sign}{significant}{}.0", "0".repeat(zeroes));
    }
    format!(
        "{sign}{}.{}",
        &significant[..decimal_position],
        &significant[decimal_position..]
    )
}

/// Return the SHA-256 of the canonical JSON bytes.
///
/// # Errors
/// Returns an encoding error if canonical JSON cannot be produced.
pub fn canonical_sha256(value: &Value) -> Result<String, ContractError> {
    let bytes = canonical_json(value)?;
    Ok(hex::encode(Sha256::digest(bytes)))
}

/// Enforce a version field before a typed contract validator is invoked.
///
/// # Errors
/// Returns an error when the root is not an object or the version differs.
pub fn require_schema_version(value: &Value, expected: &str) -> Result<(), ContractError> {
    let object = value.as_object().ok_or(ContractError::RootMustBeObject)?;
    let actual = object
        .get("schema_version")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    if actual.as_deref() != Some(expected) {
        return Err(ContractError::UnsupportedSchemaVersion {
            actual,
            expected: expected.to_owned(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        MAX_CANONICAL_INTEGER_DIGITS, MAX_CANONICAL_JSON_DEPTH, canonical_json, canonical_sha256,
        parse_json, python_float_from_shortest, require_schema_version,
    };
    use serde_json::json;

    #[test]
    fn canonical_json_matches_python_contract() {
        let value = json!({"z": [3, 2, 1], "a": "中文", "nested": {"b": true, "a": null}});
        assert_eq!(
            canonical_json(&value).unwrap(),
            "{\"a\":\"中文\",\"nested\":{\"a\":null,\"b\":true},\"z\":[3,2,1]}\n".as_bytes()
        );
        assert_eq!(
            canonical_sha256(&value).unwrap(),
            "c79438fd116b1265a5cd5d0203ce77ad3a1c48a7ec0e40867e33811c9da93abf"
        );
    }

    #[test]
    fn duplicate_keys_match_python_last_key_wins_behavior() {
        let value = parse_json(br#"{"value":1,"value":2}"#).unwrap();
        assert_eq!(value, json!({"value": 2}));
    }

    #[test]
    fn python_float_formatting_matches_exponent_boundaries() {
        let cases = [
            ("0.00001", "1e-05"),
            ("1e-7", "1e-07"),
            ("0.0001", "0.0001"),
            ("10000000.0", "10000000.0"),
            ("1000000000000000.0", "1000000000000000.0"),
            ("1e+16", "1e+16"),
            ("-0.0", "-0.0"),
        ];
        for (input, expected) in cases {
            assert_eq!(python_float_from_shortest(input), expected);
        }
    }

    #[test]
    fn arbitrary_precision_integers_are_not_rounded() {
        let value = parse_json(br#"{"value":123456789012345678901234567890}"#).unwrap();
        assert_eq!(
            canonical_json(&value).unwrap(),
            b"{\"value\":123456789012345678901234567890}\n"
        );
    }

    #[test]
    fn integer_digit_limit_matches_python_default() {
        let accepted = format!("{{\"value\":{}}}", "9".repeat(MAX_CANONICAL_INTEGER_DIGITS));
        assert!(parse_json(accepted.as_bytes()).is_ok());

        for token in [
            "9".repeat(MAX_CANONICAL_INTEGER_DIGITS + 1),
            format!("-{}", "9".repeat(MAX_CANONICAL_INTEGER_DIGITS + 1)),
        ] {
            let rejected = format!("{{\"value\":{token},\"value\":0}}");
            assert!(matches!(
                parse_json(rejected.as_bytes()),
                Err(super::ContractError::IntegerTooLong { .. })
            ));
        }
    }

    #[test]
    fn direct_value_cannot_bypass_integer_digit_limit() {
        let source = format!(
            "{{\"value\":{}}}",
            "9".repeat(MAX_CANONICAL_INTEGER_DIGITS + 1)
        );
        let value = serde_json::from_str::<serde_json::Value>(&source).unwrap();
        assert!(matches!(
            canonical_json(&value),
            Err(super::ContractError::IntegerTooLong { .. })
        ));
        assert!(matches!(
            canonical_sha256(&value),
            Err(super::ContractError::IntegerTooLong { .. })
        ));
    }

    #[test]
    fn nesting_limit_is_explicit_and_fail_closed() {
        let accepted = format!(
            "{}0{}",
            "[".repeat(MAX_CANONICAL_JSON_DEPTH),
            "]".repeat(MAX_CANONICAL_JSON_DEPTH)
        );
        assert!(parse_json(accepted.as_bytes()).is_ok());

        let rejected = format!(
            "{}0{}",
            "[".repeat(MAX_CANONICAL_JSON_DEPTH + 1),
            "]".repeat(MAX_CANONICAL_JSON_DEPTH + 1)
        );
        assert!(matches!(
            parse_json(rejected.as_bytes()),
            Err(super::ContractError::NestingTooDeep { .. })
        ));
    }

    #[test]
    fn schema_version_is_fail_closed() {
        require_schema_version(&json!({"schema_version": "1.0"}), "1.0").unwrap();
        assert!(require_schema_version(&json!({"schema_version": "2.0"}), "1.0").is_err());
        assert!(require_schema_version(&json!([]), "1.0").is_err());
    }
}
