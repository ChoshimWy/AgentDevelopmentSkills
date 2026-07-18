use agent_contracts::canonical_json;
use agent_release::{merge_native_artifacts, record_native_artifact, verify_native_artifacts};
use clap::{Parser, Subcommand};
use serde_json::json;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "agent-release",
    version,
    about = "Native AgentDevelopmentSkills release artifact compiler"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Freeze one built executable and its target-specific provenance record.
    Record {
        #[arg(long)]
        binary: PathBuf,
        #[arg(long)]
        cargo_lock: PathBuf,
        #[arg(long)]
        target: String,
        #[arg(long)]
        source_revision: String,
        #[arg(long)]
        output: PathBuf,
    },
    /// Merge the complete six-target release matrix.
    Merge {
        #[arg(long = "record", required = true)]
        records: Vec<PathBuf>,
        #[arg(long)]
        output: PathBuf,
    },
    /// Verify an existing native index and its adjacent executables.
    Verify {
        #[arg(long)]
        index: PathBuf,
        #[arg(long)]
        artifacts_dir: PathBuf,
    },
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let result = match Cli::parse().command {
        Command::Record {
            binary,
            cargo_lock,
            target,
            source_revision,
            output,
        } => {
            let record =
                record_native_artifact(&binary, &cargo_lock, &target, &source_revision, &output)?;
            json!({
                "artifact": record,
                "output": output,
                "status": "recorded",
            })
        }
        Command::Merge { records, output } => {
            let index = merge_native_artifacts(&records, &output)?;
            json!({
                "index": index,
                "output": output,
                "status": "merged",
            })
        }
        Command::Verify {
            index,
            artifacts_dir,
        } => {
            let value = verify_native_artifacts(&index, &artifacts_dir)?;
            json!({
                "fingerprint": value.fingerprint,
                "status": "verified",
                "targets": value.artifacts.len(),
            })
        }
    };
    print!("{}", String::from_utf8(canonical_json(&result)?)?);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
