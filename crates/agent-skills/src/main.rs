//! Parallel native compatibility entry point.

use agent_contracts::{canonical_json, canonical_sha256, load_json, require_schema_version};
use agent_registry::{CORE_VERSION, ManifestRegistry, automatic_recipe_capabilities};
use clap::{Parser, Subcommand};
use std::collections::BTreeSet;
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
    /// Validate and snapshot a manifest registry without executing package code.
    RegistrySnapshot {
        root: PathBuf,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
    },
    /// Resolve one capability through the native manifest registry.
    RegistryResolve {
        root: PathBuf,
        capability: String,
        #[arg(long)]
        platform: Option<String>,
        #[arg(long, default_value = CORE_VERSION)]
        core_version: String,
        #[arg(long = "disable-provider")]
        disabled_providers: Vec<String>,
        #[arg(long = "provider-root")]
        provider_roots: Vec<PathBuf>,
    },
    /// Emit the sorted automatic recipe capability closure for target platforms.
    RecipeCapabilities { targets: Vec<String> },
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
        Command::RegistrySnapshot {
            root,
            core_version,
            disabled_providers,
            provider_roots,
        } => {
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                root,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            print!(
                "{}",
                String::from_utf8(canonical_json(&registry.snapshot()?)?)?
            );
        }
        Command::RegistryResolve {
            root,
            capability,
            platform,
            core_version,
            disabled_providers,
            provider_roots,
        } => {
            let disabled = disabled_providers.into_iter().collect::<BTreeSet<_>>();
            let registry = ManifestRegistry::from_directory_with_provider_roots(
                root,
                &provider_roots,
                &disabled,
                &core_version,
            )?;
            let resolved = registry.resolve_binding(&capability, platform.as_deref())?;
            let value = serde_json::to_value(resolved)?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
        }
        Command::RecipeCapabilities { targets } => {
            let targets = targets.into_iter().collect::<BTreeSet<_>>();
            let value = serde_json::to_value(automatic_recipe_capabilities(&targets))?;
            print!("{}", String::from_utf8(canonical_json(&value)?)?);
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
