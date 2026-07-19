#![cfg(not(windows))]

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Command;

struct TestRoot(PathBuf);

impl TestRoot {
    fn new() -> Self {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "agent-skills-installed-session-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&root).expect("create test root");
        Self(root)
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("workspace root")
        .to_path_buf()
}

#[test]
#[allow(clippy::too_many_lines)]
fn fresh_apple_install_publishes_a_compatible_agent_session_cli() {
    let root = TestRoot::new();
    let target = root.0.join("target");
    let repository = root.0.join("repository");
    std::fs::create_dir(&repository).expect("create repository");
    let git = Command::new("git")
        .args(["init", "-q"])
        .current_dir(&repository)
        .status()
        .expect("run git init");
    assert!(git.success(), "git init failed");
    for arguments in [
        ["config", "user.email", "agent-skills@example.invalid"].as_slice(),
        ["config", "user.name", "Agent Skills Test"].as_slice(),
    ] {
        let status = Command::new("git")
            .args(arguments)
            .current_dir(&repository)
            .status()
            .expect("configure git repository");
        assert!(status.success(), "git config failed");
    }
    std::fs::write(repository.join("README.md"), "fixture\n").expect("write fixture");
    let commit = Command::new("git")
        .args(["add", "README.md"])
        .current_dir(&repository)
        .status()
        .and_then(|status| {
            if status.success() {
                Command::new("git")
                    .args(["commit", "-q", "-m", "test(session): [HUMAN] 创建测试基线"])
                    .current_dir(&repository)
                    .status()
            } else {
                Ok(status)
            }
        })
        .expect("create fixture commit");
    assert!(commit.success(), "git commit failed");

    let binary = PathBuf::from(env!("CARGO_BIN_EXE_agent-skills-rs"));
    let launcher_source = root.0.join("agent-session-launcher");
    std::fs::copy(&binary, &launcher_source).expect("copy native session launcher");
    let strip = Command::new("strip")
        .arg(&launcher_source)
        .status()
        .expect("strip native session launcher");
    assert!(strip.success(), "strip native session launcher failed");
    assert!(
        std::fs::metadata(&launcher_source)
            .expect("inspect stripped native session launcher")
            .len()
            <= 64 * 1024 * 1024,
        "stripped native session launcher exceeds the production contract limit"
    );
    let preview = Command::new(&binary)
        .args([
            "install",
            "--source-root",
            repository_root().to_str().expect("UTF-8 workspace"),
            "--target-root",
            target.to_str().expect("UTF-8 target"),
            "--platform",
            "apple",
            "--session-launcher",
            launcher_source.to_str().expect("UTF-8 launcher"),
            "--dry-run",
            "--json",
        ])
        .output()
        .expect("run native install preview");
    assert!(
        preview.status.success(),
        "native install preview failed: {}",
        String::from_utf8_lossy(&preview.stderr)
    );
    let preview: Value =
        serde_json::from_slice(&preview.stdout).expect("native install preview JSON");
    assert_eq!(preview["status"], "planned");
    assert_eq!(preview["engine"], "rust");
    assert!(
        preview["activation"]["managed_file_updates"]
            .as_array()
            .expect("preview activation updates")
            .iter()
            .any(|value| value == "bin/agent-skills")
    );
    assert!(!target.exists(), "native preview created the target");

    let install = Command::new(&binary)
        .args([
            "install",
            "--source-root",
            repository_root().to_str().expect("UTF-8 workspace"),
            "--target-root",
            target.to_str().expect("UTF-8 target"),
            "--platform",
            "apple",
            "--session-launcher",
            launcher_source.to_str().expect("UTF-8 launcher"),
            "--json",
        ])
        .output()
        .expect("run native install");
    assert!(
        install.status.success(),
        "native install failed: {}",
        String::from_utf8_lossy(&install.stderr)
    );

    let launcher = target.join("bin/agent-session");
    let help = Command::new(&launcher)
        .arg("--help")
        .output()
        .expect("run installed agent-session help");
    assert!(help.status.success());
    let help = String::from_utf8(help.stdout).expect("UTF-8 help");
    for command in [
        "create",
        "list",
        "inspect",
        "fingerprint",
        "checkpoint",
        "gate",
    ] {
        assert!(
            help.contains(command),
            "missing agent-session command: {command}"
        );
    }

    let worktree_root = root.0.join("worktrees");
    let create = Command::new(&launcher)
        .args([
            "create",
            "feature-a",
            "--repository",
            repository.to_str().expect("UTF-8 repository"),
            "--project-id",
            "fixture-project",
            "--worktree-root",
            worktree_root.to_str().expect("UTF-8 worktree root"),
        ])
        .output()
        .expect("run installed agent-session create");
    assert!(
        create.status.success(),
        "installed agent-session create failed: {}",
        String::from_utf8_lossy(&create.stderr)
    );
    let created: Value = serde_json::from_slice(&create.stdout).expect("create JSON report");
    assert_eq!(created["operation"], "create");
    assert_eq!(created["session"]["lifecycle"]["state"], "active");

    let listing = Command::new(&launcher)
        .args([
            "list",
            "--repository",
            repository.to_str().expect("UTF-8 repository"),
        ])
        .output()
        .expect("run installed agent-session list");
    assert!(
        listing.status.success(),
        "installed agent-session list failed: {}",
        String::from_utf8_lossy(&listing.stderr)
    );
    let report: Value = serde_json::from_slice(&listing.stdout).expect("canonical JSON report");
    assert_eq!(report["schema_version"], "1.0");
    assert_eq!(
        report["sessions"].as_array().map(Vec::len),
        Some(1),
        "created Session must be listed"
    );

    let native_cli = target.join("bin/agent-skills");
    let doctor = Command::new(&native_cli)
        .args([
            "doctor",
            "--target-root",
            target.to_str().expect("UTF-8 target"),
        ])
        .output()
        .expect("run installed native Doctor");
    assert!(
        doctor.status.success(),
        "installed native Doctor failed: {}",
        String::from_utf8_lossy(&doctor.stderr)
    );
    let doctor: Value = serde_json::from_slice(&doctor.stdout).expect("native Doctor JSON report");
    assert_eq!(doctor["schema_version"], "2.0");
    assert_eq!(doctor["status"], "passed");
    assert_eq!(
        doctor["environment"]["implementation"]["name"],
        "agent-skills-rs"
    );
    assert!(doctor["environment"].get("python_version").is_none());

    let uninstall_preview = Command::new(&native_cli)
        .args([
            "uninstall",
            target.to_str().expect("UTF-8 target"),
            "--platform",
            "all",
            "--dry-run",
            "--json",
        ])
        .output()
        .expect("run installed native uninstall preview");
    assert!(
        uninstall_preview.status.success(),
        "installed native uninstall preview failed: {}",
        String::from_utf8_lossy(&uninstall_preview.stderr)
    );
    let preview: Value =
        serde_json::from_slice(&uninstall_preview.stdout).expect("uninstall preview JSON");
    assert_eq!(preview["status"], "planned");
    assert_eq!(preview["selected_platforms"], serde_json::json!(["apple"]));
    assert!(target.join("AGENTS.md").is_file());

    let uninstall = Command::new(&native_cli)
        .args([
            "uninstall",
            target.to_str().expect("UTF-8 target"),
            "--platform",
            "all",
            "--json",
        ])
        .output()
        .expect("run installed native uninstall");
    assert!(
        uninstall.status.success(),
        "installed native uninstall failed: {}",
        String::from_utf8_lossy(&uninstall.stderr)
    );
    let uninstall: Value =
        serde_json::from_slice(&uninstall.stdout).expect("uninstall JSON report");
    assert_eq!(uninstall["status"], "uninstalled");
    for managed in ["AGENTS.md", "skills", ".agent-skills"] {
        assert!(
            !target.join(managed).exists(),
            "managed root survived uninstall: {managed}"
        );
    }
    assert!(!target.join("bin/agent-session").exists());
    assert!(!target.join("bin/agent-skills").exists());
}
