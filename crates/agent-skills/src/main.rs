//! Parallel native compatibility entry point.

use agent_contracts::{canonical_json, canonical_sha256, load_json, require_schema_version};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "agent-skills-rs",
    version,
    about = "Native AgentDevelopmentSkills compatibility CLI"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Emit the canonical JSON representation of an existing JSON artifact.
    Canonicalize { artifact: PathBuf },
    /// Emit the canonical SHA-256 identity of an existing JSON artifact.
    Hash { artifact: PathBuf },
    /// Apply the common version boundary used before typed validation.
    ValidateVersion {
        artifact: PathBuf,
        #[arg(long, default_value = "1.0")]
        expected: String,
    },
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    match Cli::parse().command {
        Command::Canonicalize { artifact } => {
            let value = load_json(artifact)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::Hash { artifact } => {
            let value = load_json(artifact)?;
            println!("{}", canonical_sha256(&value)?);
        }
        Command::ValidateVersion { artifact, expected } => {
            let value = load_json(artifact)?;
            require_schema_version(&value, &expected)?;
            println!("{}", canonical_sha256(&value)?);
        }
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
