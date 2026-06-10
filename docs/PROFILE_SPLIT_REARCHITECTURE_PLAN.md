# Profile-Split UserData Rearchitecture Plan

## Goal

Replace the current shared-root + mirror-isolated runtime model with a
per-profile UserData architecture:

- each Chromium profile owns an independent UserData root
- each profile has its own lock domain
- keepalive, MCP sessions, and manual launches coordinate per profile
- backup/mirror operates per profile root instead of cloning one shared root

Target root configured in GUI:

```text
D:\softs\chromium\UserData\UserDataSplited
```

Target layout:

```text
UserDataSplited\
  UserDataProfile1\
    Profile 1\
    Local State
    Preferences / root-level Chromium state
  UserDataProfile2\
    Profile 2\
  ...
  mirror_disk\
    template_root.zip
    profiles\
      profile-1.zip
      profile-2.zip
      ...
```

## Why This Replaces The Current Mirror Model

The current design treats one shared `user_data_root` as the unit of truth and
tries to recover concurrency by extracting isolated runtimes from snapshots.
That causes several structural problems:

- state drift between live root and mirror snapshot
- weak ownership: same logical profile may exist in both live root and mirror
- keepalive and external running detection act at shared-root scope
- one shared Chromium root can block unrelated profiles
- mirror refresh and cleanup are expensive and hard to reason about

Per-profile UserData makes the lock and state boundary match the actual unit of
work: one profile.

## Desired Properties

1. A profile can have only one live owner at a time.
2. Different profiles can run concurrently without whole-root conflicts.
3. Keepalive locks only the profile it is operating on.
4. Mirror/backup only snapshots the owning profile root.
5. New profile creation comes from a lightweight template root plus a named
   profile directory.
6. Cache cleanup can run after keepalive per profile, and also as a bulk
   maintenance action.

## New Storage Model

### Profile root

Each logical profile maps to its own UserData root:

- `Profile 1` -> `...\\UserDataProfile1`
- Chromium launches with:
  - `--user-data-dir=<profile_root>`
  - `--profile-directory=Profile 1`

This keeps Chromium's root-level state (`Local State`, root services,
extension data, etc.) isolated per profile.

### Mirror / backup

Mirror remains under:

```text
UserDataSplited\mirror_disk
```

But the mirror unit changes:

- one template root snapshot for newly created profiles
- one profile-root snapshot per profile root
- no more shared-root runtime extraction model as the primary concurrency path

### Template root

Maintain a clean lightweight template root containing:

- root-level Chromium files required for a valid UserData root
- no profile directories
- no cache directories
- no lock files

New profile creation:

1. expand/copy template root into `UserDataProfileN`
2. create `Profile N` directory inside it
3. initialize bookmarks and other defaults

## New Lock Model

### Per-profile ownership lock

Introduce one lock file per profile root, for example:

```text
UserDataProfile1\.profile_runtime.lock
```

The lock owner can be:

- GUI manual launch
- keepalive for that profile
- MCP session for that profile

### Global keepalive scheduler lock

Keep one lightweight global scheduler/job lock only for the scheduler process
itself, not for all profiles' browser usage.

### Effects

- manual launch of `Profile 1` blocks MCP/keepalive only for `Profile 1`
- keepalive on `Profile 4` does not block MCP on `Profile 1`
- different profiles may run concurrently
- same profile must not run concurrently from multiple owners

## Concurrency Model Changes

Current:

- `block`
- `mirror_isolated`

Target:

- `per_profile_live`

Optional later mode:

- `per_profile_snapshot_fallback`

But phase 1 should simplify, not multiply modes. The recommended first target is
one explicit mode:

- launch real per-profile root live
- one profile, one owner
- parallel only across different profiles

## Config Changes

### Paths

Replace shared-root semantics with split-root semantics.

Current:

- `paths.user_data_root`
- `paths.mirror_user_data_root`

Target:

- `paths.user_data_profiles_root`
- `paths.mirror_user_data_root`

GUI default:

- `D:\softs\chromium\UserData\UserDataSplited`

### Profile metadata

Each profile record should store enough data to map to its root:

