use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=RUSTC");
    let rustc = std::env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
    let output = Command::new(rustc)
        .arg("--version")
        .output()
        .expect("pinned rustc version must be discoverable");
    assert!(output.status.success(), "pinned rustc version query failed");
    let version = String::from_utf8(output.stdout).expect("rustc version must be UTF-8");
    let version = version.trim();
    assert!(
        version.starts_with("rustc 1.97.1 "),
        "agent-release must be compiled with Rust 1.97.1"
    );
    println!("cargo:rustc-env=AGENT_RELEASE_RUSTC_VERSION={version}");
}
