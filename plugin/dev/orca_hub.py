#!/usr/bin/env python3
"""Read and update the Filament Match listing on Orca Cloud.

Orca Cloud has no personal-access-token UI and its GitHub publishing token can
only upload releases. Listing metadata is protected by the normal Orca Cloud
account session, so this tool performs the same PKCE login as OrcaSlicer:

    python3 plugin/dev/orca_hub.py login
    python3 plugin/dev/orca_hub.py show
    python3 plugin/dev/orca_hub.py update --description-file plugin/HUB_DESCRIPTION.md

The refresh token is stored in ``.secrets/orca_cloud.json`` (ignored by git).
The token rotates on every refresh, so let this tool own that file. Using the
same account in OrcaSlicer is fine: OrcaSlicer keeps its own separate session.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

API_URL = os.environ.get("ORCA_API_URL", "https://api.orcaslicer.com").rstrip("/")
AUTH_URL = os.environ.get("ORCA_AUTH_URL", "https://auth.orcaslicer.com").rstrip("/")
PUBLIC_KEY = os.environ.get(
    "ORCA_PUB_KEY",
    "sb_publishable_lvVe_whOi80SU9BPSxM1kA_tbt9AbR_",
)
DEFAULT_PLUGIN_ID = os.environ.get(
    "ORCA_PLUGIN_ID",
    "3031b85e-07ce-4925-90fa-ba93ccde1c53",
)
SESSION_PATH = Path(
    os.environ.get(
        "ORCA_HUB_SESSION",
        Path(__file__).resolve().parents[2] / ".secrets" / "orca_cloud.json",
    )
)
USER_AGENT = "FilamentMatchHubTool/1.0"


class HubError(RuntimeError):
    pass


def _request(url, *, method="GET", token="", json_body=None, form_body=None,
             api_key=False):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if api_key:
        headers["apikey"] = PUBLIC_KEY
    if token:
        headers["Authorization"] = "Bearer " + token
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            payload = {"error": raw.decode("utf-8", "replace")}
        raise HubError("%s %s: HTTP %s %s" % (
            method, url, error.code, json.dumps(payload))) from error
    except urllib.error.URLError as error:
        raise HubError("%s %s: %s" % (method, url, error.reason)) from error


def _save_session(session):
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        SESSION_PATH.parent.chmod(0o700)
    except OSError:
        pass
    temp = SESSION_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(SESSION_PATH)


def _load_session():
    if not SESSION_PATH.exists():
        raise HubError(
            "No Orca Cloud session at %s. Run: python3 plugin/dev/orca_hub.py login"
            % SESSION_PATH)
    try:
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HubError("Cannot read %s: %s" % (SESSION_PATH, error)) from error


def _token_request(body):
    status, payload = _request(
        AUTH_URL + "/auth/v1/token?" + urllib.parse.urlencode(
            {"grant_type": "pkce" if "auth_code" in body else "refresh_token"}),
        method="POST",
        json_body=body,
        api_key=True,
    )
    if status >= 400 or not payload.get("access_token"):
        raise HubError("Orca Cloud did not return a session: %s" % payload)
    return payload


def _store_token_response(payload):
    session = _load_session() if SESSION_PATH.exists() else {}
    session.update({
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", session.get("refresh_token", "")),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 3600),
    })
    user = payload.get("user") or {}
    if user:
        session["user_id"] = user.get("id", session.get("user_id", ""))
        session["email"] = user.get("email", session.get("email", ""))
    _save_session(session)
    return session


def _access_token():
    session = _load_session()
    token = session.get("access_token", "")
    expires_at = int(session.get("expires_at") or 0)
    if token and expires_at - 60 > time.time():
        return token
    refresh_token = session.get("refresh_token", "")
    if not refresh_token:
        raise HubError("Session has no refresh token. Run login again.")
    payload = _token_request({"refresh_token": refresh_token})
    session = _store_token_response(payload)
    return session["access_token"]


def _api(method, path, json_body=None):
    token = _access_token()
    return _request(API_URL + path, method=method, token=token, json_body=json_body)


def _plugin_summary(plugin):
    return {
        "id": plugin.get("id"),
        "name": plugin.get("name"),
        "version": plugin.get("version"),
        "public": plugin.get("public"),
        "revision": plugin.get("revision"),
        "tags": plugin.get("tags") or [],
        "compatible_orcaslicer_version": plugin.get("compatible_orcaslicer_version"),
        "subscribers": plugin.get("subscriber_count"),
        "updated_time": plugin.get("updated_time"),
    }


def command_login(args):
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    port = args.port
    redirect = "http://localhost:%d/callback" % port
    redirect_with_state = redirect + "?" + urllib.parse.urlencode({"orca_state": state})
    authorize = AUTH_URL + "/auth/v1/authorize?" + urllib.parse.urlencode({
        "provider": args.provider,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_to": redirect_with_state,
    })
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            result.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "code" in result:
                self.wfile.write(
                    b"<h2>Orca Cloud sign-in received.</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>")
            else:
                self.wfile.write(
                    b"<h2>Orca Cloud sign-in failed.</h2>"
                    b"<p>Return to the terminal for details.</p>")

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as error:
        raise HubError("Cannot listen on port %d: %s" % (port, error)) from error
    print("Opening Orca Cloud sign-in in your browser.")
    print("If it does not open, visit:\n\n%s\n" % authorize)
    if not args.no_browser:
        webbrowser.open(authorize)
    print("Waiting for the sign-in callback on %s ..." % redirect)
    server.timeout = 1
    deadline = time.time() + args.timeout
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if not result:
        raise HubError("Timed out after %d seconds waiting for sign-in." % args.timeout)

    if "error" in result:
        raise HubError("Sign-in failed: %s %s" % (
            result.get("error"), result.get("error_description", "")))
    code = result.get("code", "")
    returned_state = result.get("orca_state") or result.get("state") or ""
    if not code:
        raise HubError("The callback did not include an authorization code.")
    if returned_state != state:
        raise HubError("The callback state did not match. Try logging in again.")

    payload = _token_request({"auth_code": code, "code_verifier": verifier})
    session = _store_token_response(payload)
    print("Connected as %s (%s)." % (
        session.get("email") or "Orca Cloud user",
        session.get("user_id") or "unknown id"))
    print("Session saved to %s" % SESSION_PATH)
    print("Try: python3 plugin/dev/orca_hub.py show")


def command_refresh(args):
    session = _load_session()
    refresh_token = session.get("refresh_token", "")
    if not refresh_token:
        raise HubError("Session has no refresh token. Run login again.")
    payload = _token_request({"refresh_token": refresh_token})
    session = _store_token_response(payload)
    print("Refreshed session for %s" % (session.get("email") or session.get("user_id")))


def command_list(args):
    _, payload = _api("GET", "/api/v1/plugins/mine")
    plugins = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(plugins, list):
        raise HubError("Unexpected response: %s" % payload)
    for plugin in plugins:
        summary = _plugin_summary(plugin)
        print("%(id)s  %(version)s  public=%(public)s  %(name)s" % summary)


def command_show(args):
    _, payload = _api("GET", "/api/v1/plugins/" + urllib.parse.quote(args.plugin_id))
    plugin = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(plugin, dict):
        raise HubError("Unexpected response: %s" % payload)
    if args.json:
        print(json.dumps(plugin, indent=2, ensure_ascii=False))
        return
    summary = _plugin_summary(plugin)
    for key, value in summary.items():
        print("%s: %s" % (key, value))
    print("description: %d characters" % len(plugin.get("description") or ""))


def command_update(args):
    _, current = _api("GET", "/api/v1/plugins/" + urllib.parse.quote(args.plugin_id))
    plugin = current.get("data", current) if isinstance(current, dict) else current
    if not isinstance(plugin, dict):
        raise HubError("Unexpected response: %s" % current)
    payload = {
        "description": plugin.get("description") or "",
        "name": plugin.get("name") or "Filament Match",
        "version": plugin.get("version") or "",
        "public": plugin.get("public"),
        "tags": plugin.get("tags") or [],
        "compatible_orcaslicer_version": plugin.get("compatible_orcaslicer_version"),
    }
    if args.description_file:
        payload["description"] = Path(args.description_file).read_text(encoding="utf-8")
    if args.description is not None:
        payload["description"] = args.description
    if args.name:
        payload["name"] = args.name
    if args.version:
        payload["version"] = args.version
    if args.tags is not None:
        payload["tags"] = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    if args.compatible_version:
        payload["compatible_orcaslicer_version"] = args.compatible_version
    if args.public is not None:
        payload["public"] = args.public
    if args.revision is not None:
        payload["expected_revision"] = args.revision
    elif isinstance(plugin.get("revision"), int):
        payload["expected_revision"] = plugin["revision"]

    if not payload["description"].strip():
        raise HubError("The plugin description cannot be empty.")
    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    _, response = _api(
        "PATCH",
        "/api/v1/plugins/" + urllib.parse.quote(args.plugin_id),
        json_body=payload,
    )
    updated = response.get("data", response) if isinstance(response, dict) else response
    print(json.dumps(_plugin_summary(updated), indent=2))


def main(argv=None):
    global SESSION_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="session file (default: %s)" % SESSION_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="sign in with a browser and save a session")
    login.add_argument("--port", type=int, default=41172)
    login.add_argument("--provider", default="google", choices=("google", "github"))
    login.add_argument("--no-browser", action="store_true")
    login.add_argument("--timeout", type=int, default=600,
                       help="seconds to wait for the browser callback")
    login.set_defaults(func=command_login)

    refresh = sub.add_parser("refresh", help="refresh the saved session")
    refresh.set_defaults(func=command_refresh)

    listing = sub.add_parser("list", help="list plugins owned by the account")
    listing.set_defaults(func=command_list)

    show = sub.add_parser("show", help="show one plugin listing")
    show.add_argument("plugin_id", nargs="?", default=DEFAULT_PLUGIN_ID)
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=command_show)

    update = sub.add_parser("update", help="update plugin listing metadata")
    update.add_argument("plugin_id", nargs="?", default=DEFAULT_PLUGIN_ID)
    update.add_argument("--description-file")
    update.add_argument("--description")
    update.add_argument("--name")
    update.add_argument("--version")
    update.add_argument("--tags", help="comma-separated tags")
    update.add_argument("--compatible-version")
    visibility = update.add_mutually_exclusive_group()
    visibility.add_argument("--public", dest="public", action="store_true")
    visibility.add_argument("--private", dest="public", action="store_false")
    update.set_defaults(public=None)
    update.add_argument("--revision", type=int,
                        help="expected revision; defaults to the current revision")
    update.add_argument("--dry-run", action="store_true")
    update.set_defaults(func=command_update)

    args = parser.parse_args(argv)
    if args.session:
        SESSION_PATH = Path(args.session)
    try:
        args.func(args)
    except HubError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
