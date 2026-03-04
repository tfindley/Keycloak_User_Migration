# Keycloak User Migration

Two standalone Python CLI scripts to backup and restore Keycloak users and groups (with attributes, IDP links, memberships, and nested group hierarchy) from one realm to another — potentially across different Keycloak instances.

## Scripts

| Script | Purpose |
|---|---|
| `keycloak_users.py` | Backup and restore users with attributes and federated identity links |
| `keycloak_groups.py` | Backup and restore groups with attributes, members, and nested subgroup hierarchy |

## Requirements

```
pip install requests
```

## Authentication

Both scripts authenticate via OAuth2 client credentials (service account). Connection parameters can be set via environment variables or CLI flags:

| Environment variable | CLI flag | Description |
|---|---|---|
| `KC_BASE_URL` | `--base-url` | Keycloak base URL (no trailing `/`) |
| `KC_REALM` | `--realm` | Realm name |
| `KC_CLIENT_ID` | `--client-id` | Service account client ID |
| `KC_CLIENT_SECRET` | `--client-secret` | Service account client secret |

```bash
export KC_BASE_URL=https://keycloak.example.com
export KC_REALM=myrealm
export KC_CLIENT_ID=migration-client
export KC_CLIENT_SECRET=your-secret
```

---

## keycloak_users.py

### Modes

| Mode | How to invoke | Description |
|---|---|---|
| **Backup** | no `--in` | Reads all users from Keycloak, writes to JSON |
| **Restore** | `--in FILE` | Reads users from JSON, creates/updates in Keycloak |
| **Dry-run** | `--in FILE --dry-run` | Validates and reports what would happen, no writes |

### Backup

```bash
# Backup all users
python keycloak_users.py

# Backup to a specific file
python keycloak_users.py --out my_users.json

# Print to stdout only (no file written)
python keycloak_users.py --no-out
```

#### User Filters

Filters can be combined. Include filters are OR-combined (any match passes). Exclude filters are OR-combined (any match fails). Excludes are applied after includes.

```bash
# Include only users linked to the google IDP
python keycloak_users.py --include-idp google

# Include users with no IDP configured
python keycloak_users.py --include-idp none

# Include users who are members of /staff, exclude those also in /legacy
python keycloak_users.py --include-group /staff --exclude-group /legacy

# Include users who have the 'department' attribute set to 'eng'
python keycloak_users.py --include-attr department=eng

# Include users who have any value for 'uid'
python keycloak_users.py --include-attr uid

# Exclude users with empty 'email' attribute
python keycloak_users.py --exclude-attr email=

# Multiple include filters (OR logic — user matching any filter is included)
python keycloak_users.py --include-idp google --include-idp github
```

#### Attribute Filtering

Control which user attributes are included in the backup output:

```bash
# Keep only specific attributes inline
python keycloak_users.py --attrs uid,email,department

# Keep only attributes listed in a schema file (JSON array of key names)
python keycloak_users.py --attr-schema schema.json
```

**schema.json example:**
```json
["uid", "email", "department", "employeeType"]
```

### Restore

```bash
# Restore users (skip existing by default)
python keycloak_users.py --in users_myrealm_202401010900.json

# Overwrite existing users
python keycloak_users.py --in users_myrealm_202401010900.json --force

# Dry-run (no changes made)
python keycloak_users.py --in users_myrealm_202401010900.json --dry-run
```

#### Attribute Restore Modes

```bash
# Restore all attributes (default)
python keycloak_users.py --in backup.json --attr-mode all

# Restore no attributes
python keycloak_users.py --in backup.json --attr-mode none

# Restore only attributes listed in a schema file
python keycloak_users.py --in backup.json --attr-mode schema --attr-schema schema.json
```

#### Restore Summary

```
=== Restore Summary ===
  Created : 38
  Updated : 2   (--force)
  Skipped : 4   (already exist — use --force to overwrite)
  Failed  : 0

  IDP links not restored: 7 user(s) had federatedIdentities (manual action required)
    - jdoe
    - asmith
```

> **Note:** Federated identities (IDP links) are backed up but **not** restored automatically. The target Keycloak must have the IDP configured, and linking must be done manually or via a separate process. Users with IDP links are listed in the restore summary.

### Backup Output Format

```json
{
  "meta": {
    "timestamp": "202401010900",
    "source_base_url": "https://keycloak.example.com",
    "source_realm": "myrealm",
    "total_users": 42
  },
  "users": [
    {
      "id": "...",
      "username": "jdoe",
      "email": "jdoe@example.com",
      "firstName": "Jane",
      "lastName": "Doe",
      "enabled": true,
      "attributes": { "uid": ["12345"] },
      "federatedIdentities": [
        { "identityProvider": "google", "userId": "...", "userName": "jdoe@gmail.com" }
      ]
    }
  ]
}
```

