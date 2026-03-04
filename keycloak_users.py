#!/usr/bin/env python3
"""
keycloak_users.py — Backup and restore Keycloak users with attributes and IDP links.

Modes (auto-detected from arguments):
  Backup  : no --in              → read users from Keycloak → write to JSON file
  Restore : --in FILE            → read from JSON file → write to Keycloak
  Dry-run : --in FILE --dry-run  → validate + report only, no writes
"""

import argparse
import datetime
import json
import os
import re
import sys
from typing import Any, NoReturn, Optional

try:
    import requests
    import urllib3
except ImportError:
    sys.exit("requests is required:  pip install requests")


# ---------------------------------------------------------------------------
# Colour support
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
DIM    = "\033[2m"

_USE_COLOR = False


def init_color(mode: str) -> None:
    global _USE_COLOR
    if mode == "always":
        _USE_COLOR = True
    elif mode == "never":
        _USE_COLOR = False
    else:  # auto
        _USE_COLOR = sys.stdout.isatty()


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes if colour is enabled."""
    if not _USE_COLOR or not codes:
        return text
    return "".join(codes) + text + RESET


# Simple regex-based JSON colorizer (keys cyan, string values green,
# numbers/booleans/null yellow).
_KEY_RE    = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:)')
_STR_RE    = re.compile(r'(:\s*)("(?:[^"\\]|\\.)*")')
_SCALAR_RE = re.compile(r'(:\s*)(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)\b')


def _colorize_json_line(line: str) -> str:
    line = _KEY_RE.sub(lambda m: c(m.group(1), CYAN) + m.group(2), line)
    line = _STR_RE.sub(lambda m: m.group(1) + c(m.group(2), GREEN), line)
    line = _SCALAR_RE.sub(lambda m: m.group(1) + c(m.group(2), YELLOW), line)
    return line


def print_json(data: object) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if _USE_COLOR:
        for line in text.splitlines():
            print(_colorize_json_line(line))
    else:
        print(text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def str2bool(v: str) -> bool:
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "1"):
        return True
    if v.lower() in ("no", "false", "f", "0"):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got: {v!r}")


def ts() -> str:
    """Return a YYYYMMDDHHMM timestamp string."""
    return datetime.datetime.now().strftime("%Y%m%d%H%M")


def die(msg: str) -> NoReturn:
    print(c(f"ERROR: {msg}", RED, BOLD), file=sys.stderr)
    sys.exit(1)


def header(text: str) -> None:
    print(c(f"{'='*3} {text} {'='*3}", BOLD))


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(c(f"  Wrote: {path}", CYAN))


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        die(f"Input file not found: {path}")
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON in {path}: {exc}")


# ---------------------------------------------------------------------------
# Keycloak API — Auth
# ---------------------------------------------------------------------------

def get_token(base_url: str, realm: str, client_id: str, client_secret: str,
              timeout: int, verify: bool) -> str:
    url = f"{base_url}/realms/{realm}/protocol/openid-connect/token"
    resp = requests.post(
        url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        timeout=timeout,
        verify=verify,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Keycloak API — Users (backup)
# ---------------------------------------------------------------------------

def fetch_all_users(base_url: str, realm: str, token: str,
                    timeout: int, verify: bool) -> list:
    """Fetch all users from Keycloak using pagination."""
    url = f"{base_url}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}"}
    users = []
    page_size = 100
    first = 0
    while True:
        resp = requests.get(
            url,
            params={"first": first, "max": page_size},
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        users.extend(page)
        if len(page) < page_size:
            break
        first += page_size
    return users


def fetch_federated_identities(base_url: str, realm: str, token: str,
                                user_id: str, timeout: int, verify: bool) -> list:
    """Fetch federated identity links for a user."""
    url = f"{base_url}/admin/realms/{realm}/users/{user_id}/federated-identity"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    resp.raise_for_status()
    return resp.json()


def fetch_group_member_ids(base_url: str, realm: str, token: str,
                           group_path: str, timeout: int, verify: bool) -> set:
    """Return set of user IDs who are members of the group at the given path."""
    url = f"{base_url}/admin/realms/{realm}/group-by-path/{group_path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    if resp.status_code == 404:
        die(f"Group not found: {group_path}")
    resp.raise_for_status()
    group_id = resp.json()["id"]

    members_url = f"{base_url}/admin/realms/{realm}/groups/{group_id}/members"
    members = []
    page_size = 100
    first = 0
    while True:
        resp = requests.get(
            members_url,
            params={"first": first, "max": page_size},
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        members.extend(page)
        if len(page) < page_size:
            break
        first += page_size
    return {m["id"] for m in members}


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def parse_attr_filter(spec: str) -> tuple:
    """Parse 'KEY' or 'KEY=VALUE' into (key, value_or_None).

    'KEY='  → (key, '')   — empty value
    'KEY'   → (key, None) — any value (key exists)
    'KEY=V' → (key, 'V')  — specific value
    """
    if "=" in spec:
        key, _, val = spec.partition("=")
        return key.strip(), val
    return spec.strip(), None


def user_matches_attr(user: dict, key: str, value: Optional[str]) -> bool:
    """Return True if user has the attribute matching key/value spec."""
    attrs = user.get("attributes", {}) or {}
    if key not in attrs:
        return False
    if value is None:
        return True  # any value
    attr_values = attrs[key]
    if value == "":
        return any(v == "" for v in attr_values)
    return value in attr_values


def user_matches_idp(user_idps: list, idp_spec: str) -> bool:
    """Return True if user matches the IDP spec ('none' or an IDP alias)."""
    if idp_spec.lower() == "none":
        return len(user_idps) == 0
    return any(fi.get("identityProvider") == idp_spec for fi in user_idps)


def apply_user_filters(users: list, idp_map: dict, group_id_sets: dict,
                       include_attrs: list, exclude_attrs: list,
                       include_idps: list, exclude_idps: list,
                       include_groups: list, exclude_groups: list) -> list:
    """Apply include/exclude filters and return filtered user list.

    Include filters are OR-combined (any match passes).
    Exclude filters are OR-combined (any match fails).
    Excludes are applied after includes.
    """
    result = []
    for user in users:
        uid = user["id"]
        user_idps = idp_map.get(uid, [])

        # --- Include filters (any match passes; no filters = all pass) ---
        has_any_include = bool(include_attrs or include_idps or include_groups)
        passes_include = True
        if has_any_include:
            passes_include = False
            for spec in include_attrs:
                key, val = parse_attr_filter(spec)
                if user_matches_attr(user, key, val):
                    passes_include = True
                    break
            if not passes_include:
                for idp in include_idps:
                    if user_matches_idp(user_idps, idp):
                        passes_include = True
                        break
            if not passes_include:
                for grp in include_groups:
                    if uid in group_id_sets.get(grp, set()):
                        passes_include = True
                        break
        if not passes_include:
            continue

        # --- Exclude filters (any match fails) ---
        excluded = False
        for spec in exclude_attrs:
            key, val = parse_attr_filter(spec)
            if user_matches_attr(user, key, val):
                excluded = True
                break
        if not excluded:
            for idp in exclude_idps:
                if user_matches_idp(user_idps, idp):
                    excluded = True
                    break
        if not excluded:
            for grp in exclude_groups:
                if uid in group_id_sets.get(grp, set()):
                    excluded = True
                    break

        if not excluded:
            result.append(user)

    return result


# ---------------------------------------------------------------------------
# Keycloak API — Users (restore)
# ---------------------------------------------------------------------------

def find_user_by_username(base_url: str, realm: str, token: str,
                          username: str, timeout: int, verify: bool) -> Optional[dict]:
    """Return user dict if username exists in realm, else None."""
    url = f"{base_url}/admin/realms/{realm}/users"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        url,
        params={"username": username, "exact": "true"},
        headers=headers,
        timeout=timeout,
        verify=verify,
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def create_user(base_url: str, realm: str, token: str,
                user_data: dict, timeout: int, verify: bool) -> None:
    url = f"{base_url}/admin/realms/{realm}/users"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, json=user_data, headers=headers,
                         timeout=timeout, verify=verify)
    resp.raise_for_status()


def update_user(base_url: str, realm: str, token: str,
                user_id: str, user_data: dict, timeout: int, verify: bool) -> None:
    url = f"{base_url}/admin/realms/{realm}/users/{user_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    resp = requests.put(url, json=user_data, headers=headers,
                        timeout=timeout, verify=verify)
    resp.raise_for_status()


def prepare_user_for_restore(user: dict, attr_mode: str,
                              schema_keys: Optional[list]) -> dict:
    """Build the user representation for restore, applying attribute mode."""
    # Exclude read-only / KC-managed fields
    exclude_keys = {
        "id", "federatedIdentities", "access", "notBefore",
        "totp", "disableableCredentialTypes", "requiredActions",
        "createdTimestamp",
    }
    result = {k: v for k, v in user.items() if k not in exclude_keys}

    if attr_mode == "none":
        result["attributes"] = {}
    elif attr_mode == "schema" and schema_keys is not None:
        attrs = user.get("attributes", {}) or {}
        result["attributes"] = {k: v for k, v in attrs.items() if k in schema_keys}
    # else "all" — keep as-is

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    env = os.environ
    p = argparse.ArgumentParser(
        description="Backup and restore Keycloak users with attributes and IDP links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (auto-detected):
  Backup  : no --in              Read all users from Keycloak; write to JSON file.
  Restore : --in FILE            Read users from JSON file; create/update in Keycloak.
  Dry-run : --in FILE --dry-run  Validate and report; no writes to Keycloak.

Examples:
  # Backup all users to a timestamped file
  %(prog)s

  # Backup only users linked to the google IDP
  %(prog)s --include-idp google

  # Backup users in /staff group but not in /legacy group
  %(prog)s --include-group /staff --exclude-group /legacy

  # Backup users who have the 'department' attribute set to 'eng'
  %(prog)s --include-attr department=eng

  # Backup with specific attributes only
  %(prog)s --attrs uid,email,department

  # Backup using an attribute schema file
  %(prog)s --attr-schema schema.json

  # Restore from backup (skip existing users)
  %(prog)s --in users_myrealm_202401010900.json

  # Restore and overwrite existing users
  %(prog)s --in users_myrealm_202401010900.json --force

  # Dry-run restore (no changes made)
  %(prog)s --in users_myrealm_202401010900.json --dry-run

  # Restore with only specific attributes
  %(prog)s --in users_myrealm_202401010900.json --attr-mode schema --attr-schema schema.json
""",
    )

    # Connection / auth
    p.add_argument("--base-url",      default=env.get("KC_BASE_URL"),
                   help="Keycloak base URL  (env: KC_BASE_URL)")
    p.add_argument("--realm",         default=env.get("KC_REALM"),
                   help="Realm name         (env: KC_REALM)")
    p.add_argument("--client-id",     default=env.get("KC_CLIENT_ID"),
                   help="Client ID          (env: KC_CLIENT_ID)")
    p.add_argument("--client-secret", default=env.get("KC_CLIENT_SECRET"),
                   help="Client secret      (env: KC_CLIENT_SECRET)")
    p.add_argument("--timeout", type=int, default=30,
                   help="HTTP timeout in seconds (default: 30)")
    p.add_argument("--insecure", action="store_true",
                   help="Disable TLS certificate verification")

    # Mode control
    p.add_argument("--in", dest="infile", default=None,
                   help="Input JSON file (enables restore mode)")
    p.add_argument("--dry-run", type=str2bool, nargs="?", const=True, default=False,
                   help="Simulate only — no writes. Accepts true/false.")

    # Output (backup mode)
    p.add_argument("--out", dest="outfile", default=None,
                   help="Output file path (default: users_{realm}_{timestamp}.json)")
    p.add_argument("--no-out", action="store_true",
                   help="Skip file output (print to stdout only)")

    # Restore options
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing users during restore (default: skip)")
    p.add_argument("--attr-mode", choices=["all", "schema", "none"], default="all",
                   help="Attribute restoration mode (default: all)")

    # Attribute schema (backup and restore)
    p.add_argument("--attr-schema", dest="attr_schema", default=None,
                   help='JSON file containing list of attribute keys: ["key1", "key2"]')
    p.add_argument("--attrs", default=None,
                   help="Comma-separated attribute keys to include (e.g. uid,email,dept)")

    # User filters (backup mode)
    p.add_argument("--include-attr", dest="include_attr", action="append",
                   metavar="KEY[=VALUE]",
                   help="Include users with this attribute key (or key=value). Repeatable.")
    p.add_argument("--exclude-attr", dest="exclude_attr", action="append",
                   metavar="KEY[=VALUE]",
                   help="Exclude users with this attribute key (or key=value). Repeatable.")
    p.add_argument("--include-idp", dest="include_idp", action="append",
                   metavar="IDP",
                   help="Include users linked to this IDP alias (use 'none' for no IDP). Repeatable.")
    p.add_argument("--exclude-idp", dest="exclude_idp", action="append",
                   metavar="IDP",
                   help="Exclude users linked to this IDP alias. Repeatable.")
    p.add_argument("--include-group", dest="include_group", action="append",
                   metavar="GROUP_PATH",
                   help="Include users who are members of this group path. Repeatable.")
    p.add_argument("--exclude-group", dest="exclude_group", action="append",
                   metavar="GROUP_PATH",
                   help="Exclude users who are members of this group path. Repeatable.")

    # Display
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                   help="Colorize output (default: auto)")

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    init_color(args.color)

    # Validate required connection args
    missing = [
        flag for flag, val in [
            ("--base-url",      args.base_url),
            ("--realm",         args.realm),
            ("--client-id",     args.client_id),
            ("--client-secret", args.client_secret),
        ]
        if not val
    ]
    if missing:
        die("Missing required arguments (or env vars): " + "  ".join(missing))

    args.base_url = args.base_url.rstrip("/")

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    verify = not args.insecure

    # Determine mode
    mode = "backup" if not args.infile else ("dryrun" if args.dry_run else "restore")

    print(c(f"Mode: {mode.upper()}", BOLD) + "  |  " + c(f"Realm: {args.realm}", BOLD))
    print()

    # Load attribute schema if provided
    schema_keys: Optional[list] = None
    if args.attr_schema:
        raw = read_json(args.attr_schema)
        if not isinstance(raw, list):
            die(f"--attr-schema file must contain a JSON array, got: {type(raw).__name__}")
        schema_keys = raw
        print(c(f"  Attribute schema: {len(schema_keys)} key(s): {schema_keys}", DIM))
    elif args.attrs:
        schema_keys = [k.strip() for k in args.attrs.split(",") if k.strip()]
        print(c(f"  Attribute schema: {len(schema_keys)} key(s): {schema_keys}", DIM))

    # Authenticate
    print("Authenticating …")
    try:
        token = get_token(args.base_url, args.realm, args.client_id,
                          args.client_secret, args.timeout, verify)
    except requests.HTTPError as exc:
        die(f"Authentication failed: {exc}")
    except requests.ConnectionError as exc:
        die(f"Connection error: {exc}")
    print(c("  OK", GREEN))
    print()

    # =========================================================================
    # BACKUP MODE
    # =========================================================================
    if mode == "backup":
        print("Fetching users …")
        try:
            users = fetch_all_users(args.base_url, args.realm, token,
                                    args.timeout, verify)
        except requests.HTTPError as exc:
            die(f"Failed to fetch users: {exc}")
        print(c(f"  Found {len(users)} user(s)", GREEN))

        # Fetch federated identities for all users
        print("Fetching federated identities …")
        idp_map: dict = {}
        for i, user in enumerate(users, 1):
            try:
                idp_map[user["id"]] = fetch_federated_identities(
                    args.base_url, args.realm, token, user["id"], args.timeout, verify
                )
            except requests.HTTPError as exc:
                print(c(f"  Warning: could not fetch IDP for {user.get('username')!r}: {exc}",
                        YELLOW))
                idp_map[user["id"]] = []
            if i % 50 == 0:
                print(c(f"  … {i}/{len(users)}", DIM))
        print(c(f"  Done", GREEN))

        # Pre-fetch group membership sets for group filters
        group_id_sets: dict = {}
        all_filter_groups = (args.include_group or []) + (args.exclude_group or [])
        if all_filter_groups:
            print("Fetching group membership sets for filters …")
            for grp_path in all_filter_groups:
                try:
                    group_id_sets[grp_path] = fetch_group_member_ids(
                        args.base_url, args.realm, token, grp_path, args.timeout, verify
                    )
                    print(c(f"  {grp_path}: {len(group_id_sets[grp_path])} member(s)", DIM))
                except requests.HTTPError as exc:
                    die(f"Failed to fetch group members for {grp_path!r}: {exc}")

        # Apply filters
        filtered = apply_user_filters(
            users, idp_map, group_id_sets,
            args.include_attr or [], args.exclude_attr or [],
            args.include_idp or [], args.exclude_idp or [],
            args.include_group or [], args.exclude_group or [],
        )
        if len(filtered) != len(users):
            print(c(f"  After filtering: {len(filtered)} user(s) "
                    f"(from {len(users)})", YELLOW))

        # Build output records
        output_users = []
        for user in filtered:
            uid = user["id"]
            u = dict(user)
            u["federatedIdentities"] = idp_map.get(uid, [])
            if schema_keys is not None:
                attrs = u.get("attributes", {}) or {}
                u["attributes"] = {k: v for k, v in attrs.items() if k in schema_keys}
            output_users.append(u)

        output = {
            "meta": {
                "timestamp":       ts(),
                "source_base_url": args.base_url,
                "source_realm":    args.realm,
                "total_users":     len(output_users),
            },
            "users": output_users,
        }

        print()
        header("Backup Summary")
        print(f"  Total users: {len(output_users)}")
        print()

        if args.no_out:
            print_json(output)
        else:
            out = args.outfile or f"users_{args.realm}_{ts()}.json"
            write_json(out, output)
        return

    # =========================================================================
    # RESTORE / DRY-RUN MODE
    # =========================================================================
    print(f"Reading input file: {args.infile}")
    backup_data = read_json(args.infile)

    if not isinstance(backup_data, dict) or "users" not in backup_data:
        die(
            f"Input file {args.infile!r} does not look like a users backup "
            f"(expected top-level 'users' array)."
        )

    users_to_restore = backup_data["users"]
    meta = backup_data.get("meta", {})
    print(c(f"  OK — {len(users_to_restore)} user(s) in backup", GREEN))
    if meta:
        print(c(
            f"  Source: {meta.get('source_realm')} @ {meta.get('source_base_url')}"
            f"  Timestamp: {meta.get('timestamp')}",
            DIM,
        ))
    print()

    attr_mode = args.attr_mode
    if attr_mode == "schema" and schema_keys is None:
        die("--attr-mode schema requires --attr-schema or --attrs")

    if args.dry_run:
        print(c("DRY-RUN: No changes will be made to Keycloak.", YELLOW, BOLD))
        print()

    # Counters
    n_created  = 0
    n_skipped  = 0
    n_updated  = 0
    n_failed   = 0
    idp_users: list = []

    for i, user in enumerate(users_to_restore, 1):
        username = user.get("username", f"(unknown #{i})")
        fed_ids  = user.get("federatedIdentities", [])
        if fed_ids:
            idp_users.append(username)

        print(f"[{i}/{len(users_to_restore)}] {c(username, CYAN)}", end="  ")

        # Check existence
        try:
            existing = find_user_by_username(
                args.base_url, args.realm, token, username, args.timeout, verify
            )
        except requests.HTTPError as exc:
            print(c(f"FAILED (lookup): {exc}", RED))
            n_failed += 1
            continue

        user_payload = prepare_user_for_restore(user, attr_mode, schema_keys)

        if existing:
            if not args.force:
                print(c("SKIPPED (exists)", YELLOW))
                n_skipped += 1
                continue
            # Force update
            if args.dry_run:
                print(c("would UPDATE (dry-run)", YELLOW))
                n_updated += 1
                continue
            try:
                update_user(args.base_url, args.realm, token,
                            existing["id"], user_payload, args.timeout, verify)
                print(c("UPDATED", GREEN))
                n_updated += 1
            except requests.HTTPError as exc:
                body = exc.response.text if exc.response is not None else "n/a"
                print(c(f"FAILED: {exc} — {body}", RED))
                n_failed += 1
        else:
            # Create
            if args.dry_run:
                print(c("would CREATE (dry-run)", YELLOW))
                n_created += 1
                continue
            try:
                create_user(args.base_url, args.realm, token,
                            user_payload, args.timeout, verify)
                print(c("CREATED", GREEN))
                n_created += 1
            except requests.HTTPError as exc:
                body = exc.response.text if exc.response is not None else "n/a"
                print(c(f"FAILED: {exc} — {body}", RED))
                n_failed += 1

    print()
    header("Restore Summary")
    if mode == "dryrun":
        print(f"  Would create : {n_created}")
        print(f"  Would update : {n_updated}  (--force)")
        print(f"  Skipped      : {n_skipped}  (already exist)")
        print(f"  Failed       : {n_failed}  (lookup errors)")
    else:
        print(f"  Created : {n_created}")
        print(f"  Updated : {n_updated}  (--force)")
        print(f"  Skipped : {n_skipped}  (already exist — use --force to overwrite)")
        print(f"  Failed  : {n_failed}")

    if idp_users:
        print()
        print(c(
            f"  IDP links not restored: {len(idp_users)} user(s) had federatedIdentities "
            f"(manual action required)",
            YELLOW,
        ))
        for u in idp_users:
            print(c(f"    - {u}", DIM))

    print()
    if mode != "dryrun" and n_failed == 0:
        print(c("All done.", GREEN, BOLD))


if __name__ == "__main__":
    main()
