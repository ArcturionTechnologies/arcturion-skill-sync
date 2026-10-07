# ArcturionSkillSync

Cross-harness skill installer for Claude Code, Codex and Hermes.

You write a skill once, as a folder with a `SKILL.md`. One JSON manifest says
which agents get which skills. ArcturionSkillSync links those folders into every
harness the agent runs in, checks every `SKILL.md` before it touches anything,
and keeps a small state file so it only ever removes links it created itself.

Python 3.10+, one file, one dependency (PyYAML, for strict frontmatter checks).

> **Portfolio project.** This is an open-source sample of the tooling behind
> Arcturion's multi-agent setup. It is not a commercial product and makes no
> claims about revenue or customers.

## Why this exists

Each AI coding harness looks for skills in its own place: Claude Code reads
`.claude/skills/`, Codex and other open-agent tools read `.agents/skills/`, and
Hermes reads the folders listed under `skills.external_dirs` in its config. Copy
skills into each place by hand and the copies drift. One agent quietly runs last
month's version of a skill, and a stale copy can even shadow the right one.

ArcturionSkillSync makes the manifest the only source of truth:

- **Links, never copies.** Every harness folder holds a symlink back to the one
  real skill folder, so an edit shows up everywhere at once.
- **Validate first, write second.** Bad YAML, missing `name`/`description`,
  unknown frontmatter keys, names that aren't lowercase-hyphenated, or angle
  brackets in a description: any of these stops the run before a single link is
  written.
- **Owns only what it made.** A `.arcturion-skill-sync.json` file in each skills
  folder records the links this tool created. It never deletes or overwrites a
  real file, a hand-made symlink, or a skill it doesn't own. Those show up as
  findings instead.
- **Hash-tracked baseline.** With schema version 2, every agent inherits one
  shared `universe_baseline`. The tool hashes each agent's resolved skill list
  and reports `baseline_resolution_drift` if any agent differs.
- **Check mode is the default.** `--check` changes nothing and exits `1` when
  anything is missing, stale or in conflict. `--apply` repairs what it safely can.

## Quickstart

```bash
git clone https://github.com/ArcturionTechnologies/arcturion-skill-sync.git
cd arcturion-skill-sync
python3 -m pip install -r requirements.txt

# 1. Check the bundled example. Nothing is linked yet, so this lists
#    missing_projection findings and exits 1.
python3 arcturion_skill_sync.py --manifest examples/basic/manifest.json --check

# 2. Create the links for both example agents
python3 arcturion_skill_sync.py --manifest examples/basic/manifest.json --apply

# 3. Check again: zero findings, exit 0
python3 arcturion_skill_sync.py --manifest examples/basic/manifest.json --check

ls -l examples/basic/agents/builder/.claude/skills/
```

The output is JSON: a `findings` list (empty means clean), a `changes` count,
the baseline hash, and each agent's resolved-skill hash.

## The manifest

```json
{
  "schema": "arcturion.skill-sync-assignments",
  "schema_version": 2,
  "universe_baseline": [
    {"name": "hello-world", "source": "skills/hello-world"},
    {"name": "release-notes", "source": "skills/release-notes"}
  ],
  "agents": {
    "builder":    {"root": "agents/builder/.claude/skills",    "inherits_universe_baseline": true},
    "researcher": {"root": "agents/researcher/.claude/skills", "inherits_universe_baseline": true}
  }
}
```

- Relative paths resolve against the manifest's folder; `~` and `$VARS` expand.
- `root` is the agent's Claude skills folder. The agent's home is two levels up,
  and `.agents/skills/` is filled in next to it.
- Schema version 2 forbids per-agent `assignments`, so every agent gets the same
  baseline. Schema version 1 allows per-agent `assignments` (or the older
  `{"home": ..., "skills": {...}}` form).
- Optional per agent: `hermes_config` (path to a Hermes `config.yaml`; the
  agent's `.agents/skills/` folder is added to `skills.external_dirs`) and
  `hermes_overrides` (replace a skill Hermes bundles with your version; the
  original is moved to `.arcturion-shadowed-skills/` so it can be restored).

## Findings you may see

| Code | Meaning |
| --- | --- |
| `invalid_manifest` | The manifest is malformed. Nothing is checked further. |
| `missing_skill` / `invalid_frontmatter` | A source skill folder is missing or its `SKILL.md` fails validation. |
| `missing_projection` | A link should exist but doesn't. `--apply` creates it. |
| `wrong_target` | A link this tool owns points somewhere else. `--apply` repoints it. |
| `stale_projection` | A link this tool owns is no longer in the manifest. `--apply` removes it. |
| `unowned_conflict` | Something the tool didn't create is in the way. Left untouched. |
| `unmanifested_projection` | A usable skill is present that the manifest doesn't list. Reported, never deleted. |
| `baseline_resolution_drift` | An agent's resolved skills don't match the shared baseline. |
| `missing_hermes_projection`, `hermes_*` | Hermes config or override problems. |

Exit code: `0` when there are no findings, `1` otherwise.

## Live harness checks (optional)

`--smoke` runs Hermes's own skill loader for each agent's profile and confirms
every baseline skill is discoverable under the right name and description.
`--codex-smoke` asks the `codex` CLI for its rendered prompt input and confirms
each skill appears. Neither runs by default, and the tests stub both.

| Variable | Default | Used for |
| --- | --- | --- |
| `ARC_ROOT` | `$HOME` | Home folder for schema-1 agents that omit `home` (`$ARC_ROOT/<agent>`) |
| `HERMES_REPO` | `~/.hermes/hermes-agent` | Hermes checkout imported by `--smoke` |
| `SKILL_SYNC_CODEX_PROBE` | `$skill-sync-probe` | Prompt token passed to `codex debug prompt-input` |

## Project layout

```
arcturion_skill_sync.py        the whole tool
requirements.txt               PyYAML
examples/basic/
  manifest.json                schema 2: one baseline, two agents
  skills/hello-world/SKILL.md  example skills
  skills/release-notes/SKILL.md
tests/test_arcturion_skill_sync.py   32 tests, stdlib unittest
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite builds everything in temporary folders, stubs the Hermes and Codex
runtimes, and needs no network.

## License

MIT. See [LICENSE](LICENSE).

Implementation is AI-assisted; architecture, requirements, and testing directed by Robert Lingoes.
