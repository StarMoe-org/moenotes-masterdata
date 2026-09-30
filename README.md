# MoeNotes Masterdata

Generated masterdata snapshots for MoeNotes, separated into `en`, `hk-tw-mo`,
`jp`, and `kr`. The publisher downloads from the configured
`MASTERDATA_BASE_URL/index.json`; this repository does not contain the
moenotes-sync converter or the original binary inputs.

## Validation

`scripts/validate_snapshot.py` checks publisher metadata before downloading and
then checks the downloaded snapshot before it can replace a published region:

- The region, `data_path`, and `snapshot_path` agree; versions and counts exist.
- The index provides each JSON file's SHA256, including `MasterManifest.json`.
- The manifest's exact bytes match `entry.manifest_sha256`; its version and table
  inventory match the entry and the downloaded JSON files.
- Tables contain `_allData` object lists; their total row count matches metadata.

The index's `.files` hashes describe the downloaded, converted JSON outputs.
`MasterManifest.files[].hash` and `.size` instead describe the original `.bin`
inputs. A binary hash cannot authenticate its decoded JSON. Without those inputs,
the converter, and an independently provided index, local repository checks prove
metadata/content consistency, not independently authenticated conversion results.

Checks use exact file bytes. Use a checkout that preserves Git blob bytes when
publishing (for example, `core.autocrlf=false` when the checkout is created).
Windows CRLF conversion can change the manifest SHA256; changing the setting
after checkout does not repair already-converted files. The helper rejects that
mismatch rather than inventing replacement expected hashes.

## Local replacement and recovery

`update_snapshot.py` prepares a recoverable transaction on the repository's
filesystem. It does not download files or push Git refs.

```sh
python3 scripts/update_snapshot.py --repository . recover
python3 scripts/update_snapshot.py --repository . publish en /tmp/en-entry.json --directory /tmp/en
```

`recover` prints `0` or `1`: whether a recognized local transaction commit may
need publication. `publish` prints `unchanged` or `committed`. A repeat push of
an already-published, recognized HEAD is harmless. The workflow recovers before
any download, validates downloads, publishes each region locally, and pushes only
after every region succeeds. Local staging/commit failures stop that final push.

Before replacing live files, the helper:

1. Refuses existing staged changes, target-region/current-version changes or
   untracked target files, and `index.lock`. It holds a process lock for its work.
2. Persists a `preparing` journal, copies and verifies the old region,
   `current_version.json`, and Git index, and retains an immutable incoming copy.
3. Prepares the installation directory and full new metadata, validates the copied
   snapshot, and rechecks the old files, HEAD, and index.
4. Persists `installing` before moving any live path. It moves the original region
   into the transaction, installs the new region, and replaces the metadata file.
5. Stages only the region and `current_version.json`, then commits with a unique
   `MoeNotes-Transaction` trailer. Recovery verifies the parent HEAD, trailer, and
   expected Git blob IDs, including Git's path-specific byte conversion.

Transactions remain under `.moenotes-transactions/<transaction-id>/`, excluded by
`.gitignore`. `old-region/`, `old-current_version.json`, and `old-index` retain
verified previous bytes; `displaced-old-region/` also retains the original region
after its move. Incoming and withdrawn installation artifacts are private there.
No old backups are automatically deleted.

The active marker stores only a transaction ID. Its journal uses schema version
1, a supported region, relative `index_path`, original HEAD and SHA256 inventories,
new Git blob IDs, `git_started`, and the transaction state. Recovery validates the
marker/journal and binds `index_path` to this repository's actual index before
restoring it. No account paths, tokens, or remote authentication are recorded.

An uncommitted interrupted installation restores the old region, metadata, and
index. Interrupted preparation has changed no live files and does not require
unfinished backups. Interrupted rollback remains retryable from retained backups.
A commit that already succeeded is completed rather than rolled back. Recognition
also covers interruption after finalization but before the caller records success.

The two live paths are **not simultaneously atomic to concurrent readers**;
process interruption may expose a missing region or a temporarily mixed pair until
recovery. Run recovery before readers, checkout cleanup, or other workspace writes.
Each replacement uses same-filesystem rename; file contents are flushed, with
parent-directory fsync on platforms that support it. Arbitrary power loss,
filesystem corruption, and concurrent external Git writers are not guaranteed.

An unexpected HEAD/index change or an unowned `index.lock` leaves recovery pending
and preserves the backups; the helper never resets refs, discards someone else's
staging, or deletes an unknown lock. Resolve the external writer/lock first, then
retry recovery. Ordinary write failures clean this invocation's unique scratch
files; an actual process exit can leave a private scratch file in transaction
storage, never a partially written live JSON file.

The workflow disables checkout cleanup. A checkout or another operation that
changes HEAD before recovery can still make safe automatic recovery impossible;
that situation is conservatively refused with backups retained. Git push/rebase
errors and live HTTP race behavior are outside the offline transaction tests.

## Offline tests

Python standard library, Git, and Bash are sufficient; no dependency installation
or game data fetch is required. Tests use small synthetic JSON snapshots and
private temporary Git repositories. No production repository is published.

```sh
python3 -B -m unittest discover -s tests -v
```

The suite injects partial writes, missing inputs, directory/metadata move failures,
Git stage/commit failures, interrupted preparation/install/commit/finalization,
and interrupted or failed rollback. Interrupted cases run independent child
processes that call `os._exit`. It verifies old backup bytes, live metadata/data
pairs, Git index restoration, commit recognition, and preservation of user staging.
When copying the executable test sources, retain `scripts/`, `tests/`, and
`.github/workflows/pull-masterdata.yml` at the same root; workflow-boundary tests
load that relative workflow path.
