# Migrate an existing profile to Discord Relay

This is a reversible **source migration candidate**, tested in disposable homes.
It is not a live migration or native installer acceptance. The new manifest name
is `hermes-discord-relay`. The former name is `hermes-discord-thread-ownership`.
The planned public source is
[dmmeteo/hermes-discord-relay](https://github.com/dmmeteo/hermes-discord-relay).
The repository rename and publication remain pending.

## Preserve the compatibility boundary

Keep these names and values unchanged:

- The platform ID `discord`, bot token, native authorization, and Discord tools.
- The entire `discord:` config block, including `thread_ownership_transfer`,
  `relay`, and the ownership environment switch.
- `$HERMES_HOME/discord_thread_ownership.json`.
- `$HERMES_HOME/discord_relay_receipts.sqlite3`, including any SQLite sidecars.

No state conversion is needed. Do not copy stores between profiles or delete
receipts to make migration succeed. The new activation key is not an alias for
the old key. Both installed copies could register the same Discord platform.
Move the old package outside `plugins/` before discovery of the new package.
A disabled old profile must remain disabled after migration.

## Rehearse the source migration

Run this first against a disposable copy. A later live rollout needs separate
owner authorization. Stop that profile's gateway and every writer before any
live migration or rollback. This procedure does not stop or start a gateway.

Use the existing Python interpreter that has PyYAML. Do not bootstrap Hermes or
install packages. Export these environment variables with absolute paths:

- `PROFILE_HOME` is the one profile to migrate.
- `CANDIDATE_DIR` is a clean, committed checkout with the new manifest name.
- `MIGRATION_BACKUP` is a new directory outside `plugins/` and the candidate.
- `MIGRATION_PYTHON` is the existing interpreter with PyYAML.

For the source checkpoint, use the accepted local commit. After publication,
review and pin the published commit before any rollout. The script clones only
local source. It does not fetch the planned URL or call package management.

<!-- migration-apply -->
```bash
"${MIGRATION_PYTHON:?}" - <<'PY'
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

home = Path(os.environ['PROFILE_HOME']).resolve()
source = Path(os.environ['CANDIDATE_DIR']).resolve()
backup = Path(os.environ['MIGRATION_BACKUP']).resolve()
old_id = 'hermes-discord-thread-ownership'
new_id = 'hermes-discord-relay'
old = home / 'plugins' / old_id
new = home / 'plugins' / new_id
config = home / 'config.yaml'
metadata = home / 'plugins' / '.install-metadata.json'
assert not backup.exists(), 'Use a new backup directory'
assert not backup.is_relative_to(home / 'plugins')
assert not backup.is_relative_to(source)
assert old.is_dir() and not old.is_symlink(), 'Expected an owned old package'
assert not new.exists() and not new.is_symlink(), 'Refuse a second installation'
assert yaml.safe_load((old / 'plugin.yaml').read_text())['name'] == old_id
assert yaml.safe_load((source / 'plugin.yaml').read_text())['name'] == new_id
assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'])
revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
cfg = yaml.safe_load(config.read_text())
plugins = cfg.setdefault('plugins', {})
for key in ('enabled', 'disabled'):
    if key in plugins:
        assert isinstance(plugins[key], list)
        assert new_id not in plugins[key], 'Refuse conflicting activation keys'
        plugins[key] = [new_id if value == old_id else value for value in plugins[key]]
records = json.loads(metadata.read_text()) if metadata.exists() else None
if records is not None:
    assert new_id not in records, 'Refuse conflicting installer provenance'
    records.pop(old_id, None)
backup.mkdir(parents=True)
shutil.copy2(config, backup / 'config.yaml')
if metadata.exists():
    shutil.copy2(metadata, backup / 'install-metadata.json')
subprocess.run(['git', 'clone', '--no-hardlinks', '--', str(source), str(backup / 'candidate')], check=True)
subprocess.run(['git', '-C', str(backup / 'candidate'), 'remote', 'set-url', 'origin',
                'https://github.com/dmmeteo/hermes-discord-relay.git'], check=True)
shutil.copy2(config, backup / 'config.migrated.yaml')
(backup / 'config.migrated.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))
old.rename(backup / 'old-plugin')
(backup / 'candidate').rename(new)
os.replace(backup / 'config.migrated.yaml', config)
if records is not None:
    staged = backup / 'metadata.migrated.json'
    staged.write_text(json.dumps(records, indent=2) + '\n')
    os.replace(staged, metadata)
(backup / 'revision.txt').write_text(revision + '\n')
print('Source identity migrated at', revision, '; gateway remains stopped')
PY
```

The script changes only exact activation keys. PyYAML rewrites YAML formatting
and comments, so the backup preserves the original config bytes. An old explicit
deny remains a new explicit deny. Other plugin settings and installer records
are preserved. The old installer record is archived, then removed from active
metadata. The local clone has **no new installer provenance**. Do not run native
update on it or represent it as catalog-installed.

Inspect native discovery before a separately authorized restart. Exactly one
custom Discord platform registration must come from `hermes-discord-relay`.
A second profile must retain its own adapter and state. Verify ownership and
relay duplicate rejection before considering a live rollout.

This stopped-writer procedure is not a crash-atomic transaction. If a command
fails after a move, keep the gateway stopped and use the rollback below.
Native installer acceptance is a separate remaining gate. After the public
rename, the manager must qualify a pinned native install in a fully disposable
Hermes environment before replacing this manual source candidate in profiles.
The native installer invokes Hermes PM even without plugin dependencies.

## Roll back without losing current receipts

Keep the same `PROFILE_HOME`, `MIGRATION_BACKUP`, and `MIGRATION_PYTHON` values.
Stop all profile writers before rollback. The following restores the original
package, config bytes, and installer metadata. It archives the candidate outside
plugin discovery. It leaves current ownership and receipts in place, including
any admitted requests since migration.

<!-- migration-rollback -->
```bash
"${MIGRATION_PYTHON:?}" - <<'PY'
import os
import shutil
from pathlib import Path

home = Path(os.environ['PROFILE_HOME']).resolve()
backup = Path(os.environ['MIGRATION_BACKUP']).resolve()
old = home / 'plugins' / 'hermes-discord-thread-ownership'
new = home / 'plugins' / 'hermes-discord-relay'
assert (backup / 'config.yaml').is_file()
assert (backup / 'old-plugin').is_dir(), 'No moved old package to restore'
assert not old.exists() and not old.is_symlink()
assert not (backup / 'rolled-back-candidate').exists()
if new.exists():
    new.rename(backup / 'rolled-back-candidate')
(backup / 'old-plugin').rename(old)
shutil.copy2(backup / 'config.yaml', home / 'config.yaml')
metadata = home / 'plugins' / '.install-metadata.json'
if (backup / 'install-metadata.json').exists():
    shutil.copy2(backup / 'install-metadata.json', metadata)
print('Original source identity restored; current stores preserved; gateway remains stopped')
PY
```

If migration failed before the old package moved, leave it in place and inspect
the saved config and candidate. Do not run rollback over an existing old package.
If the old revision predates relay, disable the new relay config when downgrading
and keep the receipt store for later re-enable. The source rehearsal uses the
previous relay-capable baseline, so it verifies replay denial on rollback too.
