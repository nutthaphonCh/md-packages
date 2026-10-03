from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scope = self.root / "repo"
        self.scope.mkdir()
        (self.scope / "packages" / "skills" / "sample").mkdir(parents=True)
        (self.scope / "packages" / "skills" / "sample" / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
        self.manifest = self.scope / "md-package.json"
        self.manifest.write_text(json.dumps({
            "version": 1,
            "artifacts": [{"kind": "skills", "key": "sample", "source": "packages/skills/sample"}],
            "routing": {"start": {"when": "Start here", "read": ["skills/sample", "docs/readme.md"]}},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT / "src")
        return subprocess.run([sys.executable, "-m", "md_packages", *args], cwd=cwd or self.scope,
                              env=env, text=True, capture_output=True)

    def test_install_dry_run_idempotency_router_discovery_and_lookup(self) -> None:
        dry = self.run_cli("--json", "install", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.scope / ".md-lock.json").exists())
        self.assertFalse((self.scope / "ROUTER.md").exists())
        first = self.run_cli("install")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((self.scope / "skills" / "sample" / "SKILL.md").read_text(), "# Sample\n")
        self.assertEqual((self.scope / ".agents" / "skills" / "sample" / "SKILL.md").read_text(), "# Sample\n")
        self.assertEqual((self.scope / ".claude" / "skills" / "sample" / "SKILL.md").read_text(), "# Sample\n")
        router = (self.scope / "ROUTER.md").read_text()
        self.assertLess(router.index("skills/sample"), router.index("docs/readme.md"))
        lock_before = (self.scope / ".md-lock.json").read_bytes()
        again = self.run_cli("install")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.scope / ".md-lock.json").read_bytes(), lock_before)
        health = self.run_cli("doctor")
        self.assertEqual(health.returncode, 0, health.stdout + health.stderr)
        for command, identity in (("which", "skills/sample"), ("explain", "start")):
            result = self.run_cli("--json", command, identity)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["identity"], identity)
        listed = self.run_cli("--json", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)["artifacts"]), 1)

    def test_conflict_drift_and_locked_replay_safety(self) -> None:
        (self.scope / "ROUTER.md").write_text("private")
        conflict = self.run_cli("install")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("unmanaged", conflict.stderr)
        self.assertFalse((self.scope / ".md-lock.json").exists())
        (self.scope / "ROUTER.md").unlink()
        self.assertEqual(self.run_cli("install").returncode, 0)
        self.assertEqual(self.run_cli("install", "--locked").returncode, 0)
        (self.scope / "ROUTER.md").write_text("edited")
        self.assertNotEqual(self.run_cli("doctor").returncode, 0)
        self.assertNotEqual(self.run_cli("install", "--locked").returncode, 0)
        self.assertEqual((self.scope / "ROUTER.md").read_text(), "edited")
        (self.scope / "ROUTER.md").write_text("managed restored")

    def test_locked_requires_source_metadata_and_does_not_upgrade(self) -> None:
        self.assertNotEqual(self.run_cli("install", "--locked").returncode, 0)
        self.assertEqual(self.run_cli("install").returncode, 0)
        authored = self.scope / "packages" / "skills" / "sample" / "SKILL.md"
        authored.write_text("# Changed\n")
        result = self.run_cli("install", "--locked")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked source hash changed", result.stderr)
        self.assertEqual((self.scope / "skills" / "sample" / "SKILL.md").read_text(), "# Sample\n")

    def test_nested_all_and_replay_from_subdirectory(self) -> None:
        child = self.scope / "child"
        child.mkdir()
        (child / "md-package.json").write_text(json.dumps({"version": 1, "routing": {
            "child": {"read": ["skills/sample"]}}}))
        manifest = json.loads(self.manifest.read_text())
        manifest["nest"] = [{"path": "child"}]
        self.manifest.write_text(json.dumps(manifest))
        first = self.run_cli("install", "--all")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((child / "skills" / "sample" / "SKILL.md").exists())
        self.assertTrue((child / "ROUTER.md").exists())
        subdir = self.scope / "notes"
        subdir.mkdir()
        replay = self.run_cli("install", "--locked", "--all", cwd=subdir)
        self.assertEqual(replay.returncode, 0, replay.stderr)

    def test_promotion_dry_run_saved_plan_and_apply(self) -> None:
        source = self.root / "draft.md"
        source.write_text("# Draft\n")
        dry = self.run_cli("--json", "promote", str(source), "--as", "docs/draft", "--to-repo", str(self.scope), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((self.scope / "packages" / "docs" / "draft").exists())
        plan_path = self.root / "promotion.json"
        saved = self.run_cli("promote", str(source), "--as", "docs/draft", "--to-repo", str(self.scope), "--plan", str(plan_path))
        self.assertEqual(saved.returncode, 0, saved.stderr)
        applied = self.run_cli("apply", str(plan_path))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.scope / "packages" / "docs" / "draft").read_text(), "# Draft\n")
        listed = json.loads(self.run_cli("--json", "list").stdout)
        self.assertEqual({f"{x['kind']}/{x['key']}" for x in listed["artifacts"]}, {"skills/sample", "docs/draft"})

    def test_promote_materialized_capture_requires_explicit_flag(self) -> None:
        self.assertEqual(self.run_cli("install").returncode, 0)
        materialized = self.scope / "skills" / "sample"
        destination = self.root / "target"
        destination.mkdir()
        clean = self.run_cli("promote", str(materialized), "--as", "skills/shared", "--to-repo", str(destination), "--dry-run")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        (materialized / "SKILL.md").write_text("# Forked\n")
        refused = self.run_cli("promote", str(materialized), "--as", "skills/shared", "--to-repo", str(destination), "--dry-run")
        self.assertNotEqual(refused.returncode, 0)
        capture = self.run_cli("promote", str(materialized), "--as", "skills/shared", "--to-repo", str(destination),
                               "--from-materialized", "--dry-run")
        self.assertEqual(capture.returncode, 0, capture.stderr)

    def test_recover_and_nonzero_product_error(self) -> None:
        sys.path.insert(0, str(PROJECT / "src"))
        try:
            from md_packages.transaction import Journal
            journal = Journal(self.scope, "interrupted-cli")
            backup = journal.directory / "backup" / "ROUTER.md"
            backup.parent.mkdir(parents=True)
            backup.write_text("before")
            (self.scope / "ROUTER.md").write_text("after")
            journal.data["state"] = "staged"
            journal.data["operations"] = [{"path": "ROUTER.md", "backup": str(backup), "had_original": True}]
            journal.save()
        finally:
            sys.path.pop(0)
        recovered = self.run_cli("--json", "recover")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual((self.scope / "ROUTER.md").read_text(), "before")
        missing = self.run_cli("which", "skills/nope")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("no artifact or route", missing.stderr)

    def test_generated_descendants_routers_and_discovery_require_capture(self) -> None:
        self.assertEqual(self.run_cli("install").returncode, 0)
        destination = self.root / "target"; destination.mkdir()
        for relative in ("skills/sample/SKILL.md", "ROUTER.md", "router-extension.md", ".agents/skills/sample/SKILL.md", ".md-lock.json"):
            with self.subTest(relative=relative):
                base = ["promote", relative, "--to-repo", str(destination), "--dry-run"]
                missing_identity = self.run_cli(*base, "--from-materialized")
                self.assertNotEqual(missing_identity.returncode, 0)
                self.assertIn("--as", missing_identity.stderr)
                refused = self.run_cli(*base, "--as", "docs/capture")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("--from-materialized", refused.stderr)
                capture = self.run_cli("--json", *base, "--as", "docs/capture", "--from-materialized")
                self.assertEqual(capture.returncode, 0, capture.stderr)
                plan = json.loads(capture.stdout)["plan"]
                self.assertTrue(plan["source"]["capture"])
                self.assertEqual(set(plan["affectedScopes"]), {str(self.scope.resolve()), str(destination.resolve())})

    def test_generated_source_self_reference_is_rejected(self) -> None:
        self.assertEqual(self.run_cli("install").returncode, 0)
        manifest = json.loads(self.manifest.read_text())
        manifest["artifacts"][0]["source"] = "skills/sample"
        self.manifest.write_text(json.dumps(manifest))
        result = self.run_cli("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated content", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX executable permissions")
    def test_executable_modes_survive_install_and_locked_replay(self) -> None:
        script = self.scope / "packages/skills/sample/run.sh"
        script.write_text("#!/bin/sh\nprintf 'works'\n")
        script.chmod(0o755)
        installed = self.run_cli("install")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        for relative in ("skills/sample/run.sh", ".agents/skills/sample/run.sh", ".claude/skills/sample/run.sh"):
            path = self.scope / relative
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
            self.assertEqual(subprocess.check_output([str(path)], text=True), "works")
            path.unlink()
        replay = self.run_cli("install", "--locked")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        target = self.scope / "skills/sample/run.sh"
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        target.chmod(0o644)
        refused = self.run_cli("install", "--locked")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("mode was locally modified", refused.stderr)
        self.assertNotEqual(self.run_cli("doctor").returncode, 0)

    def test_shipped_lock_schema_matches_generated_and_rejects_malformed_records(self) -> None:
        self.assertEqual(self.run_cli("install").returncode, 0)
        schema = json.loads((PROJECT / "schemas/md-lock.schema.json").read_text())
        lock = json.loads((self.scope / ".md-lock.json").read_text())

        def validate(value, rule):
            if "$ref" in rule:
                return validate(value, schema["$defs"][rule["$ref"].split("/")[-1]])
            if "oneOf" in rule:
                matches = 0
                for choice in rule["oneOf"]:
                    try:
                        validate(value, choice)
                        matches += 1
                    except AssertionError:
                        pass
                self.assertEqual(matches, 1)
                return
            if "const" in rule:
                self.assertEqual(value, rule["const"])
            if "type" in rule:
                kinds = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
                types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
                self.assertTrue(any(type(value) is types[kind] for kind in kinds))
            if isinstance(value, dict):
                self.assertTrue(set(rule.get("required", [])) <= value.keys())
                properties = rule.get("properties", {})
                for key, item in value.items():
                    if "propertyNames" in rule:
                        validate(key, rule["propertyNames"])
                    if key in properties:
                        validate(item, properties[key])
                    elif rule.get("additionalProperties") is False:
                        self.fail(f"unknown property {key}")
                    elif isinstance(rule.get("additionalProperties"), dict):
                        validate(item, rule["additionalProperties"])
            if isinstance(value, list) and "items" in rule:
                for item in value:
                    validate(item, rule["items"])
            if isinstance(value, str):
                self.assertGreaterEqual(len(value), rule.get("minLength", 0))
                if "pattern" in rule:
                    self.assertIsNotNone(re.fullmatch(rule["pattern"], value))
            if type(value) is int:
                self.assertGreaterEqual(value, rule.get("minimum", value))
                self.assertLessEqual(value, rule.get("maximum", value))

        validate(lock, schema)
        malformed = [dict(lock, unexpected=True), dict(lock, version=2), dict(lock, outputs=[]),
                     dict(lock, outputs={"x": {"sha256": "bad"}}),
                     dict(lock, outputs={"x": {"sha256": "a" * 64, "mode": "755"}}),
                     dict(lock, resolution={"artifacts": []}),
                     {key: value for key, value in lock.items() if key != "outputs"}]
        for value in malformed:
            with self.assertRaises(AssertionError):
                validate(value, schema)


if __name__ == "__main__":
    unittest.main()
