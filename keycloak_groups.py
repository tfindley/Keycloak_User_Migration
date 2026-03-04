#!/usr/bin/env python3
"""
keycloak_groups.py — Backup and restore Keycloak groups with attributes, members,
and nested group hierarchy.

Modes (auto-detected from arguments):
  Backup  : no --in              → read groups from Keycloak → write to JSON file
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
# Keycloak API — Groups (backup)
# ---------------------------------------------------------------------------

def fetch_top_level_groups(base_url: str, realm: str, token: str,
                            timeout: int, verify: bool) -> list:
    """Fetch all top-level groups (brief representation) using pagination."""
    url = f"{base_url}/admin/realms/{realm}/groups"
    headers = {"Authorization": f"Bearer {token}"}
    groups = []
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
        groups.extend(page)
        if len(page) < page_size:
            break
        first += page_size
    return groups


def fetch_group_by_id(base_url: str, realm: str, token: str,
                      group_id: str, timeout: int, verify: bool) -> dict:
    """Fetch full group representation by Keycloak UUID."""
    url = f"{base_url}/admin/realms/{realm}/groups/{group_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    resp.raise_for_status()
    return resp.json()


def fetch_group_members_paged(base_url: str, realm: str, token: str,
                               group_id: str, timeout: int, verify: bool) -> list:
    """Fetch all members of a group using pagination."""
    url = f"{base_url}/admin/realms/{realm}/groups/{group_id}/members"
    headers = {"Authorization": f"Bearer {token}"}
    members = []
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
        members.extend(page)
        if len(page) < page_size:
            break
        first += page_size
    return members


def fetch_group_full(base_url: str, realm: str, token: str,
                     group_id: str, timeout: int, verify: bool,
                     schema_keys: Optional[list] = None) -> dict:
    """Fetch a group's full details including members; recurse into subGroups."""
    group = fetch_group_by_id(base_url, realm, token, group_id, timeout, verify)

    # Fetch members
    try:
        raw_members = fetch_group_members_paged(
            base_url, realm, token, group_id, timeout, verify
        )
    except requests.HTTPError as exc:
        print(c(f"  Warning: could not fetch members for {group.get('name')!r}: {exc}", YELLOW))
        raw_members = []

    # Keep only id and username in member records
    group["members"] = [
        {"id": m["id"], "username": m.get("username", "")} for m in raw_members
    ]

    # Apply attribute schema
    if schema_keys is not None:
        attrs = group.get("attributes", {}) or {}
        group["attributes"] = {k: v for k, v in attrs.items() if k in schema_keys}

    # Recurse into subGroups
    sub_groups = group.get("subGroups", [])
    group["subGroups"] = [
        fetch_group_full(base_url, realm, token, sg["id"], timeout, verify, schema_keys)
        for sg in sub_groups
    ]

    return group


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


def group_matches_attr(group: dict, key: str, value: Optional[str]) -> bool:
    """Return True if group has the attribute matching key/value spec."""
    attrs = group.get("attributes", {}) or {}
    if key not in attrs:
        return False
    if value is None:
        return True  # any value
    attr_values = attrs[key]
    if value == "":
        return any(v == "" for v in attr_values)
    return value in attr_values


