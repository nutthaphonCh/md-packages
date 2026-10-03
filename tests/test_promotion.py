import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from md_packages.promotion import PromotionError, apply_promotion, content_hash, plan_promotion
from md_packages.resolver import Resolver
from md_packages.materialize import journal_replace, recover_scope


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.source = self.root / "source"; self.source.mkdir()
        (self.source / "guide.md").write_text("hello")

    def tearDown(self): self.tmp.cleanup()

    def test_authored_copy_and_idempotency(self):
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide")
        apply_promotion(plan)
        target = self.repo / "packages/skills/guide/guide.md"
        self.assertEqual(target.read_text(), "hello")
        manifest = json.loads((self.repo / "md-package.json").read_text())
        self.assertEqual(manifest["artifacts"], [{"kind": "skills", "key": "guide", "source": "packages/skills/guide", "target": "skills/guide"}])
        resolved = Resolver().resolve(self.repo)
        self.assertEqual([(item.identity, item.source, item.target) for item in resolved.artifacts], [("skills/guide", "packages/skills/guide", "skills/guide")])
        again = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide")
        self.assertEqual(again["operation"], "noop")

    def test_move(self):
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="wiki", name="guide", move=True)
        apply_promotion(plan)
        self.assertFalse(self.source.exists())
        self.assertTrue((self.repo / "packages/wiki/guide").exists())

    def test_materialized_clean_and_capture(self):
        pinned = self.root / "authored"; pinned.mkdir(); (pinned / "a.md").write_text("base")
        material = self.root / "skills" / "a"; material.parent.mkdir(); material.mkdir(); (material / "a.md").write_text("base")
        info = {"source": str(pinned), "baseHash": content_hash(pinned), "origin": "@team/a"}
        plan = plan_promotion(material, destination_repo=self.repo, kind="skills", name="a", materialized=info)
        self.assertEqual(plan["source"]["path"], str(pinned.resolve()))
        (material / "a.md").write_text("fork")
        with self.assertRaises(PromotionError):
            plan_promotion(material, destination_repo=self.repo, kind="skills", name="a", materialized=info)
        plan = plan_promotion(material, destination_repo=self.repo, kind="skills", name="a", materialized=info, from_materialized=True)
        self.assertEqual(plan["source"]["provenance"]["origin"], "@team/a")

    def test_route_registry_and_stale_manifest(self):
        parent = self.root / "parent"; parent.mkdir(); registry = parent / "md-package.json"
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide", route_key="guide", route={"when": "x", "read": ["skills/guide"]}, registry_manifest=registry, registry_name="@x/guide")
        (self.repo / "md-package.json").write_text("{}")
        with self.assertRaises(PromotionError): apply_promotion(plan)
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide", route_key="guide", route={"when": "x", "read": ["skills/guide"]}, registry_manifest=registry, registry_name="x")
        apply_promotion(plan)
        self.assertEqual(json.loads(registry.read_text())["registry"]["x"]["path"], "../repo/md-package.json")

    def test_conflict_and_unrelated_dirty_allowed(self):
        target = self.repo / "packages/skills/guide"; target.parent.mkdir(parents=True); target.mkdir(); (target / "guide.md").write_text("other")
        with self.assertRaises(PromotionError): plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide")
        (self.repo / "notes.txt").write_text("unrelated")
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="new")
        apply_promotion(plan)
        self.assertEqual((self.repo / "notes.txt").read_text(), "unrelated")

    def test_manifest_write_failure_rolls_back_payload(self):
        plan = plan_promotion(self.source, destination_repo=self.repo, kind="skills", name="guide")
        plan["manifestEdits"][0]["after"] = {"bad": {"not": {"serializable": object()}}}
        with self.assertRaises(TypeError): apply_promotion(plan)
        self.assertFalse((self.repo / "packages/skills/guide").exists())

    def test_promotion_crash_recovery_restores_payload_manifests_and_moved_source(self):
        for crash_number in range(1, 4):
            with self.subTest(crash_number=crash_number):
                repo = self.root / f"target-{crash_number}"; repo.mkdir()
                source = self.root / f"source-{crash_number}"; source.mkdir()
                (source / "guide.md").write_text("original")
                (repo / "md-package.json").write_text('{"version": 1}\n')
                plan = plan_promotion(source, destination_repo=repo, kind="docs", name="guide", move=True)
                count = 0
                def interrupted(*args, **kwargs):
                    nonlocal count
                    journal_replace(*args, **kwargs)
                    count += 1
                    if count == crash_number:
                        raise KeyboardInterrupt("simulated process crash")
                with patch("md_packages.promotion.journal_replace", interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        apply_promotion(plan)
                self.assertEqual(len(recover_scope(repo)), 1)
                self.assertEqual(recover_scope(repo), [])
                self.assertEqual((source / "guide.md").read_text(), "original")
                self.assertEqual((repo / "md-package.json").read_text(), '{"version": 1}\n')
                self.assertFalse((repo / "packages/docs/guide").exists())

    def test_symlink_destination_and_transaction_ancestors_are_rejected(self):
        outside = self.root / "outside"; outside.mkdir()
        (self.repo / "packages").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            plan_promotion(self.source, destination_repo=self.repo, kind="docs", name="guide")
        self.assertEqual(list(outside.iterdir()), [])
