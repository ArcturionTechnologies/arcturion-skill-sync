import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import arcturion_skill_sync as chs


class CrossHarnessSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent_home = self.root / "builder"
        self.agent_home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def skill(self, name="alpha", description="Use for alpha work.", frontmatter_name=None):
        path = self.root / "sources" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name or name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
        return path

    def manifest(self, skills, **agent_fields):
        path = self.root / "manifest.json"
        payload = {
            "version": 1,
            "agents": {
                "builder": {
                    "home": str(self.agent_home),
                    "skills": {name: str(source) for name, source in skills.items()},
                    **agent_fields,
                }
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def canonical_manifest(self, skills, hermes_overrides=None, hermes_config=None):
        root = self.agent_home / ".claude/skills"
        path = self.root / "canonical-manifest.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "agents": {
                "builder": {
                    "root": str(root),
                    "assignments": [
                        {"name": name, "source": str(source)}
                        for name, source in skills.items()
                    ],
                    "hermes_overrides": hermes_overrides or [],
                    **({"hermes_config": str(hermes_config)} if hermes_config else {}),
                }
            },
        }), encoding="utf-8")
        return path

    def universal_manifest(self, skills, agents=("builder", "researcher")):
        path = self.root / "universal-manifest.json"
        payload = {
            "schema": "arcturion.skill-sync-assignments",
            "schema_version": 2,
            "universe_baseline": [
                {"name": name, "source": str(source)}
                for name, source in skills.items()
            ],
            "agents": {},
        }
        for agent in agents:
            home = self.root / agent
            home.mkdir(exist_ok=True)
            payload["agents"][agent] = {
                "root": str(home / ".claude/skills"),
                "inherits_universe_baseline": True,
            }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_apply_projects_both_adapters_and_is_idempotent(self):
        source = self.skill()
        manifest = self.manifest({"alpha": source})

        first = chs.run(manifest, apply=True)
        second = chs.run(manifest, apply=True)
        checked = chs.run(manifest, apply=False)

        self.assertEqual([], first["findings"])
        self.assertEqual([], second["findings"])
        self.assertEqual([], checked["findings"])
        for relative in (".claude/skills/alpha", ".agents/skills/alpha"):
            projected = self.agent_home / relative
            self.assertTrue(projected.is_symlink())
            self.assertEqual(source.resolve(), projected.resolve())

    def test_accepts_canonical_assignment_schema_and_named_manifest_flag(self):
        source = self.skill()
        manifest = self.canonical_manifest({"alpha": source})

        applied = chs.run(manifest, apply=True)
        command = subprocess.run(
            [sys.executable, str(MODULE_DIR / "arcturion_skill_sync.py"), "--check", "--manifest", str(manifest)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual([], applied["findings"])
        self.assertEqual(0, command.returncode, command.stdout + command.stderr)
        self.assertEqual([], json.loads(command.stdout)["findings"])

    def test_universe_baseline_projects_the_same_resolved_loadout_for_every_agent(self):
        alpha = self.skill("alpha")
        beta = self.skill("beta")
        manifest = self.universal_manifest({"alpha": alpha, "beta": beta})

        result = chs.run(manifest, apply=True)

        self.assertEqual([], result["findings"])
        self.assertEqual(2, result["baseline_skill_count"])
        self.assertEqual(1, len(set(result["resolved_skill_hashes"].values())))
        for agent in ("builder", "researcher"):
            for name, source in (("alpha", alpha), ("beta", beta)):
                projected = self.root / agent / ".agents/skills" / name
                self.assertTrue(projected.is_symlink(), (agent, name))
                self.assertEqual(source.resolve(), projected.resolve())

    def test_universe_baseline_rejects_an_agent_that_does_not_inherit_it(self):
        manifest = self.universal_manifest({"alpha": self.skill("alpha")})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["agents"]["researcher"]["inherits_universe_baseline"] = False
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        result = chs.run(manifest, apply=False)

        self.assertTrue(any(
            finding["code"] == "missing_universe_baseline_inheritance" and finding["agent"] == "researcher"
            for finding in result["findings"]
        ))

    def test_universe_baseline_rejects_agent_specific_assignments(self):
        manifest = self.universal_manifest({"alpha": self.skill("alpha")})
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["agents"]["researcher"]["assignments"] = [
            {"name": "beta", "source": str(self.skill("beta"))}
        ]
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        result = chs.run(manifest, apply=False)

        self.assertTrue(any(
            finding["code"] == "agent_specific_assignment_forbidden" and finding["agent"] == "researcher"
            for finding in result["findings"]
        ))

    def test_invalid_skill_frontmatter_is_reported_without_writes(self):
        source = self.skill(description="")
        manifest = self.manifest({"alpha": source})

        result = chs.run(manifest, apply=True)

        self.assertTrue(any(f["code"] == "invalid_frontmatter" for f in result["findings"]))
        self.assertFalse((self.agent_home / ".agents").exists())

    def test_frontmatter_name_must_be_lowercase_hyphenated_but_may_differ_from_alias(self):
        valid_alias = self.skill("cc-skill-frontend-patterns", frontmatter_name="frontend-patterns")
        valid_manifest = self.manifest({"cc-skill-frontend-patterns": valid_alias})
        self.assertEqual([], chs.run(valid_manifest, apply=True)["findings"])

        invalid = self.skill("bad", frontmatter_name="Bad_Name")
        invalid_manifest = self.manifest({"bad": invalid})
        result = chs.run(invalid_manifest, apply=False)

        self.assertTrue(any(f["code"] == "invalid_frontmatter" for f in result["findings"]))

    def test_rejects_malformed_yaml_frontmatter(self):
        source = self.skill()
        (source / "SKILL.md").write_text(
            "---\nname: [unterminated\ndescription: broken\n---\n", encoding="utf-8"
        )

        result = chs.run(self.manifest({"alpha": source}), apply=False)

        self.assertTrue(any(f["code"] == "invalid_frontmatter" for f in result["findings"]))

    def test_rejects_unexpected_frontmatter_keys(self):
        source = self.skill()
        (source / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: valid\ntype: reference\n---\n", encoding="utf-8"
        )

        result = chs.run(self.manifest({"alpha": source}), apply=False)

        self.assertTrue(any("unexpected" in f["detail"] for f in result["findings"]))

    def test_accepts_string_trigger_frontmatter(self):
        source = self.skill()
        (source / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: valid\ntrigger: /alpha\n---\n", encoding="utf-8"
        )

        result = chs.run(self.manifest({"alpha": source}), apply=False)

        self.assertFalse(any(f["code"] == "invalid_frontmatter" for f in result["findings"]))

    def test_rejects_angle_brackets_in_description(self):
        source = self.skill(description="Use <unsafe> markup.")

        result = chs.run(self.manifest({"alpha": source}), apply=False)

        self.assertTrue(any("angle brackets" in f["detail"] for f in result["findings"]))

    def test_rejects_overlength_name_and_description(self):
        long_name = "a" * 65
        bad_name = self.skill("bad-name", frontmatter_name=long_name)
        name_result = chs.run(self.manifest({"bad-name": bad_name}), apply=False)
        self.assertTrue(any("64" in f["detail"] for f in name_result["findings"]))

        bad_description = self.skill("bad-description", description="x" * 1025)
        description_result = chs.run(
            self.manifest({"bad-description": bad_description}), apply=False
        )
        self.assertTrue(any("1024" in f["detail"] for f in description_result["findings"]))

    def test_missing_yaml_dependency_fails_closed(self):
        source = self.skill()
        with mock.patch.object(chs, "yaml", None):
            result = chs.run(self.manifest({"alpha": source}), apply=False)

        self.assertTrue(any(f["code"] == "validator_dependency_missing" for f in result["findings"]))

    def test_metadata_requires_string_key_and_value_mapping(self):
        for index, metadata_yaml in enumerate(("[one, two]", "{owner: 42}", "{7: owner}")):
            with self.subTest(metadata=metadata_yaml):
                skill_name = f"metadata-test-{index}"
                source = self.skill(skill_name)
                (source / "SKILL.md").write_text(
                    f"---\nname: {skill_name}\ndescription: valid\n"
                    f"metadata: {metadata_yaml}\n---\n",
                    encoding="utf-8",
                )
                result = chs.run(self.manifest({skill_name: source}), apply=False)
                self.assertTrue(any(
                    f["code"] == "invalid_frontmatter" and "metadata" in f["detail"]
                    for f in result["findings"]
                ))

    def test_allowed_tools_must_be_string(self):
        source = self.skill("tools-test")
        (source / "SKILL.md").write_text(
            "---\nname: tools-test\ndescription: valid\nallowed-tools: [Read, Write]\n---\n",
            encoding="utf-8",
        )

        invalid = chs.run(self.manifest({"tools-test": source}), apply=False)
        self.assertTrue(any(
            f["code"] == "invalid_frontmatter" and "allowed-tools" in f["detail"]
            for f in invalid["findings"]
        ))

        (source / "SKILL.md").write_text(
            "---\nname: tools-test\ndescription: valid\nallowed-tools: Read Write\n---\n",
            encoding="utf-8",
        )
        valid = chs.run(self.manifest({"tools-test": source}), apply=False)
        self.assertFalse(any(f["code"] == "invalid_frontmatter" for f in valid["findings"]))

    def test_unmanifested_usable_projection_is_reported_and_never_deleted(self):
        source = self.skill()
        extra_source = self.skill("extra")
        manifest = self.manifest({"alpha": source})
        self.assertEqual([], chs.run(manifest, apply=True)["findings"])
        extra = self.agent_home / ".agents/skills/extra"
        extra.symlink_to(extra_source)
        support = self.agent_home / ".agents/skills/_shared"
        support.mkdir()
        (support / "SKILL.md").write_text(
            "---\nname: shared\ndescription: support\n---\n", encoding="utf-8"
        )

        checked = chs.run(manifest, apply=False)
        applied = chs.run(manifest, apply=True)

        self.assertEqual(1, sum(f["code"] == "unmanifested_projection" for f in checked["findings"]))
        self.assertEqual(1, sum(f["code"] == "unmanifested_projection" for f in applied["findings"]))
        self.assertTrue(extra.is_symlink())
        self.assertTrue(support.is_dir())

    def test_wrong_unowned_symlink_is_preserved_as_conflict(self):
        source = self.skill()
        other = self.skill("other")
        destination = self.agent_home / ".agents/skills/alpha"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(other)
        manifest = self.manifest({"alpha": source})

        result = chs.run(manifest, apply=True)

        self.assertTrue(any(f["code"] == "unowned_conflict" for f in result["findings"]))
        self.assertEqual(other.resolve(), destination.resolve())

    def test_stale_owned_link_is_removed_but_unowned_entry_is_preserved(self):
        alpha = self.skill()
        manifest = self.manifest({"alpha": alpha})
        self.assertEqual([], chs.run(manifest, apply=True)["findings"])
        unowned = self.agent_home / ".agents/skills/unowned"
        unowned.mkdir()

        empty_manifest = self.manifest({})
        result = chs.run(empty_manifest, apply=True)

        self.assertEqual([], result["findings"])
        self.assertFalse((self.agent_home / ".agents/skills/alpha").exists())
        self.assertTrue(unowned.is_dir())

    def test_hermes_external_dir_is_added_without_losing_existing_entries(self):
        source = self.skill()
        config = self.root / "hermes" / "config.yaml"
        config.parent.mkdir()
        config.write_text(
            "model: test\nskills:\n  external_dirs:\n  - /already/here\n  template_vars: true\ntimezone: UTC\n",
            encoding="utf-8",
        )
        manifest = self.manifest({"alpha": source}, hermes_config=str(config))

        result = chs.run(manifest, apply=True)
        text = config.read_text(encoding="utf-8")

        self.assertEqual([], result["findings"])
        self.assertIn("- /already/here", text)
        self.assertIn(str(self.agent_home / ".agents/skills"), text)
        self.assertIn("model: test", text)
        self.assertIn("timezone: UTC", text)

    def test_hermes_real_yaml_parser_accepts_standard_four_space_list_indent(self):
        source = self.skill()
        config = self.root / "hermes" / "config.yaml"
        config.parent.mkdir()
        config.write_text(
            "model: test\nskills:\n  external_dirs:\n    - /already/here\n  template_vars: true\n",
            encoding="utf-8",
        )
        manifest = self.manifest({"alpha": source}, hermes_config=str(config))

        result = chs.run(manifest, apply=True)
        parsed = chs.yaml.safe_load(config.read_text(encoding="utf-8"))

        self.assertEqual([], result["findings"])
        self.assertCountEqual(
            ["/already/here", str((self.agent_home / ".agents/skills").resolve())],
            parsed["skills"]["external_dirs"],
        )

    def test_hermes_malformed_yaml_is_reported_without_rewrite(self):
        source = self.skill()
        config = self.root / "hermes" / "config.yaml"
        config.parent.mkdir()
        original = "skills:\n  external_dirs: [unterminated\n"
        config.write_text(original, encoding="utf-8")
        manifest = self.manifest({"alpha": source}, hermes_config=str(config))

        result = chs.run(manifest, apply=True)

        self.assertTrue(any(item["code"] == "invalid_hermes_config" for item in result["findings"]))
        self.assertEqual(original, config.read_text(encoding="utf-8"))

    def test_hermes_override_is_recoverable_and_idempotent(self):
        source = self.skill("spotify")
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/media/spotify"
        local.mkdir(parents=True)
        (local / "ORIGINAL").write_text("bundled", encoding="utf-8")
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
            config,
        )
        runner = lambda hermes_home, name: {
            "prompt": "spotify\nUse for alpha work.",
            "view": {"success": True, "content": "Use for alpha work."},
        }

        first = chs.run(manifest, apply=True, hermes_runner=runner)
        second = chs.run(manifest, apply=True, hermes_runner=runner)
        checked = chs.run(manifest, apply=False, hermes_runner=runner)

        backup = profile / ".arcturion-shadowed-skills/media/spotify"
        self.assertEqual([], first["findings"])
        self.assertEqual([], second["findings"])
        self.assertEqual([], checked["findings"])
        self.assertTrue(local.is_symlink())
        self.assertEqual(source.resolve(), local.resolve())
        self.assertEqual("bundled", (backup / "ORIGINAL").read_text(encoding="utf-8"))

    def test_hermes_runtime_prompt_verifies_override_name_and_description(self):
        source = self.skill("spotify", description="Shared Spotify skill.")
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/media/spotify"
        local.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
            config,
        )
        homes = []

        result = chs.run(
            manifest,
            hermes_runner=lambda hermes_home, name: homes.append(hermes_home) or {
                "prompt": "spotify\nShared Spotify skill.",
                "view": {"success": True, "content": "Shared Spotify skill."},
            },
        )

        self.assertEqual([profile.resolve()], homes)
        self.assertFalse(any(f["code"].startswith("hermes_runtime_") for f in result["findings"]))
        self.assertEqual(
            [{"agent": "builder", "skill": "spotify", "name": "spotify", "description": "Shared Spotify skill."}],
            result["hermes_runtime_verified"],
        )

    def test_hermes_runtime_prompt_reports_shadowed_override(self):
        source = self.skill("airtable", description="Shared Airtable skill.")
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/productivity/airtable"
        local.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"airtable": source},
            [{"name": "airtable", "local_path": str(local)}],
            config,
        )

        result = chs.run(manifest, hermes_runner=lambda hermes_home, name: {
            "prompt": "airtable\nShared Airtable skill.",
            "view": {"success": False, "error": "Ambiguous skill name 'airtable'."},
        })

        self.assertTrue(any(f["code"] == "hermes_runtime_shadowed" for f in result["findings"]))

    def test_hermes_runtime_accepts_hermes_truncated_prompt_description(self):
        description = "Shared Spotify description that Hermes deliberately truncates after sixty characters."
        source = self.skill("spotify", description=description)
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/media/spotify"
        local.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
            config,
        )

        result = chs.run(manifest, hermes_runner=lambda hermes_home, name: {
            "prompt": f"spotify: {description[:57]}...",
            "view": {"success": True, "content": description},
        })

        self.assertFalse(any(f["code"].startswith("hermes_runtime_") for f in result["findings"]))

    def test_hermes_runtime_prompt_reports_runner_failure(self):
        source = self.skill("spotify")
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/media/spotify"
        local.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
            config,
        )

        def failed_runner(hermes_home, name):
            raise RuntimeError("Hermes import failed")

        result = chs.run(manifest, hermes_runner=failed_runner)

        self.assertTrue(any(
            f["code"] == "hermes_runtime_unverified" and "import failed" in f["detail"]
            for f in result["findings"]
        ))

    def test_hermes_runtime_prompt_reports_wrong_skill_view_content(self):
        source = self.skill("spotify", description="Shared Spotify skill.")
        profile = self.root / "hermes-profile"
        config = profile / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
        local = profile / "skills/media/spotify"
        local.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
            config,
        )

        result = chs.run(manifest, hermes_runner=lambda hermes_home, name: {
            "prompt": "spotify\nShared Spotify skill.",
            "view": {"success": True, "content": "Bundled Spotify instructions."},
        })

        self.assertTrue(any(f["code"] == "hermes_runtime_shadowed" for f in result["findings"]))

    def test_hermes_override_backup_conflict_preserves_local_directory(self):
        source = self.skill("airtable")
        profile = self.root / "hermes-profile"
        local = profile / "skills/productivity/airtable"
        local.mkdir(parents=True)
        backup = profile / ".arcturion-shadowed-skills/productivity/airtable"
        backup.mkdir(parents=True)
        manifest = self.canonical_manifest(
            {"airtable": source},
            [{"name": "airtable", "local_path": str(local)}],
        )

        result = chs.run(manifest, apply=True)

        self.assertTrue(any(f["code"] == "hermes_backup_conflict" for f in result["findings"]))
        self.assertTrue(local.is_dir())
        self.assertFalse(local.is_symlink())

    def test_wrong_hermes_override_symlink_is_preserved(self):
        source = self.skill("spotify")
        other = self.skill("other")
        local = self.root / "hermes-profile/skills/media/spotify"
        local.parent.mkdir(parents=True)
        local.symlink_to(other)
        manifest = self.canonical_manifest(
            {"spotify": source},
            [{"name": "spotify", "local_path": str(local)}],
        )

        result = chs.run(manifest, apply=True)

        self.assertTrue(any(f["code"] == "wrong_hermes_override" for f in result["findings"]))
        self.assertEqual(other.resolve(), local.resolve())

    def test_codex_surface_accepts_projected_alias_for_resolved_symlink_source(self):
        source = self.skill("vault-notes")
        projection_root = self.agent_home / ".agents/skills"
        projection_root.mkdir(parents=True)
        (projection_root / "vault-notes").symlink_to(source)
        prompt = json.dumps([
            {"role": "system", "content": (
                "### Skill roots\n- `r0` = `" + str(projection_root) + "`\n"
                "### Available skills\n- vault-notes: Use vault notes "
                "(file: r0/vault-notes/SKILL.md)"
            )}
        ])
        completed = subprocess.CompletedProcess(
            args=["codex"], returncode=0, stdout=prompt, stderr="",
        )
        agent = {
            "name": "builder", "home": self.agent_home,
            "skills": {"vault-notes": source.resolve()},
        }

        with mock.patch.object(chs.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(chs.subprocess, "run", return_value=completed):
            findings, verified = chs.validate_codex_surface(agent)

        self.assertEqual([], findings)
        self.assertEqual({"agent": "builder", "skills": 1}, verified)


    def test_bundled_example_projects_cleanly(self):
        import shutil
        example = MODULE_DIR / "examples" / "basic"
        copy = self.root / "example"
        shutil.copytree(example, copy, ignore=shutil.ignore_patterns("agents"))
        manifest = copy / "manifest.json"

        before = chs.run(manifest, apply=False)
        applied = chs.run(manifest, apply=True)
        after = chs.run(manifest, apply=False)

        self.assertTrue(any(f["code"] == "missing_projection" for f in before["findings"]))
        self.assertEqual([], applied["findings"])
        self.assertEqual([], after["findings"])
        self.assertEqual(2, after["baseline_skill_count"])
        for agent in ("builder", "researcher"):
            for adapter in (".claude/skills", ".agents/skills"):
                self.assertTrue((copy / "agents" / agent / adapter / "hello-world").is_symlink())

    def test_legacy_home_defaults_to_arc_root(self):
        source = self.skill()
        path = self.root / "legacy.json"
        path.write_text(json.dumps({"version": 1, "agents": {"solo": {"skills": {"alpha": str(source)}}}}),
                        encoding="utf-8")
        arc_root = self.root / "arc"
        with mock.patch.dict(chs.os.environ, {"ARC_ROOT": str(arc_root)}):
            result = chs.run(path, apply=True)

        self.assertEqual([], result["findings"])
        self.assertTrue((arc_root / "solo" / ".claude/skills/alpha").is_symlink())


if __name__ == "__main__":
    unittest.main()