- `profile_name`
- `user_data_dir_name` such as `UserDataProfile1`

Do not derive this only from display ordering; persist it explicitly so rename
or import logic is stable.

## API / Runtime Changes

### Path resolution helpers

Add helpers:

- `get_profile_user_data_root(config, profile_name) -> str`
- `get_profile_directory_path(config, profile_name) -> str`
- `discover_profiles_from_split_roots(root) -> List[profile metadata]`

All browser engines must stop reading the shared `paths.user_data_root`
directly for launches.

### Browser engine contract

Browser engines should launch from resolved per-profile root:

- `user_data_dir = get_profile_user_data_root(...)`
- `profile_directory = profile_name`

This applies to:

- `selenium_uc`
- `patchright`
- `playwright_cli`

### SessionManager governance

Governance must become per-profile:

- external running detection should resolve which profile root a process belongs to
- busy state should block only matching profile root
- allow concurrent sessions for different profile roots
- remove shared-root mirror fallback from normal MCP startup path

### Keepalive

Keepalive should:

- acquire profile lock per profile
- run profile by profile without globally blocking all profiles
- optionally skip locked profiles and continue with others
- clean caches for that profile after run
- refresh that profile snapshot after run if mirror enabled

## GUI Changes

1. Replace root path label/semantics:
   - shared `user_data_root` -> split `user_data_profiles_root`
2. Profile creation:
   - create a per-profile root directory
   - populate from template root
3. Profile deletion:
   - remove that per-profile root safely
4. Profile detail pane:
   - show per-profile root path
   - show current lock owner/state
5. Concurrency mode:
   - deprecate `mirror_isolated`
   - show explicit per-profile live governance mode

## Migration Strategy

### Phase 1: non-destructive migration

Add a migration utility:

1. read old shared root
2. for each discovered `Profile N`
   - create `UserDataProfileN`
   - copy root-level required files
   - copy `Profile N`
   - exclude caches and locks
3. write updated config with `user_data_dir_name`
4. keep old root untouched until validation passes

### Phase 2: switch runtime

After migration success:

- GUI and MCP use split roots only
- old shared-root mirror path is no longer the runtime path

### Phase 3: retire old mirror-isolated flow

- remove runtime extraction for normal MCP concurrency
- keep snapshot only for backup/recovery/new-profile seeding

## Testing Matrix

### Unit tests

1. profile root resolution from config
2. discovery of profiles from split roots
3. migration from shared root to split roots
4. per-profile lock acquisition/release
5. keepalive skip/continue behavior when one profile is locked
6. plugin/site state still preserved across migration

### Integration tests

1. `Profile 1` manual launch blocks MCP only for `Profile 1`
2. MCP on `Profile 2` still starts while `Profile 1` is open
3. keepalive on `Profile 4` does not block MCP on `Profile 1`
4. new profile creation from template root succeeds
5. post-keepalive cache cleanup reduces profile-root size
6. per-profile snapshot refresh updates only touched profile

### Real validation

1. start two MCP sessions on different profiles concurrently
2. run keepalive on one profile while another profile is used manually
3. verify live login state matches what GUI/MCP sees for the same profile
4. verify no mirror/live confusion remains

## Delivery Sequence

1. Add split-root path model and helper API.
2. Add migration utility and tests.
3. Update GUI create/delete/sync/discovery to split roots.
4. Update all engines to launch from per-profile root.
5. Replace SessionManager governance with per-profile ownership.
6. Narrow keepalive locking to per-profile scope.
7. Convert mirror manager to per-profile snapshot/backup mode.
8. Add post-keepalive cache cleanup.
9. Update docs, skills, templates, and example config.
10. Build, replace desktop app, and run real multi-profile validation.

## Acceptance Criteria

1. Same profile cannot be used simultaneously by GUI/MCP/keepalive.
2. Different profiles can run concurrently without mirror runtime extraction.
3. Keepalive no longer blocks all MCP usage.
4. Live state and MCP-visible state are consistent for the same profile.
5. Snapshot remains only a backup/recovery mechanism, not the primary live
   concurrency mechanism.
