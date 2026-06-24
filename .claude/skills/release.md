---
name: release
description: Release workflow for netbox-rack-design — lint, test, version bump (3 places), changelog, compatibility, commit, then tag-push to trigger CI/CD publish to PUBLIC PyPI via trusted publisher. Certification-grade.
user_invocable: true
---

# Release netbox-rack-design (public NetBox plugin)

Unlike an internal tool, this plugin is **published by CI/CD, not by you**.
You never run `twine` and never handle PyPI credentials. The release mechanism is:

> **push a git tag `vX.Y.Z` → `.github/workflows/publish-pypi.yaml` builds the
> dist and publishes to public PyPI (https://pypi.org/p/netbox-rack-design) via a
> PyPI _trusted publisher_ (OIDC, `id-token: write`, environment `pypi`).**

Docs deploy separately and automatically: `mkdocs.yaml` runs `mkdocs gh-deploy`
on every push to `main`. So your job is: get `main` green and correct, then tag.

Related skills: **netbox-plugin-packaging-certification** (the packaging/cert rules
this enforces) and **netbox-plugin-testing** (how to run the suite locally).

## One-time prerequisites (verify before the FIRST release)

1. **PyPI trusted publisher configured.** On https://pypi.org, the project
   `netbox-rack-design` must trust: repo `ravenrs/netbox-rack-design`, workflow
   `publish-pypi.yaml`, environment `pypi`. For the very first release the project
   doesn't exist yet → add a **"pending publisher"** under your PyPI account
   (Publishing → Add a pending publisher) with those exact values.
2. **GitHub environment `pypi`** exists in repo settings (the publish job pins
   `environment: name: pypi`).
3. The git tag version and `pyproject.toml` `version` MUST be identical — the build
   uses the pyproject version, not the tag, so a mismatch ships a wheel whose
   version ≠ the tag.

## Before starting

1. Read `CHANGELOG.md` (the `## [Unreleased]` items + the release-notes template at
   the bottom) and `COMPATIBILITY.md`.
2. Decide if a release is needed — only changes under `netbox_rack_design/` warrant
   one. Changes to `tests/`, `docs/`, `README.md`, `.claude/`, CI do not.
   ```bash
   git -C /Users/petr.voronov/Documents/Developing/netbox-rack-design \
     diff $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD)..HEAD \
     --name-only -- netbox_rack_design/
   # empty → no release needed
   ```

## Steps

### 1. Lint + test locally (mirror CI — do not proceed on failure)

Lint with the **same Ruff version CI uses** (`.github/workflows/ci.yaml` pins it):
```bash
cd /Users/petr.voronov/Documents/Developing/netbox-rack-design
pip install 'ruff==0.11.12' >/dev/null   # match ci.yaml, NOT pyproject's test extra
ruff check netbox_rack_design/
```
> ⚠ Known drift to fix: `ci.yaml` pins `ruff==0.11.12` but `pyproject.toml`
> `[test]` pins `ruff==0.14.14`. Align them, then use the agreed version here.

Run the NetBox test suite the way CI does, against the dev NetBox checkout
(`../netbox-contribute`). Per **netbox-plugin-testing**, from the netbox dir:
```bash
cd /Users/petr.voronov/Documents/Developing/netbox-contribute/netbox
NETBOX_CONFIGURATION=netbox.configuration_testing \
  python manage.py test netbox_rack_design --keepdb -v 2
# and confirm no missing migrations:
NETBOX_CONFIGURATION=netbox.configuration_testing \
  python manage.py makemigrations --check netbox_rack_design
```

### 2. Determine version (semver)

- MAJOR — breaking changes (model/API removals, incompatible migrations)
- MINOR — new features, backward-compatible
- PATCH — bug fixes only

Ask the user if the bump is ambiguous.

### 3. Bump version in BOTH source places (keep in sync)

`PluginConfig.version` reads `__version__`, so there are two files to edit:
- `netbox_rack_design/__init__.py` → `__version__ = "X.Y.Z"`
- `pyproject.toml` → `version = "X.Y.Z"`

These two **and** the `vX.Y.Z` tag (step 7) must all be the same string.

### 4. Update compatibility (if NetBox support changed)

If `min_version`/`max_version` in `__init__.py` changed, update:
- `netbox_rack_design/__init__.py` (`min_version` / `max_version`)
- `COMPATIBILITY.md` — add a row for the new plugin version.

> ⚠ Known drift to fix: `ci.yaml` clones NetBox `--branch v4.5`, but the plugin
> declares `max_version = "4.4.99"` and pyproject classifies Django 5.1 (= NetBox
> 4.4). Either bump the declared compat or point CI at the matching NetBox branch —
> certification checks that declared compat matches what you actually test.

### 5. Update CHANGELOG.md

Move `## [Unreleased]` items into a new dated section using the template at the
bottom of the file. Per certification, include a **narrative Release Summary** and a
bold **Breaking Changes** section when applicable (with migration guidance):
```markdown
## [X.Y.Z] - YYYY-MM-DD

### Release Summary
<one-paragraph narrative: release type + highlights>

### **Breaking Changes**     <!-- omit if none -->
- ...

### Added / Fixed / Changed / Deprecated / Removed / Security
- ... (with #issue references)
```
> The current `0.1.0` entry still references a cookiecutter `Rackdesign model` —
> our real models are `Design` / `DesignGroup` / `DesignPlacement`. Write accurate
> notes; don't copy the placeholder.

### 6. Commit on a branch → PR → merge to main

Never commit the bump straight to `main` if the repo protects it. Branch, PR, merge:
```bash
cd /Users/petr.voronov/Documents/Developing/netbox-rack-design
git checkout -b release/X.Y.Z
git add netbox_rack_design/__init__.py pyproject.toml CHANGELOG.md COMPATIBILITY.md
git commit -m "Release vX.Y.Z"   # end with the Co-Authored-By trailer
git push -u origin release/X.Y.Z
gh pr create --fill --base main
```

### 7. Wait for CI green on main, then tag

After the PR merges, confirm CI (lint + the Python 3.12/3.13/3.14 NetBox matrix)
is green on `main` — publishing a tag whose tree fails CI ships a broken release.
```bash
gh run list --branch main --limit 3
git checkout main && git pull
git tag vX.Y.Z          # MUST equal the pyproject/__init__ version
git push origin vX.Y.Z  # ← this triggers publish-pypi.yaml
```

### 8. Watch the publish workflow

```bash
gh run watch $(gh run list --workflow publish-pypi.yaml --limit 1 --json databaseId -q '.[0].databaseId')
```
The `publish-to-pypi` job runs only on the tag ref and uploads via the trusted
publisher. If it fails on auth, the trusted-publisher/pending-publisher setup
(prerequisites) is missing or its repo/workflow/environment values don't match.

### 9. Verify on PyPI

```bash
pip index versions netbox-rack-design    # or open https://pypi.org/project/netbox-rack-design/
# the new X.Y.Z must be listed
```

### 10. Create a GitHub Release

Surface the changelog to users and mark the release point:
```bash
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
```
(or `--generate-notes`). Adjust the sed range to the new section.

### 11. Confirm docs deployed

`mkdocs.yaml` auto-runs `mkdocs gh-deploy --force` on push to `main`, so docs
update on merge (step 6), not on the tag. Verify the GitHub Pages site reflects the
release; if it didn't run, check the `ci`/mkdocs workflow run.

### 12. Certification note (per-release)

NetBox plugin certification is granted **per release**, and only **GitHub-hosted
CI** counts. After a release that targets certification, ensure: CI was green, the
declared `min/max_version` matches the NetBox branch CI tested, docs published, and
COMPATIBILITY.md is current. See **netbox-plugin-packaging-certification** for the
full checklist and the application steps.
