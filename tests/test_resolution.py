from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from md_packages.errors import ConflictError, ResolutionError, SourceSymlinkError
from md_packages.hashing import file_hash, tree_hash
from md_packages.paths import PathValidationError, normalize_target
from md_packages.resolver import Resolver, discover_ancestors


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def manifest(self, directory: Path, **body: object) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "md-package.json"
        path.write_text(json.dumps({"version": 1, **body}), encoding="utf-8")
        return path

    def artifact(self, directory: Path, name: str, value: str = "body") -> None:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(value, encoding="utf-8")

    def test_recursive_flatten_and_deterministic_plan(self) -> None:
        shared = self.root / "shared"
        self.artifact(shared, "a.md", "alpha")
        self.manifest(shared, artifacts=[{"kind": "skills", "key": "a", "source": "a.md"}])
        app = self.root / "app"
        self.artifact(app, "b.md", "bravo")
        self.manifest(app, registry={"shared": {"path": "../shared"}}, imports=["shared"],
                      artifacts=[{"kind": "wiki", "key": "b", "source": "b.md"}],
                      routing={"read-b": {"when": "B", "read": ["wiki/b"]}})
        one = Resolver().resolve(app)
        two = Resolver().resolve(app)
        self.assertEqual(one.to_dict(), two.to_dict())
        self.assertEqual([a.identity for a in one.artifacts], ["skills/a", "wiki/b"])
        self.assertEqual(one.routes[0].key, "read-b")

    def test_child_whole_record_override_and_tombstone(self) -> None:
        parent = self.root / "parent"
        self.artifact(parent, "shared.md", "old")
        self.manifest(parent, artifacts=[{"kind": "skills", "key": "build", "source": "shared.md"}],
                      routing={"build": {"when": "old", "read": ["skills/build"]}})
        child = parent / "child"
        self.artifact(child, "shared.md", "new")
        self.manifest(child, artifacts=[{"kind": "skills", "key": "build", "source": "shared.md"}],
                      routing={"build": {"disabled": True}})
        plan = Resolver().resolve(child)
        self.assertEqual(plan.artifacts[0].hash, file_hash(child / "shared.md"))
        self.assertEqual(plan.routes, ())
        self.assertEqual({r.identity for r in plan.replaced}, {"skills/build", "route/build"})

    def test_same_level_and_target_conflicts_fail(self) -> None:
        package = self.root / "package"
        self.artifact(package, "one.md")
        self.artifact(package, "two.md", "two")
        self.manifest(package, artifacts=[
            {"kind": "skills", "key": "same", "source": "one.md"},
            {"kind": "skills", "key": "same", "source": "two.md"},
        ])
        with self.assertRaises(ConflictError):
            Resolver().resolve(package)
        self.manifest(package, artifacts=[
            {"kind": "skills", "key": "one", "source": "one.md", "target": "docs/A"},
            {"kind": "wiki", "key": "two", "source": "two.md", "target": "docs/a/page.md"},
        ])
        with self.assertRaises(ConflictError):
            Resolver().resolve(package)

    def test_path_validation(self) -> None:
        for value in ("/absolute", "../escape", "C:/drive", ".md-lock.json", "ROUTER.md"):
            with self.assertRaises(PathValidationError):
                normalize_target(value)

    def test_import_cycle_and_graph_limits(self) -> None:
        a, b = self.root / "a", self.root / "b"
        self.manifest(a, registry={"b": {"path": "../b"}}, imports=["b"])
        self.manifest(b, registry={"a": {"path": "../a"}}, imports=["a"])
        with self.assertRaisesRegex(ResolutionError, "cycle"):
            Resolver().resolve(a)
        self.manifest(b)
        with self.assertRaisesRegex(ResolutionError, "maxGraphNodes"):
            Resolver(max_graph_nodes=1).resolve(a)

    def test_parent_depth_and_root_marker(self) -> None:
        self.manifest(self.root, name="top")
        child = self.root / "one" / "two"
        self.manifest(child, name="child")
        self.assertEqual(len(discover_ancestors(child, max_depth=3)), 2)
        (self.root / "one" / ".git").mkdir(parents=True)
        self.assertEqual(len(discover_ancestors(child, max_depth=3)), 1)

    def test_symlink_sources_and_deterministic_tree_hashes_are_rejected_or_stable(self) -> None:
        package = self.root / "package"
        self.artifact(package, "dir/a.md", "a")
        self.artifact(package, "dir/b.md", "b")
        first = tree_hash(package / "dir")
        second = tree_hash(package / "dir")
        self.assertEqual(first, second)
        try:
            os.symlink(package / "dir/a.md", package / "dir/link.md")
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(SourceSymlinkError):
            tree_hash(package / "dir")
        self.manifest(package, artifacts=[{"kind": "docs", "key": "bad", "source": "dir"}])
        with self.assertRaises(SourceSymlinkError):
            Resolver().resolve(package)

    def test_nested_package_boundary_inherits_and_applies_registered_package(self) -> None:
        shared, app, child = self.root / "shared", self.root / "app", self.root / "app/child"
        self.artifact(shared, "shared.md", "shared")
        self.manifest(shared, artifacts=[{"kind": "skills", "key": "shared", "source": "shared.md"}])
        self.artifact(app, "root.md", "root")
        self.manifest(app, registry={"shared": {"path": "../shared"}},
                      artifacts=[{"kind": "skills", "key": "root", "source": "root.md"}],
                      nest=[{"path": "child", "package": "shared"}])
        self.manifest(child)
        nested = Resolver().resolve(app).nested[0]
        self.assertEqual([item.identity for item in nested.artifacts], ["skills/root", "skills/shared"])


if __name__ == "__main__":
    unittest.main()
