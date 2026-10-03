from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from md_packages.materialize import recover_scope
from md_packages.migration import MigrationError, apply_migration, plan_migration
from md_packages.resolver import Resolver
from md_packages.service import install
from md_packages.transaction import ScopeLocks


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "work"
        self.source.mkdir(); self.destination.mkdir()
        for key in ("code", "review"):
            path = self.source / "packages" / "skills" / key
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text(f"# {key}\n")
        self.source_manifest = self.source / "md-package.json"
        self.source_manifest.write_text(json.dumps({"version": 1, "extra": "keep", "artifacts": [
            {"kind": "skills", "key": key, "source": f"packages/skills/{key}", "target": f"skills/{key}"}
            for key in ("code", "review")], "routing": {"code-route": {"read": ["skills/code"]}}}))
        (self.destination / "md-package.json").write_text('{"version": 1, "name": "work"}\n')

    def test_batch_move_routes_and_resolution(self) -> None:
        plan = plan_migration(["skills/code", "skills/review"], self.destination,
                              scope=self.source, with_routes=("code-route",))
        self.assertEqual(plan["kind"], "migration")
        self.assertEqual(len(plan["payloadOperations"]), 2)
        result = apply_migration(plan)
        self.assertEqual(result["mode"], "move")
        self.assertFalse((self.source / "packages/skills/code").exists())
        self.assertFalse((self.source / "packages/skills/review").exists())
        src_manifest = json.loads(self.source_manifest.read_text())
        self.assertEqual(src_manifest["artifacts"], [])
        self.assertEqual(src_manifest["extra"], "keep")
        self.assertNotIn("code-route", src_manifest["routing"])
        dest_manifest = json.loads((self.destination / "md-package.json").read_text())
        self.assertEqual(dest_manifest["routing"]["code-route"]["read"], ["skills/code"])
        self.assertEqual({item.identity for item in Resolver().resolve(self.destination).artifacts},
                         {"skills/code", "skills/review"})

    def test_copy_and_idempotency(self) -> None:
        plan = plan_migration(["skills/code"], self.destination, scope=self.source, copy_mode=True)
        apply_migration(plan)
        self.assertTrue((self.source / "packages/skills/code").exists())
        self.assertEqual(json.loads(self.source_manifest.read_text())["artifacts"][0]["key"], "code")
        second = plan_migration(["skills/code"], self.destination, scope=self.source, copy_mode=True)
        self.assertEqual(second["manifestEdits"], [])
        apply_migration(second)

    def test_identity_prefers_authored_source_when_generated_path_exists(self) -> None:
        install(self.source)
        self.assertTrue((self.source / "skills/code/SKILL.md").exists())
        plan = plan_migration(["skills/code"], self.destination, scope=self.source, copy_mode=True)
        self.assertEqual(plan["payloadOperations"][0]["source"],
                         str((self.source / "packages/skills/code").resolve()))

    def test_arbitrary_file_and_directory_identity(self) -> None:
        note = self.source / "principles.md"
        note.write_text("# Standards\n")
        plan = plan_migration([str(note)], self.destination, scope=self.source, copy_mode=True)
        self.assertEqual(plan["payloadOperations"][0]["identity"], "docs/principles")
        self.assertTrue(plan["payloadOperations"][0]["destination"].endswith("packages/docs/principles.md"))
        apply_migration(plan)
        self.assertTrue(note.exists())
        directory = self.source / "unclassified"
        directory.mkdir()
        (directory / "a.md").write_text("x")
        with self.assertRaisesRegex(MigrationError, "--as"):
            plan_migration([str(directory)], self.destination, scope=self.source)
        plan = plan_migration([str(directory)], self.destination, scope=self.source,
                              as_identity="docs/shared")
        apply_migration(plan)
        self.assertFalse(directory.exists())
        self.assertTrue((self.destination / "packages/docs/shared/a.md").exists())

    def test_route_and_registry_are_explicit(self) -> None:
        with self.assertRaisesRegex(MigrationError, "dangle"):
            plan_migration(["skills/code"], self.destination, scope=self.source)
        registry = self.root / "md-package.json"
        registry.write_text('{"version": 1, "registry": {"other": "other/md-package.json"}}')
        plan = plan_migration(["skills/code"], self.destination, scope=self.source,
                              with_routes=("code-route",), registry_manifest=registry)
        apply_migration(plan)
        data = json.loads(registry.read_text())
        self.assertEqual(data["registry"]["other"], "other/md-package.json")
        self.assertEqual(data["registry"]["work"]["path"], "work/md-package.json")

    def test_stale_hash_and_mode(self) -> None:
        plan = plan_migration(["skills/review"], self.destination, scope=self.source)
        source_file = self.source / "packages/skills/review/SKILL.md"
        source_file.write_text("changed")
        with self.assertRaises(MigrationError):
            apply_migration(plan)
        self.assertFalse((self.destination / "packages/skills/review").exists())
        source_file.write_text("# review\n")
        plan = plan_migration(["skills/review"], self.destination, scope=self.source)
        source_file.chmod(0o600)
        with self.assertRaises(MigrationError):
            apply_migration(plan)
        source_file.chmod(0o644)
        plan = plan_migration(["skills/review"], self.destination, scope=self.source)
        self.source_manifest.write_text(self.source_manifest.read_text() + "\n")
        with self.assertRaises(MigrationError):
            apply_migration(plan)

    def test_conflicts_symlinks_overlap_and_managed_refusal(self) -> None:
        source_path = self.source / "packages/skills/review"
        with self.assertRaises(MigrationError):
            plan_migration([str(source_path), "skills/review"], self.destination, scope=self.source)
        bad = self.source / "bad.md"
        bad.symlink_to(source_path / "SKILL.md")
        with self.assertRaises(MigrationError):
            plan_migration([str(bad)], self.destination, scope=self.source)
        target = self.destination / "packages/skills/review"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("other")
        with self.assertRaises(MigrationError):
            plan_migration(["skills/review"], self.destination, scope=self.source)
        (self.source / "ROUTER.md").write_text("generated")
        with self.assertRaises(MigrationError):
            plan_migration([str(self.source / "ROUTER.md")], self.destination, scope=self.source,
                           as_identity="docs/router")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.rename(target.parent / "Review")
        with self.assertRaisesRegex(MigrationError, "casefold"):
            plan_migration(["skills/review"], self.destination, scope=self.source)

    def test_rejects_shared_or_overlapping_authored_sources(self) -> None:
        manifest = json.loads(self.source_manifest.read_text())
        manifest["artifacts"].append({"kind": "skills", "key": "alias",
                                      "source": "packages/skills/review", "target": "skills/alias"})
        self.source_manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MigrationError, "overlaps another authored artifact"):
            plan_migration(["skills/review"], self.destination, scope=self.source)

        manifest["artifacts"].pop()
        self.source_manifest.write_text(json.dumps(manifest))
        broad = self.destination / "packages" / "skills"
        dest = json.loads((self.destination / "md-package.json").read_text())
        dest["artifacts"] = [{"kind": "bundles", "key": "all", "source": "packages/skills",
                              "target": "bundles/all"}]
        (self.destination / "md-package.json").write_text(json.dumps(dest))
        broad.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(MigrationError, "destination overlaps"):
            plan_migration(["skills/review"], self.destination, scope=self.source)

    def test_selected_route_must_resolve_at_destination(self) -> None:
        manifest = json.loads(self.source_manifest.read_text())
        manifest["routing"]["code-route"]["read"].append("skills/review")
        self.source_manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MigrationError, "invalid at destination"):
            plan_migration(["skills/code"], self.destination, scope=self.source,
                           with_routes=("code-route",))

    def test_route_can_migrate_into_new_empty_scope(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        plan = plan_migration(["skills/code"], empty, scope=self.source,
                              with_routes=("code-route",))
        apply_migration(plan)
        install(empty)
        self.assertTrue((empty / "skills/code/SKILL.md").exists())

    def test_legacy_routes_alias_is_checked_and_edited(self) -> None:
        manifest = json.loads(self.source_manifest.read_text())
        manifest["routes"] = manifest.pop("routing")
        self.source_manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MigrationError, "would dangle"):
            plan_migration(["skills/code"], self.destination, scope=self.source)
        plan = plan_migration(["skills/code"], self.destination, scope=self.source,
                              with_routes=("code-route",))
        apply_migration(plan)
        self.assertNotIn("code-route", json.loads(self.source_manifest.read_text())["routes"])

    def test_source_change_during_cleanup_rolls_back(self) -> None:
        plan = plan_migration(["skills/review"], self.destination, scope=self.source)
        source_file = self.source / "packages/skills/review/SKILL.md"
        changed = False
        def mutate(label: str) -> None:
            nonlocal changed
            if not changed and label == "packages/skills/review":
                source_file.write_text("changed during apply")
                changed = True
        with self.assertRaisesRegex(MigrationError, "source changed before cleanup"):
            apply_migration(plan, failure_injector=mutate)
        self.assertEqual(source_file.read_text(), "changed during apply")
        self.assertFalse((self.destination / "packages/skills/review").exists())

    def test_recovery_acquires_every_recorded_operation_scope(self) -> None:
        plan = plan_migration(["skills/review"], self.destination, scope=self.source)
        with self.assertRaises(KeyboardInterrupt):
            apply_migration(plan, failure_injector=lambda label: (_ for _ in ()).throw(KeyboardInterrupt())
                            if label == "intent:md-package.json" else None)
        other = self.root / "other"
        other.mkdir()
        (other / "md-package.json").write_text('{"version": 1}\n')
        other_source = other / "other.md"
        other_source.write_text("other")
        second = plan_migration([str(other_source)], self.destination, scope=other,
                                as_identity="docs/other", copy_mode=True)
        with ScopeLocks([self.source]):
            with self.assertRaisesRegex(Exception, "locked"):
                apply_migration(second)

    def test_recovery_after_each_phase(self) -> None:
        for stop in range(1, 5):
            with self.subTest(stop=stop):
                dest = self.root / f"dest-{stop}"
                dest.mkdir()
                (dest / "md-package.json").write_text('{"version": 1}\n')
                plan = plan_migration(["skills/review"], dest, scope=self.source)
                count = 0
                def crash(label: str) -> None:
                    nonlocal count
                    if not label.startswith("intent:"):
                        return
                    count += 1
                    if count == stop:
                        raise KeyboardInterrupt("crash")
                with self.assertRaises(KeyboardInterrupt):
                    apply_migration(plan, failure_injector=crash)
                self.assertEqual(len(recover_scope(dest)), 1)
                self.assertEqual(recover_scope(dest), [])
                self.assertTrue((self.source / "packages/skills/review/SKILL.md").exists())
                self.assertFalse((dest / "packages/skills/review").exists())
                self.assertEqual((dest / "md-package.json").read_text(), '{"version": 1}\n')


if __name__ == "__main__":
    unittest.main()