def apply_group_filters(group: dict, args: argparse.Namespace) -> Optional[dict]:
    """Apply include/exclude filters to a single group node.

    Returns None if the group is filtered out (and has no matching descendants).
    SubGroups are filtered independently — a filtered-out parent still passes
    through its matching children (wrapped in a minimal parent shell).
    """
    include_attrs = args.include_attr or []
    exclude_attrs = args.exclude_attr or []
    include_names = args.include_name or []
    exclude_names = args.exclude_name or []
    min_members   = args.min_members
    max_members   = args.max_members

    member_count = len(group.get("members", []))
    name = group.get("name", "")

    # --- Member count filters (independent AND conditions) ---
    if min_members is not None and member_count < min_members:
        filtered_subs = _filter_subgroups(group.get("subGroups", []), args)
        if filtered_subs:
            result = dict(group)
            result["subGroups"] = filtered_subs
            return result
        return None
    if max_members is not None and member_count > max_members:
        filtered_subs = _filter_subgroups(group.get("subGroups", []), args)
        if filtered_subs:
            result = dict(group)
            result["subGroups"] = filtered_subs
            return result
        return None

    # --- Include filters (any match passes; no filters = all pass) ---
    has_any_include = bool(include_attrs or include_names)
    passes_include = True
    if has_any_include:
        passes_include = False
        for spec in include_attrs:
            key, val = parse_attr_filter(spec)
            if group_matches_attr(group, key, val):
                passes_include = True
                break
        if not passes_include:
            for n in include_names:
                if name == n:
                    passes_include = True
                    break

    if not passes_include:
        # This node doesn't match, but its children may — recurse
        filtered_subs = _filter_subgroups(group.get("subGroups", []), args)
        if filtered_subs:
            result = dict(group)
            result["subGroups"] = filtered_subs
            return result
        return None

    # --- Exclude filters (any match fails) ---
    excluded = False
    for spec in exclude_attrs:
        key, val = parse_attr_filter(spec)
        if group_matches_attr(group, key, val):
            excluded = True
            break
    if not excluded:
        for n in exclude_names:
            if name == n:
                excluded = True
                break

    if excluded:
        filtered_subs = _filter_subgroups(group.get("subGroups", []), args)
        if filtered_subs:
            result = dict(group)
            result["subGroups"] = filtered_subs
            return result
        return None

    # Group passes — recurse into subGroups
    result = dict(group)
    result["subGroups"] = _filter_subgroups(group.get("subGroups", []), args)
    return result


def _filter_subgroups(subgroups: list, args: argparse.Namespace) -> list:
    result = []
    for sg in subgroups:
        filtered = apply_group_filters(sg, args)
        if filtered is not None:
            result.append(filtered)
    return result


def count_groups_in_tree(groups: list) -> int:
    """Recursively count all group nodes in a nested tree."""
    total = 0
    for g in groups:
        total += 1
        total += count_groups_in_tree(g.get("subGroups", []))
    return total


# ---------------------------------------------------------------------------
# Keycloak API — Groups (restore)
# ---------------------------------------------------------------------------

def find_group_by_path(base_url: str, realm: str, token: str,
                       path: str, timeout: int, verify: bool) -> Optional[dict]:
    """Return group dict if a group at the given path exists, else None."""
    url = f"{base_url}/admin/realms/{realm}/group-by-path/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _extract_id_from_location(resp: requests.Response, label: str) -> str:
    """Extract the resource ID from a Location header, or die with a clear message."""
    location = resp.headers.get("Location", "")
    if not location:
        die(f"Keycloak did not return a Location header when creating {label} "
            f"(HTTP {resp.status_code})")
    return location.rstrip("/").rsplit("/", 1)[-1]


def create_top_level_group(base_url: str, realm: str, token: str,
                            group_data: dict, timeout: int, verify: bool) -> str:
    """Create a top-level group and return its new Keycloak ID."""
    url = f"{base_url}/admin/realms/{realm}/groups"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, json=group_data, headers=headers,
                         timeout=timeout, verify=verify)
    resp.raise_for_status()
    return _extract_id_from_location(resp, f"group {group_data.get('name', '?')!r}")


def create_child_group(base_url: str, realm: str, token: str,
                       parent_id: str, group_data: dict,
                       timeout: int, verify: bool) -> str:
    """Create a child group under parent_id and return its new Keycloak ID."""
    url = f"{base_url}/admin/realms/{realm}/groups/{parent_id}/children"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    resp = requests.post(url, json=group_data, headers=headers,
                         timeout=timeout, verify=verify)
    resp.raise_for_status()
    return _extract_id_from_location(resp, f"child group {group_data.get('name', '?')!r}")


def update_group(base_url: str, realm: str, token: str,
                 group_id: str, group_data: dict,
                 timeout: int, verify: bool) -> None:
    url = f"{base_url}/admin/realms/{realm}/groups/{group_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    resp = requests.put(url, json=group_data, headers=headers,
                        timeout=timeout, verify=verify)
    resp.raise_for_status()


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


def add_user_to_group(base_url: str, realm: str, token: str,
                      user_id: str, group_id: str,
                      timeout: int, verify: bool) -> None:
    url = f"{base_url}/admin/realms/{realm}/users/{user_id}/groups/{group_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.put(url, headers=headers, timeout=timeout, verify=verify)
    resp.raise_for_status()


