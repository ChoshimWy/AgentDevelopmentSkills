use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

fn main() {
    let manifest = PathBuf::from(
        std::env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR is required"),
    );
    let workspace = manifest
        .parent()
        .and_then(Path::parent)
        .expect("lifecycle crate must remain inside the workspace");
    let mut sources = vec![
        workspace.join("Cargo.toml"),
        workspace.join("Cargo.lock"),
        workspace.join("rust-toolchain.toml"),
    ];
    for package in [
        "agent-contracts",
        "agent-engine",
        "agent-lifecycle",
        "agent-registry",
        "agent-runtime",
    ] {
        collect_local_crate(&workspace.join("crates").join(package), &mut sources);
    }
    for root in ["schemas", "disciplines", "platforms", "stacks"] {
        println!("cargo:rerun-if-changed={}", workspace.join(root).display());
    }
    let schema_inventory = agent_engine::schema_inventory(workspace.join("schemas"))
        .expect("workspace Schema inventory must be valid");
    let schema_inventory_bytes = agent_contracts::canonical_json(&schema_inventory)
        .expect("workspace Schema inventory must be canonicalizable");
    let output = PathBuf::from(std::env::var_os("OUT_DIR").expect("OUT_DIR is required"));
    fs::write(
        output.join("embedded-schema-inventory.json"),
        schema_inventory_bytes,
    )
    .expect("embedded Schema inventory must be writable");
    for entry in schema_inventory
        .get("files")
        .and_then(serde_json::Value::as_array)
        .expect("Schema inventory files must be an array")
    {
        let relative = entry
            .get("path")
            .and_then(serde_json::Value::as_str)
            .expect("Schema inventory path must be a string");
        sources.push(workspace.join(relative));
    }
    sources.sort();

    let mut digest = Sha256::new();
    for name in [
        "HOST",
        "TARGET",
        "CARGO_CFG_TARGET_ARCH",
        "CARGO_CFG_TARGET_ENV",
        "CARGO_CFG_TARGET_FAMILY",
        "CARGO_CFG_TARGET_OS",
    ] {
        let value = std::env::var(name).unwrap_or_default();
        digest.update(b"\0build-context\0");
        digest.update(name.as_bytes());
        digest.update(b"\0");
        digest.update(value.as_bytes());
    }
    for path in sources {
        let relative = path
            .strip_prefix(workspace)
            .expect("handler source must remain inside workspace")
            .to_string_lossy()
            .replace('\\', "/");
        let bytes = fs::read(&path).expect("handler source must be readable");
        digest.update(b"\0");
        digest.update(relative.as_bytes());
        digest.update(b"\0");
        digest.update(Sha256::digest(&bytes));
        println!("cargo:rerun-if-changed={}", path.display());
    }
    println!(
        "cargo:rustc-env=AGENT_LIFECYCLE_SOURCE_SHA256={:x}",
        digest.finalize()
    );
}

fn collect_local_crate(root: &Path, output: &mut Vec<PathBuf>) {
    for name in ["Cargo.toml", "build.rs"] {
        let path = root.join(name);
        if path.exists() {
            output.push(path);
        }
    }
    collect_rust_sources(&root.join("src"), output);
}

fn collect_rust_sources(root: &Path, output: &mut Vec<PathBuf>) {
    let mut entries = fs::read_dir(root)
        .expect("lifecycle source directory must be readable")
        .map(|entry| {
            entry
                .expect("lifecycle source entry must be readable")
                .path()
        })
        .collect::<Vec<_>>();
    entries.sort();
    for path in entries {
        let metadata = fs::symlink_metadata(&path).expect("lifecycle source metadata is required");
        assert!(
            !metadata.file_type().is_symlink(),
            "lifecycle handler source must not be a symlink: {}",
            path.display()
        );
        if metadata.is_dir() {
            collect_rust_sources(&path, output);
        } else if path.extension().is_some_and(|extension| extension == "rs") {
            output.push(path);
        }
    }
}
