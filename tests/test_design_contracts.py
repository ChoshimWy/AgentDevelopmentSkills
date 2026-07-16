from __future__ import annotations

from copy import deepcopy
import unittest

from agent_workflow.design import (
    DesignSourceGateway,
    WriteApproval,
    build_ui_validation_report,
    compile_canonical_ir,
    compile_design_system_registry,
    slice_agent_packet,
    validate_packet_freshness,
)
from agent_workflow.design.contracts import (
    validate_canonical_ui_ir,
    validate_design_agent_packet,
    validate_design_evidence,
    validate_design_source_request,
    validate_design_system_registry,
    validate_ui_validation_report,
)
from agent_workflow.models import ContractError


def screen_slice() -> dict:
    return {
        "id": "settings",
        "kind": "screen",
        "payload": {
            "accessibility": {"dynamic_type": True, "reduce_motion": True, "voice_over": True},
            "assets": [{"id": "icon.save", "semantic": "save"}],
            "interactions": [{"event": "tap", "from": "default", "id": "save", "to": "loading"}],
            "regions": [{
                "components": [{
                    "id": "save-button",
                    "slots": {"label": "Save"},
                    "states": ["default", "disabled", "pressed"],
                    "token_refs": ["color.action.primary"],
                    "type": "button",
                    "variant": "primary",
                }],
                "id": "footer",
                "layout": {"alignment": "trailing", "kind": "stack"},
            }],
            "responsive": {"compact": {"footer": "fill"}},
            "states": ["default", "loading"],
        },
    }


class DesignContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = DesignSourceGateway()

    def evidence(self, source_kind: str = "figma") -> dict:
        return self.gateway.normalize(
            source_kind=source_kind,
            document_id="document-1",
            document_version="42",
            page_id="page-settings",
            node_slices=[screen_slice()],
            parser_name=f"{source_kind}-adapter",
            parser_version="1.0.0",
        )

    def test_equivalent_source_slices_compile_to_deterministic_semantics(self) -> None:
        first = compile_canonical_ir([self.evidence("figma")])
        second = compile_canonical_ir([self.evidence("figma")])
        self.assertEqual(first, second)
        self.assertEqual(first["screens"][0]["regions"][0]["components"][0]["type"], "button")
        self.assertEqual(first["interactions"][0]["id"], "save")
        self.assertTrue(first["accessibility"]["settings"]["dynamic_type"])
        self.assertNotIn("swiftui", str(first).lower())

    def test_all_source_adapters_share_one_evidence_contract(self) -> None:
        values = [self.evidence(kind) for kind in ("figma", "sketch", "manual", "screenshot")]
        self.assertEqual([item["source"]["kind"] for item in values], ["figma", "sketch", "manual", "screenshot"])
        self.assertTrue(all(item["schema_version"] == "1.0" for item in values))

    def test_screenshot_fallback_is_partial_and_preserves_unknown(self) -> None:
        evidence = self.evidence("screenshot")
        self.assertEqual(evidence["status"], "partial")
        self.assertEqual(evidence["slices"][0]["provenance"]["kind"], "inference")
        self.assertEqual(evidence["unknowns"][0]["id"], "structured-states-unavailable")
        self.assertTrue(evidence["unknowns"][0]["blocking"])
        ir = compile_canonical_ir([evidence])
        with self.assertRaisesRegex(ContractError, "blocking unknowns"):
            slice_agent_packet(
                ir,
                self.registry(),
                scope_kind="component",
                scope_id="save-button",
            )

    def test_inference_cannot_claim_source_certainty(self) -> None:
        evidence = self.evidence("screenshot")
        evidence["slices"][0]["provenance"]["confidence"] = 1
        with self.assertRaisesRegex(ContractError, "source certainty"):
            validate_design_evidence(evidence)

    def test_write_requires_exact_attempt_and_scope(self) -> None:
        approval = WriteApproval(
            approval_id="approval-1",
            attempt_id="attempt-1",
            document_id="document-1",
            page_id="page-settings",
            node_ids=("settings",),
        )
        value = self.gateway.normalize(
            source_kind="figma",
            document_id="document-1",
            document_version="42",
            page_id="page-settings",
            node_slices=[screen_slice()],
            parser_name="figma-adapter",
            parser_version="1.0.0",
            mode="write",
            approval=approval,
            attempt_id="attempt-1",
        )
        self.assertEqual(value["permission"]["approval_id"], "approval-1")
        with self.assertRaisesRegex(ContractError, "not approved"):
            self.gateway.normalize(
                source_kind="figma",
                document_id="document-1",
                document_version="42",
                page_id="page-settings",
                node_slices=[screen_slice()],
                parser_name="figma-adapter",
                parser_version="1.0.0",
                mode="write",
                approval=approval,
                attempt_id="attempt-2",
            )

    def test_gateway_rejects_credential_like_payload(self) -> None:
        for key in ("access_token", "token", "accessToken", "client_secret", "private_key"):
            with self.subTest(key=key):
                source = screen_slice()
                source["payload"][key] = "must-not-enter-evidence"
                with self.assertRaisesRegex(ContractError, "credential-like"):
                    self.gateway.normalize(
                        source_kind="figma",
                        document_id="document-1",
                        document_version="42",
                        page_id="page-settings",
                        node_slices=[source],
                        parser_name="figma-adapter",
                        parser_version="1.0.0",
                    )

    def test_gateway_ledger_is_minimal_hashed_and_cache_free(self) -> None:
        evidence = self.evidence()
        record = self.gateway.ledger_projection(evidence, artifact_uri="artifact://design/evidence-1")
        self.assertEqual(record["retention"], "task")
        self.assertEqual(record["cleanup"], "not-required")
        self.assertNotIn("slices", record)
        self.assertNotIn("approval_id", str(record))
        self.assertEqual(self.gateway.cleanup(), {"reason": "gateway-does-not-cache", "status": "not-required"})
        with self.assertRaisesRegex(ContractError, "uncontrolled"):
            self.gateway.ledger_projection(evidence, artifact_uri="file:///tmp/evidence.json")

    def test_source_request_keeps_read_export_and_write_approval_separate(self) -> None:
        request = {
            "approval_id": None,
            "attempt_id": "attempt-1",
            "data_policy": {"allow_credentials": False, "max_nodes": 16, "retention": "task"},
            "document_id": "document-1",
            "document_version": "42",
            "operation": "read",
            "request_id": "request-1",
            "schema_version": "1.0",
            "scope": {"node_ids": ["settings"], "page_id": "page-settings"},
            "source_kind": "figma",
        }
        validate_design_source_request(request)
        request["operation"] = "write"
        with self.assertRaisesRegex(ContractError, "requires approval"):
            validate_design_source_request(request)
        request["approval_id"] = "approval-1"
        validate_design_source_request(request)

    def test_packet_is_task_scoped_and_marks_stale_input(self) -> None:
        ir = compile_canonical_ir([self.evidence()])
        registry = self.registry()
        packet = slice_agent_packet(
            ir,
            registry,
            scope_kind="component",
            scope_id="save-button",
            expected_inputs=["design-v1:" + "0" * 64],
        )
        self.assertEqual(packet["task_scope"], {"id": "save-button", "kind": "component"})
        self.assertEqual(list(packet["registry_slice"]["components"]), ["button"])
        self.assertEqual(packet["stale_inputs"], ["design-v1:" + "0" * 64])

        report = build_ui_validation_report(
            packet,
            environment={
                "build_fingerprint": "build-1",
                "locale": "en-US",
                "os_version": "26.0",
                "platform": "iOS",
                "viewport": {"height": 844, "width": 390},
            },
            checks=[{
                "classification": "environment-noise",
                "evidence_refs": [{
                    "kind": "visual-diff",
                    "sha256": "1" * 64,
                    "uri": "artifact://visual/save-button",
                }],
                "kind": "visual",
                "status": "partial",
                "target_id": "save-button",
            }],
        )
        self.assertEqual(report["status"], "blocked")
        self.assertIn("stale-input:", report["blockers"][0])

    def test_artifact_body_integrity_scope_and_upstream_freshness_fail_closed(self) -> None:
        ir = compile_canonical_ir([self.evidence()])
        registry = self.registry()
        packet = slice_agent_packet(ir, registry, scope_kind="component", scope_id="save-button")
        validate_packet_freshness(packet, ir, registry)

        tampered_ir = deepcopy(ir)
        tampered_ir["screens"][0]["regions"][0]["components"][0]["slots"]["label"] = "Tampered"
        with self.assertRaisesRegex(ContractError, "identity"):
            validate_canonical_ui_ir(tampered_ir)

        tampered_registry = deepcopy(registry)
        tampered_registry["tokens"]["color.action.primary"]["semantic"] = "tampered"
        with self.assertRaisesRegex(ContractError, "fingerprint"):
            validate_design_system_registry(tampered_registry)

        tampered_packet = deepcopy(packet)
        tampered_packet["task_scope"]["id"] = "another-component"
        with self.assertRaisesRegex(ContractError, "task scope"):
            validate_design_agent_packet(tampered_packet)

        newer_registry = compile_design_system_registry(
            registry_id="default",
            version="1.0.1",
            tokens={"color.action.primary": {"semantic": "action-primary-v2"}},
            components=registry["components"],
            binding_refs=["apple-registry-v1"],
        )
        with self.assertRaisesRegex(ContractError, "stale"):
            validate_packet_freshness(packet, ir, newer_registry)

    def test_ui_report_requires_hashed_controlled_evidence_and_consistent_identity(self) -> None:
        ir = compile_canonical_ir([self.evidence()])
        packet = slice_agent_packet(ir, self.registry(), scope_kind="component", scope_id="save-button")
        report = build_ui_validation_report(
            packet,
            environment={
                "build_fingerprint": "build-1", "locale": "en-US", "os_version": "26.0",
                "platform": "iOS", "viewport": {"height": 844, "width": 390},
            },
            checks=[{
                "classification": "none",
                "evidence_refs": [{
                    "kind": "semantic-tree", "sha256": "2" * 64,
                    "uri": "artifact://semantic/save-button",
                }],
                "kind": "semantic", "status": "passed", "target_id": "save-button",
            }],
        )
        tampered = deepcopy(report)
        tampered["checks"][0]["evidence_refs"] = ["artifact://forged"]
        with self.assertRaises(ContractError):
            validate_ui_validation_report(tampered)
        tampered = deepcopy(report)
        tampered["status"] = "failed"
        with self.assertRaises(ContractError):
            validate_ui_validation_report(tampered)
        mismatched = deepcopy(report)
        mismatched["checks"][0]["kind"] = "visual"
        with self.assertRaisesRegex(ContractError, "mismatched"):
            validate_ui_validation_report(mismatched)

    def registry(self) -> dict:
        return compile_design_system_registry(
            registry_id="default",
            version="1.0.0",
            tokens={"color.action.primary": {"semantic": "action-primary"}},
            components={
                "button": {
                    "motion": {},
                    "slots": ["label"],
                    "states": ["default", "disabled", "pressed"],
                    "token_refs": ["color.action.primary"],
                    "variants": ["primary"],
                }
            },
            binding_refs=["apple-registry-v1"],
        )

    def test_conflicting_source_facts_fail_closed(self) -> None:
        first = self.evidence()
        second = deepcopy(first)
        second["evidence_id"] = "evidence-conflict"
        second["slices"][0]["payload"]["states"] = ["default", "error"]
        second["slices"][0]["provenance"]["evidence_ref"] = "evidence-conflict"
        with self.assertRaisesRegex(ContractError, "conflicting screen evidence"):
            compile_canonical_ir([first, second])


if __name__ == "__main__":
    unittest.main()