---

## keycloak_groups.py

### Modes

| Mode | How to invoke | Description |
|---|---|---|
| **Backup** | no `--in` | Reads all groups from Keycloak, writes to JSON |
| **Restore** | `--in FILE` | Reads groups from JSON, creates/updates in Keycloak |
| **Dry-run** | `--in FILE --dry-run` | Validates and reports what would happen, no writes |

### Backup

```bash
# Backup all groups
python keycloak_groups.py

# Backup to a specific file
python keycloak_groups.py --out my_groups.json

# Print to stdout only
python keycloak_groups.py --no-out
```

#### Group Filters

```bash
# Include only groups with the 'gid' attribute set
python keycloak_groups.py --include-attr gid

# Include only groups where 'env' attribute equals 'prod'
python keycloak_groups.py --include-attr env=prod

# Exclude groups named 'legacy'
python keycloak_groups.py --exclude-name legacy

# Include only groups with at least 5 members
python keycloak_groups.py --min-members 5

# Include only groups with at most 100 members
python keycloak_groups.py --max-members 100
```

> **Note:** Filters apply to each group node independently. A filtered-out parent group still passes through as a shell if any of its child groups match the filters — this preserves the correct nesting structure.

#### Attribute Filtering

```bash
# Keep only specific attributes inline
python keycloak_groups.py --attrs gid,description,owner

# Keep only attributes listed in a schema file
python keycloak_groups.py --attr-schema schema.json
```

### Restore

Groups are restored in path-depth order (parents before children) so that child groups can be correctly nested.

```bash
# Restore groups (skip existing by default)
python keycloak_groups.py --in groups_myrealm_202401010900.json

# Overwrite existing groups
python keycloak_groups.py --in groups_myrealm_202401010900.json --force

# Restore groups without re-assigning members
python keycloak_groups.py --in groups_myrealm_202401010900.json --skip-members

# Dry-run (no changes made)
python keycloak_groups.py --in groups_myrealm_202401010900.json --dry-run
```

#### Attribute Restore Modes

```bash
# Restore all attributes (default)
python keycloak_groups.py --in backup.json --attr-mode all

# Restore no attributes
python keycloak_groups.py --in backup.json --attr-mode none

# Restore only attributes listed in a schema file
python keycloak_groups.py --in backup.json --attr-mode schema --attr-schema schema.json
```

#### Restore Summary

```
=== Restore Summary ===
  Groups created  : 12
  Groups updated  : 1   (--force)
  Groups skipped  : 3   (already exist — use --force to overwrite)
  Groups failed   : 0
  Members added   : 47
  Members not found : 2 username(s) not found in target realm
    - olduser1
    - olduser2
```

### Backup Output Format

Groups are stored as a nested tree preserving the original hierarchy:

```json
{
  "meta": {
    "timestamp": "202401010900",
    "source_base_url": "https://keycloak.example.com",
    "source_realm": "myrealm",
    "total_groups": 15
  },
  "groups": [
    {
      "id": "...",
      "name": "RootGroup",
      "path": "/RootGroup",
      "attributes": { "gid": ["100"] },
      "members": [
        { "id": "...", "username": "jdoe" }
      ],
      "subGroups": [
        {
          "id": "...",
          "name": "ChildGroup",
          "path": "/RootGroup/ChildGroup",
          "attributes": {},
          "members": [],
          "subGroups": []
        }
      ]
    }
  ]
}
```

---

## Common Options

Both scripts support these options:

| Flag | Description |
|---|---|
| `--timeout N` | HTTP timeout in seconds (default: 30) |
| `--insecure` | Disable TLS certificate verification |
| `--color auto\|always\|never` | Colorize terminal output (default: auto) |

---

## Typical Migration Workflow

```bash
# 1. Export env vars for the source realm
export KC_BASE_URL=https://source-keycloak.example.com
export KC_REALM=source-realm
export KC_CLIENT_ID=migration-client
export KC_CLIENT_SECRET=source-secret

# 2. Backup groups (must be done before users if you need group-based user filters)
python keycloak_groups.py
# → groups_source-realm_202401010900.json

# 3. Backup users
python keycloak_users.py
# → users_source-realm_202401010900.json

# 4. Switch to target realm
export KC_BASE_URL=https://target-keycloak.example.com
export KC_REALM=target-realm
export KC_CLIENT_SECRET=target-secret

# 5. Dry-run restore to verify
python keycloak_groups.py --in groups_source-realm_202401010900.json --dry-run
python keycloak_users.py  --in users_source-realm_202401010900.json  --dry-run

# 6. Restore groups (creates hierarchy and assigns members)
python keycloak_groups.py --in groups_source-realm_202401010900.json

# 7. Restore users
python keycloak_users.py --in users_source-realm_202401010900.json
```