def prepare_group_for_restore(group: dict, attr_mode: str,
                               schema_keys: Optional[list]) -> dict:
    """Build the group payload for a Keycloak create/update, applying attribute mode."""
    # Exclude fields that are not part of a group create/update payload
    exclude_keys = {"id", "subGroups", "members", "access"}
    result = {k: v for k, v in group.items() if k not in exclude_keys}

    if attr_mode == "none":
        result["attributes"] = {}
    elif attr_mode == "schema" and schema_keys is not None:
        attrs = group.get("attributes", {}) or {}
        result["attributes"] = {k: v for k, v in attrs.items() if k in schema_keys}
    # else "all" — keep as-is

    return result


def flatten_group_tree(groups: list) -> list:
    """Flatten nested group tree into a list in DFS pre-order (parents before children).

    Returns list of (path, group_dict) tuples.
    """
    result = []
    for group in groups:
        path = group.get("path", "")
        result.append((path, group))
        result.extend(flatten_group_tree(group.get("subGroups", [])))
    return result


def restore_members(group: dict, group_id: str,
                    base_url: str, realm: str, token: str,
                    timeout: int, verify: bool,
                    dry_run: bool, verify_members: bool = False) -> tuple:
    """Add members to a group. Returns (n_added, not_found_usernames, n_failed)."""
    members = group.get("members", [])
    n_added = 0
    n_failed = 0
    not_found = []

    for member in members:
        username = member.get("username", "")
        if not username:
            continue
        if dry_run and not verify_members:
            n_added += 1
            continue
        try:
            user = find_user_by_username(base_url, realm, token,
                                         username, timeout, verify)
            if user is None:
                not_found.append(username)
                print(c(f"    member {username!r}: NOT FOUND in target realm", YELLOW))
                continue
            if dry_run:
                n_added += 1
                continue
            add_user_to_group(base_url, realm, token,
                              user["id"], group_id, timeout, verify)
            n_added += 1
        except requests.HTTPError as exc:
            print(c(f"    member {username!r}: FAILED: {exc}", RED))
            n_failed += 1

    return n_added, not_found, n_failed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    env = os.environ
    p = argparse.ArgumentParser(
        description="Backup and restore Keycloak groups with attributes, members, and hierarchy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (auto-detected):
  Backup  : no --in              Read all groups from Keycloak; write to JSON file.
  Restore : --in FILE            Read groups from JSON file; create/update in Keycloak.
  Dry-run : --in FILE --dry-run  Validate and report; no writes to Keycloak.

Examples:
  # Backup all groups to a timestamped file
  %(prog)s

  # Backup only groups with the 'gid' attribute set
  %(prog)s --include-attr gid

  # Backup groups with the 'env' attribute equal to 'prod'
  %(prog)s --include-attr env=prod

  # Backup groups with at least 5 members
  %(prog)s --min-members 5

  # Backup with specific attributes only
  %(prog)s --attrs gid,description

  # Restore from backup (skip existing groups)
  %(prog)s --in groups_myrealm_202401010900.json

  # Restore and overwrite existing groups
  %(prog)s --in groups_myrealm_202401010900.json --force

  # Restore groups only (no member assignment)
  %(prog)s --in groups_myrealm_202401010900.json --skip-members

  # Dry-run restore (no changes made)
  %(prog)s --in groups_myrealm_202401010900.json --dry-run
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
                   help="Output file path (default: groups_{realm}_{timestamp}.json)")
    p.add_argument("--no-out", action="store_true",
                   help="Skip file output (print to stdout only)")

    # Restore options
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing groups during restore (default: skip)")
    p.add_argument("--attr-mode", choices=["all", "schema", "none"], default="all",
                   help="Attribute restoration mode (default: all)")
    p.add_argument("--skip-members", action="store_true",
                   help="Skip group membership restoration during restore")
    p.add_argument("--verify-members", action="store_true",
                   help="During --dry-run, verify each member exists in target realm "
                        "(slower but accurate count). Without this flag, dry-run "
                        "assumes all backup members exist (optimistic count).")

    # Attribute schema (backup and restore)
    p.add_argument("--attr-schema", dest="attr_schema", default=None,
                   help='JSON file containing list of attribute keys: ["key1", "key2"]')
    p.add_argument("--attrs", default=None,
                   help="Comma-separated attribute keys to include (e.g. gid,description)")

    # Group filters (backup mode)
    p.add_argument("--include-attr", dest="include_attr", action="append",
                   metavar="KEY[=VALUE]",
                   help="Include groups with this attribute key (or key=value). Repeatable.")
    p.add_argument("--exclude-attr", dest="exclude_attr", action="append",
                   metavar="KEY[=VALUE]",
                   help="Exclude groups with this attribute key (or key=value). Repeatable.")
    p.add_argument("--include-name", dest="include_name", action="append",
                   metavar="NAME",
                   help="Include group by exact name. Repeatable.")
    p.add_argument("--exclude-name", dest="exclude_name", action="append",
                   metavar="NAME",
                   help="Exclude group by exact name. Repeatable.")
    p.add_argument("--min-members", dest="min_members", type=int, default=None,
                   help="Only include groups with at least N members.")
    p.add_argument("--max-members", dest="max_members", type=int, default=None,
                   help="Only include groups with at most N members.")

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
        print("Fetching top-level groups …")
        try:
            top_groups = fetch_top_level_groups(
                args.base_url, args.realm, token, args.timeout, verify
            )
        except requests.HTTPError as exc:
            die(f"Failed to fetch groups: {exc}")
        print(c(f"  Found {len(top_groups)} top-level group(s)", GREEN))

        print("Fetching full group details (members and subgroups) …")
        full_groups = []
        for i, g in enumerate(top_groups, 1):
            print(c(f"  [{i}/{len(top_groups)}] {g['name']} …", DIM), end="\r")
            try:
                full = fetch_group_full(
                    args.base_url, args.realm, token,
                    g["id"], args.timeout, verify, schema_keys,
                )
                full_groups.append(full)
            except requests.HTTPError as exc:
                print(c(f"\n  Warning: failed to fetch group {g['name']!r}: {exc}", YELLOW))
        print(c(f"  Done ({len(full_groups)} top-level group(s) fetched)           ", GREEN))

        # Apply filters
        has_filters = any([
            args.include_attr, args.exclude_attr,
            args.include_name, args.exclude_name,
            args.min_members is not None, args.max_members is not None,
        ])
        if has_filters:
            filtered_groups = []
            for g in full_groups:
                result = apply_group_filters(g, args)
                if result is not None:
                    filtered_groups.append(result)
            total_filtered  = count_groups_in_tree(filtered_groups)
            total_original  = count_groups_in_tree(full_groups)
            if total_filtered != total_original:
                print(c(
                    f"  After filtering: {total_filtered} group(s) "
                    f"(from {total_original})",
                    YELLOW,
                ))
            full_groups = filtered_groups

        total_count = count_groups_in_tree(full_groups)
        output = {
            "meta": {
                "timestamp":       ts(),
                "source_base_url": args.base_url,
                "source_realm":    args.realm,
                "total_groups":    total_count,
            },
            "groups": full_groups,
        }

        print()
        header("Backup Summary")
        print(f"  Total groups (including subgroups): {total_count}")
        print()

        if args.no_out:
            print_json(output)
        else:
            out = args.outfile or f"groups_{args.realm}_{ts()}.json"
            write_json(out, output)
        return

    # =========================================================================
    # RESTORE / DRY-RUN MODE
    # =========================================================================
    print(f"Reading input file: {args.infile}")
    backup_data = read_json(args.infile)

    if not isinstance(backup_data, dict) or "groups" not in backup_data:
        die(
            f"Input file {args.infile!r} does not look like a groups backup "
            f"(expected top-level 'groups' array)."
        )

    groups_to_restore = backup_data["groups"]
    meta = backup_data.get("meta", {})
    total_in_backup = count_groups_in_tree(groups_to_restore)
    print(c(f"  OK — {total_in_backup} group(s) in backup (including subgroups)", GREEN))
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

    # Flatten group tree into DFS pre-order (parents before children)
    flat_groups = flatten_group_tree(groups_to_restore)

    # Counters
    n_created          = 0
    n_skipped          = 0
    n_updated          = 0
    n_failed           = 0
    n_members_added    = 0
    n_members_failed   = 0
    members_not_found: list = []

    # path → KC group ID (used to look up parent IDs for child creation)
    path_to_id: dict = {}

    for path, group in flat_groups:
        name = group.get("name", "(unnamed)")
        path_segments = [s for s in path.split("/") if s]
        depth = len(path_segments) - 1
        indent = "  " * depth

        print(f"{indent}[{c(path, CYAN)}]", end="  ")

        # Check if group already exists in target realm
        try:
            existing = find_group_by_path(
                args.base_url, args.realm, token, path, args.timeout, verify
            )
        except requests.HTTPError as exc:
            print(c(f"FAILED (lookup): {exc}", RED))
            n_failed += 1
            continue

        group_payload = prepare_group_for_restore(group, attr_mode, schema_keys)

        if existing:
            existing_id = existing["id"]
            path_to_id[path] = existing_id

            if not args.force:
                print(c("SKIPPED (exists)", YELLOW))
                n_skipped += 1
                # Still restore members even for skipped groups
                if not args.skip_members:
                    added, not_found, m_failed = restore_members(
                        group, existing_id,
                        args.base_url, args.realm, token,
                        args.timeout, verify, args.dry_run,
                        args.verify_members,
                    )
                    n_members_added  += added
                    n_members_failed += m_failed
                    members_not_found.extend(not_found)
                continue

            # Force update
            if args.dry_run:
                print(c("would UPDATE (dry-run)", YELLOW))
                n_updated += 1
            else:
                try:
                    update_group(args.base_url, args.realm, token,
                                 existing_id, group_payload, args.timeout, verify)
                    print(c("UPDATED", GREEN))
                    n_updated += 1
                except requests.HTTPError as exc:
                    body = exc.response.text if exc.response is not None else "n/a"
                    print(c(f"FAILED: {exc} — {body}", RED))
                    n_failed += 1
                    continue

        else:
            # Create group
            if args.dry_run:
                print(c("would CREATE (dry-run)", YELLOW))
                n_created += 1
                path_to_id[path] = f"dry-run-{name}"
            else:
                try:
                    if len(path_segments) <= 1:
                        # Top-level group
                        new_id = create_top_level_group(
                            args.base_url, args.realm, token,
                            group_payload, args.timeout, verify,
                        )
                    else:
                        parent_path = "/" + "/".join(path_segments[:-1])
                        if parent_path not in path_to_id:
                            print(c(
                                f"FAILED: parent group {parent_path!r} was not "
                                f"created/found — skipping child",
                                RED,
                            ))
                            n_failed += 1
                            continue
                        new_id = create_child_group(
                            args.base_url, args.realm, token,
                            path_to_id[parent_path], group_payload,
                            args.timeout, verify,
                        )
                    path_to_id[path] = new_id
                    print(c("CREATED", GREEN))
                    n_created += 1
                except requests.HTTPError as exc:
                    body = exc.response.text if exc.response is not None else "n/a"
                    print(c(f"FAILED: {exc} — {body}", RED))
                    n_failed += 1
                    continue

        # Restore members (for created/updated groups)
        if not args.skip_members and path in path_to_id:
            added, not_found, m_failed = restore_members(
                group, path_to_id[path],
                args.base_url, args.realm, token,
                args.timeout, verify, args.dry_run,
                args.verify_members,
            )
            n_members_added   += added
            n_members_failed  += m_failed
            members_not_found.extend(not_found)

    print()
    header("Restore Summary")
    if mode == "dryrun":
        print(f"  Groups would create : {n_created}")
        print(f"  Groups would update : {n_updated}  (--force)")
        print(f"  Groups skipped      : {n_skipped}  (already exist)")
        if not args.skip_members:
            if args.verify_members:
                print(f"  Members would add   : {n_members_added}  (verified)")
                if members_not_found:
                    print(c(
                        f"  Members not found   : {len(members_not_found)} username(s) "
                        f"not found in target realm",
                        YELLOW,
                    ))
                    for u in members_not_found:
                        print(c(f"    - {u}", DIM))
            else:
                print(f"  Members would add   : {n_members_added}  "
                      f"(optimistic — use --verify-members for accurate count)")
    else:
        print(f"  Groups created  : {n_created}")
        print(f"  Groups updated  : {n_updated}  (--force)")
        print(f"  Groups skipped  : {n_skipped}  (already exist — use --force to overwrite)")
        print(f"  Groups failed   : {n_failed}")
        if not args.skip_members:
            print(f"  Members added   : {n_members_added}")
            if n_members_failed:
                print(f"  Members failed  : {n_members_failed}")
            if members_not_found:
                print(c(
                    f"  Members not found : {len(members_not_found)} username(s) "
                    f"not found in target realm",
                    YELLOW,
                ))
                for u in members_not_found:
                    print(c(f"    - {u}", DIM))

    print()
    if mode != "dryrun" and n_failed == 0:
        print(c("All done.", GREEN, BOLD))


if __name__ == "__main__":
    main()
