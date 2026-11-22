# Orca Cloud listing management

GitHub releases publish new versions through OIDC, but they cannot change the
listing metadata. The Orca Cloud metadata API requires the normal account
session, and GitHub's publishing token is rejected with `401 invalid_token` on
`/api/v1/plugins/*`.

`plugin/dev/orca_hub.py` performs the same PKCE sign-in as OrcaSlicer and then
reads or updates the plugin listing.

## One-time sign-in

```sh
python3 plugin/dev/orca_hub.py login
```

The command starts a temporary callback listener on `localhost:41172`, opens
the Orca Cloud sign-in page, and saves the resulting session to:

```text
.secrets/orca_cloud.json
```

The file is ignored by git and is written with mode `600`. The Orca Cloud
session is separate from OrcaSlicer's own session, so signing in here does not
log OrcaSlicer out.

If the browser does not open, copy the printed URL into a browser. Use
`--provider github` to sign in with GitHub instead of Google.

## Commands

```sh
# List the plugins owned by the account
python3 plugin/dev/orca_hub.py list

# Show the Filament Match listing
python3 plugin/dev/orca_hub.py show

# Show the raw listing JSON
python3 plugin/dev/orca_hub.py show --json

# Update the description from a file, keeping the other metadata
python3 plugin/dev/orca_hub.py update --description-file plugin/HUB_DESCRIPTION.md

# Update tags and visibility
python3 plugin/dev/orca_hub.py update --tags filament,spoolman,preset --public

# Preview the PATCH payload without sending it
python3 plugin/dev/orca_hub.py update --description-file plugin/HUB_DESCRIPTION.md --dry-run
```

The default plugin ID is the Filament Match listing. Pass another ID as the
first argument, or set `ORCA_PLUGIN_ID`, when working with a different plugin.

## API used

The Orca Cloud web app uses these endpoints:

```text
GET   /api/v1/plugins/mine
GET   /api/v1/plugins/{id}
PATCH /api/v1/plugins/{id}
```

The update payload can contain:

```text
description, name, version, public, tags,
compatible_orcaslicer_version, expected_revision
```

`expected_revision` prevents overwriting a change made in the web UI between
the GET and PATCH. The tool reads the current revision and sends it
automatically.

The API is undocumented and may change. Authentication uses the public
Supabase client key that ships with OrcaSlicer; no client secret or password is
stored by this tool. The listing description is limited to 10,000 characters.

## Token rotation

Orca Cloud refresh tokens are single-use. Every refresh returns a new refresh
token, and the tool writes it back to `.secrets/orca_cloud.json` immediately.
Do not copy that file into another tool or run two copies of the tool at the
same time. If a token is spent or revoked, run `login` again.

## Security

- Never commit `.secrets/`; it is listed in `.gitignore`.
- The helper only talks to `api.orcaslicer.com` and `auth.orcaslicer.com`.
- It does not read OrcaSlicer's keychain item or encrypted token file.
- It can only manage plugins owned by the signed-in account.
