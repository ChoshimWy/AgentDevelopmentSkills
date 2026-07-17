from __future__ import annotations

import copy
import unittest

from scripts.validate_github_publication import (
    PublicationError,
    QUALIFICATION_WORKFLOW_PATH,
    validate_publication_request,
    validate_tag_absent,
)


REPOSITORY = "ChoshimWy/AgentDevelopmentSkills"
REVISION = "a" * 40


def valid_run() -> dict:
    return {
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": REVISION,
        "path": QUALIFICATION_WORKFLOW_PATH,
        "repository": {"full_name": REPOSITORY},
        "status": "completed",
    }


def valid_main_branch() -> dict:
    return {
        "commit": {"sha": REVISION},
        "name": "main",
        "protected": True,
    }


class GitHubPublicationTests(unittest.TestCase):
    def test_current_main_qualification_run_is_accepted(self) -> None:
        validate_publication_request(
            valid_run(),
            valid_main_branch(),
            repository=REPOSITORY,
            source_revision=REVISION,
            workflow_revision=REVISION,
        )

    def test_wrong_workflow_event_status_branch_or_revision_is_rejected(self) -> None:
        cases = {
            "conclusion": "failure",
            "event": "push",
            "head_branch": "feature/untrusted",
            "head_sha": "b" * 40,
            "path": ".github/workflows/untrusted.yml",
            "status": "in_progress",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                run = valid_run()
                run[field] = value
                with self.assertRaises(PublicationError):
                    validate_publication_request(
                        run,
                        valid_main_branch(),
                        repository=REPOSITORY,
                        source_revision=REVISION,
                        workflow_revision=REVISION,
                    )

    def test_cross_repository_stale_main_and_different_workflow_revision_are_rejected(self) -> None:
        mutations = []
        cross_repository = valid_run()
        cross_repository["repository"] = {"full_name": "attacker/fork"}
        mutations.append((cross_repository, valid_main_branch(), REVISION))
        cross_head = valid_run()
        cross_head["head_repository"] = {"full_name": "attacker/fork"}
        mutations.append((cross_head, valid_main_branch(), REVISION))
        stale_main = valid_main_branch()
        stale_main["commit"]["sha"] = "b" * 40
        mutations.append((valid_run(), stale_main, REVISION))
        unprotected_main = valid_main_branch()
        unprotected_main["protected"] = False
        mutations.append((valid_run(), unprotected_main, REVISION))
        mutations.append((valid_run(), valid_main_branch(), "b" * 40))

        for run, main_ref, workflow_revision in mutations:
            with self.subTest(
                repository=run["repository"]["full_name"],
                main=main_ref["commit"]["sha"],
                workflow=workflow_revision,
            ):
                with self.assertRaises(PublicationError):
                    validate_publication_request(
                        run,
                        main_ref,
                        repository=REPOSITORY,
                        source_revision=REVISION,
                        workflow_revision=workflow_revision,
                    )

    def test_existing_lightweight_or_annotated_tag_is_rejected(self) -> None:
        validate_tag_absent([], tag="v0.2.0")
        validate_tag_absent(
            [{"object": {"sha": REVISION, "type": "commit"}, "ref": "refs/tags/v0.2.0"}],
            tag="v0.2.1",
        )
        for object_type in ("commit", "tag"):
            with self.subTest(object_type=object_type):
                with self.assertRaisesRegex(PublicationError, "already exists"):
                    validate_tag_absent(
                        [{
                            "object": {"sha": REVISION, "type": object_type},
                            "ref": "refs/tags/v0.2.0",
                        }],
                        tag="v0.2.0",
                    )

    def test_malformed_tag_response_is_rejected(self) -> None:
        for value in ({}, [None], [{"object": {}}]):
            with self.subTest(value=copy.deepcopy(value)):
                with self.assertRaises(PublicationError):
                    validate_tag_absent(value, tag="v0.2.0")


if __name__ == "__main__":
    unittest.main()
