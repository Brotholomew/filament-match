# Publishing Filament Match to Orca Cloud

## Version source of truth

`plugin/filament_match.py` is the canonical version source. Its PEP 723
metadata `version` and its `PLUGIN_VERSION` constant must agree. Docs, the
static entry test and both generated artifacts are checked against it:

```sh
python3 plugin/check_version.py
```

The GitHub release tag supplies the version for an upload at release time:

```sh
python3 plugin/build_hub_file.py --version "${GITHUB_REF_NAME#v}"
```

`--version` rewrites both the file header and `PLUGIN_VERSION`; the build fails
if either substitution does not happen exactly once. The artifact is validated
again before upload:

```sh
python3 plugin/check_artifact.py plugin/hub/filament_match_any.py \
  --version "${GITHUB_REF_NAME#v}"
```

## Build the upload artifact

```sh
python3 plugin/build_hub_file.py
```

It writes:

```text
plugin/hub/filament_match_any.py
```

`_any.py` is the required Orca Cloud target-OS suffix for a pure-Python plugin
that runs on macOS, Windows and Linux. Use `--out PATH` for a dry run without
replacing the normal artifact.

## Initial listing

In Orca Cloud → **Plugins → Shared → +**, use:

| Field | Value |
| --- | --- |
| Type | **Script** |
| Plugin files | `plugin/hub/filament_match_any.py` |
| Name | `Filament Match` |
| Version | `0.8.1` |
| Min. OrcaSlicer version | `2.5.2` |
| Tags | `filament, spoolman, printer, ams, utility` |
| Public plugin | your choice |

Suggested 0.8.1 changelog:

```text
Filament Match 0.8.1

Open Filament Database matching is now enabled by default. Filament Match can
find a ready-to-use preset for a new filament from the Open Filament Database
without manual preset creation, alongside the presets already installed in Orca.
Spoolman users can still sync per-kg prices with their chosen instance.

The plugin description now explains the OFD-first matching flow and the
optional Spoolman price sync.
```

The metadata in the generated file is:

```toml
# [tool.orcaslicer.plugin]
# id = "filament-match"
# name = "Filament Match"
# version = "0.8.1"
# author = "brotholomew"
```

Edit `plugin/build_hub_file.py` to change the author or description, then
regenerate the file. The release tag override changes the version, not the
name, description or author.

## Updating the listing metadata

The GitHub publishing credential is accepted only by
`/api/v1/plugin-publish/releases`. It returns `401 invalid_token` for
`/api/v1/plugins/*`, so metadata edits need an Orca Cloud account session.

Use the helper in [dev/ORCA_HUB.md](dev/ORCA_HUB.md):

```sh
python3 plugin/dev/orca_hub.py login
python3 plugin/dev/orca_hub.py show
python3 plugin/dev/orca_hub.py update --description-file plugin/HUB_DESCRIPTION.md
```

The session is stored in `.secrets/orca_cloud.json`, which is ignored by git.
It is created with owner-only permissions (`0600`); the directory is `0700`.
It is separate from OrcaSlicer's own session and is not needed by the released
plugin.

Orca Cloud limits the listing description to **10,000 characters and 10,000
UTF-8 bytes**. `orca_hub.py update` enforces both before sending or printing a
dry run. Check the markdown source locally with:

```sh
python3 plugin/check_hub_description.py plugin/HUB_DESCRIPTION.md
```

A multi-byte character such as `—` counts as one character but three bytes, so
a file that fits the character limit can still be rejected by the API.

## GitHub publishing

After the listing exists, open **Edit plugin → GitHub publishing**, enter
`owner/repository`, and connect it. The release workflow is
[`.github/workflows/publish-orcacloud.yml`](../.github/workflows/publish-orcacloud.yml).

The workflow runs on published GitHub releases, skipping prereleases. It:

- skips prereleases;
- runs the test suite before publishing;
- builds the artifact for the tag version and validates it;
- attaches `filament_match_any.py` and `filament_match_any.py.sha256` to the
  GitHub release (`contents: write`);
- requests a GitHub OIDC token, checks the HTTP response and token before use;
- truncates the release notes by Unicode character, not bytes;
- sends the metadata with `--form-string` and keeps plugin paths in a quoted
  bash array.

The relevant workflow environment and file loop:

```yaml
env:
  PLUGIN_FILES: "plugin/hub/filament_match_any.py"
```

```bash
IFS=' ' read -r -a plugin_patterns <<< "${PLUGIN_FILES}"
plugin_files=()
for pattern in "${plugin_patterns[@]}"; do
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    plugin_files+=("$file")
  done < <(compgen -G "$pattern" || true)
done
for file in "${plugin_files[@]}"; do
  args+=(-F "files=@$file")
done
```

Use the array form rather than `for file in $PLUGIN_FILES`; unquoted expansion
splits on spaces and reinterprets shell metacharacters.

The GitHub release tag must be a higher semantic version than the published
plugin version. `v0.8.0` was the first successful repository release; `v0.8.1`
enables OFD by default. Later releases could be `v0.8.2` or `v0.9.0`. The
`--version` override updates both the file header and `PLUGIN_VERSION` so the
uploaded code matches the release tag.

## Credentials

- Never commit `.env`, `.env.*` or `.secrets/`. They are covered by
  `.gitignore`; `.env.example` is the only committed placeholder.
- A personal access token that was stored in a plaintext `.env` — especially an
  admin-scoped token — should be rotated now. Revoke the old token in GitHub
  settings before creating a new one.
- Prefer `gh auth login` for local GitHub operations. If a token is required,
  use a fine-grained token scoped to this repository with only the permissions
  needed for releases.
- The release workflow uses GitHub OIDC and stores no repository secret. The
  Orca Cloud account session is a separate, git-ignored file.
- Treat `plugin/dev/orca_hub.py login` like a password login: the refresh token
  rotates on every use and is written atomically with mode `0600`.

## Local development build

`plugin/hub/filament_match_any.py` is for Orca Cloud. The local development
install remains:

```sh
./plugin/install.sh
```

which installs `plugin/dist/filament_match.py` to
`orca_plugins/filament_match/`. Do not enable both the local development copy
and the Orca Cloud subscription at the same time; they each have their own
plugin storage and would both auto-load.
