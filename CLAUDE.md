# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Python tool (or tools) to backup Keycloak users and groups (along with their attributes) from an existing realm and migrate them to a new realm on the same Keycloak instance or a fresh keycloak instance.

### Detail

What should be backed up:
- Users
  - user attributes
  - idp provider
- Groups
  - group attributes
  - group members
  - child groups
  - child group attributes
  - full group paths (so child groups are correctly nested)

What should be restored:
- Users
  - user attributes
- Groups
  - group attributes
  - group members
  - child groups
  - child group attributes
  - full group paths (so child groups are correctly nested)

### Users

#### Backup mode (or Read Mode)

The tool should be able to read all users from a Keycloak realm and store them in a .json file.

The tool should also be able to read all groups and memberships from a Keycloak realm and store them along with their attributes and members in a .json file.

Pagination can be supported if it makes backup/restore to/from keycloak easier, however this does make it less user-searchable.

##### User filtering

The backup function should have the ability to filter users, with the following options:

- certain attribute key, or value that is empty, a specific value, or any value
- Users who have a certain IDP provider value, or no IDP provider specified
- Users who are a member of a certain group
- Specific attribute values (i.e: uids, email addresses, etc) (this means we can manually select x number of users to create a test set from)

Filters must be both Inclusive (include users who meet a certain criteria) and Exclusive (exclude users who meet a certain criteria).

##### Attribute filtering

Ability to backup all user attributes, or a subset of attributes based on their key.

This schema could be stored in a file and read into the tool instead of being provided in the tool execution flags.

#### Restore mode (or Write mode)

The tool should be able to restore backed up users from a specified .json file (or files if pagination is required) to a keycloak instance.

By default users and attributes should not be overwritten. An option to force overwrite these would be helpful

If a user exists the restore process should skip that users and report it in a summary at the end of the restoration process.

When restoring user accounts, the option should be present to allow the restoration of all attributes, a subset of attributes (based on a predefined schema - see Backup mode), or no attributes (if this is possible - obviously certain minimum key attributes would be required. unsure how we would handle this but we'd need to tailor it for the environment we're restoring into as each environment could have different mininmum attribute requirements)

There should be an option to re-add members of the group based on the backup data, or to just repopoulate the list of groups and not re-add members.

### Groups

#### Backup mode (or Read Mode)

Read existing groups and store them along with their attributes, child groups, and members, in a .json file.

##### Group filtering

It should be possible to filter groups based on the following:

- certain attribute key, or value that is empty, a specific value, or any value
- total number of members, or no members.
- Specific attribute values (i.e: gids, names, etc) (this means we can manually select x number of groups to create a test set from).

##### Attribute filtering

Ability to backup all group attributes, or a subset of attributes based on their key.

This schema could be stored in a file and read into the tool instead of being provided in the tool execution flags.

#### Restore mode

The tool should be able to restore backed up groups from a specified .json file (or files if pagination is required) to a keycloak instance.

By default groups and attributes should not be overwritten. An option to force overwrite these would be helpful

If a group exists the restore process should skip that group and report it in a summary at the end of the restoration process.

When restoring groups, the option should be present to allow the restoration of all attributes, a subset of attributes (based on a predefined schema - see Backup mode), or no attributes (if this is possible - obviously certain minimum key attributes would be required. unsure how we would handle this but we'd need to tailor it for the environment we're restoring into as each environment could have different mininmum attribute requirements)

## Project Status

This is a green field project. We have existing Python scripts that we can use for guidance for common code and structure, however this is a new project.

## Expected Architecture

When implemented, this tool will likely:
- Connect to a source Keycloak instance via the Keycloak Admin REST API
- Export/backup user data from a specified realm
- Import/migrate users into a target realm (potentially on a different Keycloak instance)

## Development Setup

No build or test infrastructure exists yet. Once established, update this file with:
- How to install dependencies (e.g., `pip install -e .[dev]` or `pip install -r requirements.txt`)
- How to run tests (e.g., `pytest`)
- How to lint (e.g., `ruff check .` or `flake8`)

## Environmental variables

The following environmental variables are set in another Keycloak related script. These should be reused where possible

| Environment variable | CLI flag          | Description                         |
|----------------------|-------------------|-------------------------------------|
| `KC_BASE_URL`        | `--base-url`      | Keycloak base URL (no trailing `/`) |
| `KC_REALM`           | `--realm`         | Realm name                          |
| `KC_CLIENT_ID`       | `--client-id`     | Service account client ID           |
| `KC_CLIENT_SECRET`   | `--client-secret` | Service account client secret       |

## Python modules

- requests

Add additional python modules as required.
