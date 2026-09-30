#!/usr/bin/env python3
"""Recoverable local snapshot replacement; Git push remains the caller's job."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from validate_snapshot import JSON_NAME, SnapshotError, load_json, require, valid_hash, validate_snapshot

STATE_DIRECTORY = ".moenotes-transactions"
TRANSACTION_ID = re.compile(r"[0-9a-f]{32}\Z")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def sync_directory(path):
    # Python cannot open/fsync Windows directories. File contents are flushed
    # there, but directory durability across power loss is filesystem dependent.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SnapshotUpdater:
    def __init__(self, repository, checkpoint=None):
        self.repository = Path(repository).resolve()
        self.state = self.repository / STATE_DIRECTORY
        require(not self.state.is_symlink(), "transaction storage must not be a symlink")
        self.checkpoint = checkpoint or (lambda name: None)

    def git(self, *arguments, raw=None):
        result = subprocess.run(["git", *arguments], cwd=self.repository,
                                input=raw, capture_output=True)
        if result.returncode:
            raise SnapshotError(f"git {arguments[0]} failed (exit {result.returncode})")
        return result.stdout

    @contextlib.contextmanager
    def locked(self):
        self.state.mkdir(exist_ok=True)
        require(self.state.stat().st_dev == self.repository.stat().st_dev,
                "transaction storage must use the repository filesystem")
        with (self.state / "lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if not lock.tell():
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise SnapshotError("another snapshot transaction is running") from error
            else:
                import fcntl
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise SnapshotError("another snapshot transaction is running") from error
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def write(self, path, raw):
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            # Only this invocation's unique scratch file is removed. A real
            # process exit may leave it inside private transaction storage.
            if temporary.exists():
                temporary.unlink()
                sync_directory(temporary.parent)

    def save(self, transaction, journal):
        self.write(transaction / "journal.json",
                   (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode())

    def tree_hashes(self, directory):
        if not directory.exists():
            return None
        require(directory.is_dir() and not directory.is_symlink(), "snapshot must be a regular directory")
        result = {}
        for path in directory.iterdir():
            require(path.is_file() and not path.is_symlink(), "snapshot contains a non-regular file")
            result[path.name] = digest(path.read_bytes())
        return result

    def copy_tree(self, source, destination, expected, checkpoint):
        destination.mkdir()
        for name, checksum in sorted(expected.items()):
            raw = (source / name).read_bytes()
            require(digest(raw) == checksum, "snapshot changed while being copied")
            self.write(destination / name, raw)
            self.checkpoint(checkpoint)
        require(self.tree_hashes(destination) == expected, "prepared snapshot differs from its source")
        sync_directory(destination)
        sync_directory(destination.parent)

    def head(self):
        return self.git("rev-parse", "HEAD").decode().strip()

    def index_path(self):
        path = Path(self.git("rev-parse", "--git-path", "index").decode().strip())
        if not path.is_absolute():
            path = self.repository / path
        require(not path.is_symlink(), "Git index must not be a symlink")
        path = path.resolve()
        require(path.is_relative_to(self.repository),
                "Git index must be inside the repository")
        return path

    def file_hash(self, path):
        return digest(path.read_bytes()) if path.exists() else None

    def preflight(self, region):
        index = self.index_path()
        require(not index.with_name(index.name + ".lock").exists(), "Git index.lock exists")
        require(not self.git("diff", "--cached", "--name-only").strip(), "existing staged changes must be preserved")
        require(not self.git("diff", "--name-only", "--", region, "current_version.json").strip(),
                "target snapshot has uncommitted changes")
        require(not self.git("ls-files", "--others", "--exclude-standard", "-z", "--",
                             region, "current_version.json").strip(), "target snapshot has untracked files")
        return index

    def git_blob(self, name, raw):
        return self.git("hash-object", "--path=" + name, "--stdin", raw=raw).decode().strip()

    def git_tree(self, reference, region):
        result = {}
        for row in self.git("ls-tree", "-r", "-z", reference, "--", region, "current_version.json").split(b"\0"):
            if row:
                details, path = row.split(b"\t", 1)
                mode, kind, oid = details.split()
                require(mode == b"100644" and kind == b"blob", "unexpected committed snapshot entry")
                result[path.decode()] = oid.decode()
        return result

    def is_committed(self, journal):
        head = self.head()
        if head == journal["base_head"]:
            return False
        parents = self.git("rev-list", "--parents", "-n", "1", head).decode().split()
        message = self.git("show", "-s", "--format=%B", head).decode()
        require(parents == [head, journal["base_head"]]
                and ("MoeNotes-Transaction: " + journal["id"]) in message.splitlines()
                and self.git_tree(head, journal["region"]) == journal["new_git_blobs"],
                "Git HEAD changed outside the transaction; backups retained")
        journal["committed_head"] = head
        return True

    def finish(self, transaction, journal, state):
        journal["state"] = state
        self.save(transaction, journal)
        self.checkpoint("final-journal")
        (self.state / "active.json").unlink()
        sync_directory(self.state)
        self.checkpoint("finalized")

    def active(self):
        active = self.state / "active.json"
        if not active.exists():
            return None
        require(not active.is_symlink(), "transaction marker must not be a symlink")
        marker = load_json(active)
        require(isinstance(marker, dict) and isinstance(marker.get("transaction"), str)
                and TRANSACTION_ID.fullmatch(marker["transaction"]),
                "invalid transaction marker; backups retained")
        transaction = self.state / marker["transaction"]
        require(transaction.is_dir() and not transaction.is_symlink(), "missing transaction directory")
        require(not (transaction / "journal.json").is_symlink(), "journal must not be a symlink")
        journal = load_json(transaction / "journal.json")
        self.check_journal(journal, marker["transaction"])
        return transaction, journal

    def check_journal(self, journal, identity):
        require(isinstance(journal, dict) and journal.get("schema_version") == 1
                and journal.get("id") == identity
                and journal.get("region") in {"en", "hk-tw-mo", "jp", "kr"}
                and journal.get("state") in {"preparing", "prepared", "installing", "installed",
                                             "staging", "committing", "rolling_back", "rolled_back", "committed"},
                "invalid transaction journal")
        require(isinstance(journal.get("base_head"), str)
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", journal["base_head"]), "invalid base HEAD")
        index = journal.get("index_path")
        require(isinstance(index, str) and index == self.index_path().relative_to(self.repository).as_posix(),
                "journal index_path is not this repository's Git index")
        for key in ("old_metadata_sha256", "old_index_sha256", "new_metadata_sha256"):
            require(valid_hash(journal.get(key)) or (key != "new_metadata_sha256" and journal.get(key) is None),
                    "invalid journal checksum")
        for key in ("old_files", "new_files"):
            files = journal.get(key)
            require((key == "old_files" and files is None) or
                    (isinstance(files, dict) and all(isinstance(name, str) and JSON_NAME.fullmatch(name)
                     and valid_hash(checksum) for name, checksum in files.items())), "invalid journal file inventory")
        require(type(journal.get("git_started")) is bool and isinstance(journal.get("new_git_blobs"), dict),
                "invalid journal Git state")

    def verify_backups(self, transaction, journal):
        require(self.tree_hashes(transaction / "old-region") == journal["old_files"], "old snapshot backup is damaged")
        require(self.file_hash(transaction / "old-current_version.json") == journal["old_metadata_sha256"],
                "old metadata backup is damaged")
        require(self.file_hash(transaction / "old-index") == journal["old_index_sha256"], "old Git index backup is damaged")

    def move(self, source, target, checkpoint):
        self.checkpoint("before-" + checkpoint)
        os.replace(source, target)
        sync_directory(source.parent)
        sync_directory(target.parent)
        self.checkpoint(checkpoint)

    def install_pair(self, transaction, journal, prefix, source, files, metadata):
        region = self.repository / journal["region"]
        if files is None:
            if region.exists():
                self.move(region, transaction / ("withdrawn-region-" + uuid.uuid4().hex), prefix + "-live-moved")
        else:
            prepared = transaction / ("restore-region-" + uuid.uuid4().hex)
            self.copy_tree(transaction / source, prepared, files, prefix + "-file-written")
            if region.exists():
                self.move(region, transaction / ("withdrawn-region-" + uuid.uuid4().hex), prefix + "-live-moved")
            self.move(prepared, region, prefix + "-region-installed")
        target = self.repository / "current_version.json"
        if metadata is None:
            if target.exists():
                self.move(target, transaction / ("withdrawn-metadata-" + uuid.uuid4().hex), prefix + "-metadata-installed")
        else:
            raw = (transaction / metadata).read_bytes()
            prepared = transaction / ("restore-metadata-" + uuid.uuid4().hex)
            self.write(prepared, raw)
            self.move(prepared, target, prefix + "-metadata-installed")

    def index_matches_owned_stage(self, journal):
        changes = self.git("diff", "--cached", "--name-only").decode().splitlines()
        if any(path != "current_version.json" and not path.startswith(journal["region"] + "/") for path in changes):
            return False
        entries = {}
        for row in self.git("ls-files", "--stage", "-z", "--", journal["region"], "current_version.json").split(b"\0"):
            if row:
                details, path = row.split(b"\t", 1)
                mode, oid, stage = details.split()
                if mode != b"100644" or stage != b"0":
                    return False
                entries[path.decode()] = oid.decode()
        return entries == journal["new_git_blobs"]

    def restore_index(self, transaction, journal):
        index = self.repository / journal["index_path"]
        lock = index.with_name(index.name + ".lock")
        require(not lock.exists(), "Git index.lock blocks recovery; backups and journal retained")
        current = self.file_hash(index)
        require(current == journal["old_index_sha256"]
                or (journal.get("git_started") and self.index_matches_owned_stage(journal)),
                "Git index changed outside the transaction; backups and journal retained")
        if current == journal["old_index_sha256"]:
            return
        raw = (transaction / "old-index").read_bytes()
        require(digest(raw) == journal["old_index_sha256"], "Git index backup is damaged")
        # Git's standard exclusive index lock prevents a simultaneous writer.
        # An interrupted Git/restore writer may leave this lock; recovery then
        # refuses it instead of deleting a lock with uncertain ownership.
        created = False
        try:
            with lock.open("xb") as stream:
                created = True
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(lock, index)
            sync_directory(index.parent)
            self.checkpoint("rollback-index-installed")
        except Exception:
            if created and lock.exists():
                lock.unlink()
                sync_directory(lock.parent)
            raise

    def recover_active(self):
        active = self.active()
        if active is None:
            return 0
        transaction, journal = active
        if journal["state"] in {"preparing", "prepared", "rolled_back"}:
            # These states promise no live/index mutation, even if backup
            # preparation was interrupted before any backup existed.
            self.finish(transaction, journal, "rolled_back")
            return 0
        if self.is_committed(journal):
            require(self.tree_hashes(transaction / "incoming") == journal["new_files"], "new snapshot archive is damaged")
            require(self.file_hash(transaction / "new-current_version.json") == journal["new_metadata_sha256"],
                    "new metadata archive is damaged")
            live_matches = (self.tree_hashes(self.repository / journal["region"]) == journal["new_files"]
                            and self.file_hash(self.repository / "current_version.json") == journal["new_metadata_sha256"])
            if not live_matches:
                self.install_pair(transaction, journal, "committed-recovery", "incoming",
                                  journal["new_files"], "new-current_version.json")
            self.finish(transaction, journal, "committed")
            return 1
        self.verify_backups(transaction, journal)
        journal["state"] = "rolling_back"
        self.save(transaction, journal)
        self.install_pair(transaction, journal, "rollback", "old-region", journal["old_files"],
                          "old-current_version.json" if journal["old_metadata_sha256"] is not None else None)
        self.restore_index(transaction, journal)
        self.finish(transaction, journal, "rolled_back")
        return 0

    def recover(self):
        with self.locked():
            recovered = self.recover_active()
            if recovered:
                return 1
            # Covers interruption after finalize but before the shell recorded
            # the commit. A repeat push of the same owned HEAD is harmless.
            head = self.head()
            for transaction in self.state.iterdir():
                journal_path = transaction / "journal.json"
                if transaction.is_dir() and journal_path.is_file():
                    journal = load_json(journal_path)
                    if journal.get("state") == "committed" and journal.get("committed_head") == head:
                        require(self.is_committed(journal), "committed transaction no longer matches HEAD")
                        return 1
            return 0

    def publish(self, region, metadata, directory):
        with self.locked():
            recovered = self.recover_active()
            validate_snapshot(region, metadata, directory)
            index = self.preflight(region)
            live = self.repository / region
            version_path = self.repository / "current_version.json"
            old_metadata = version_path.read_bytes() if version_path.exists() else None
            versions = load_json(version_path) if old_metadata is not None else {"schema_version": 1, "regions": {}}
            require(isinstance(versions, dict) and versions.get("schema_version") == 1
                    and isinstance(versions.get("regions"), dict), "invalid existing current_version.json")
            old_files = self.tree_hashes(live)
            if old_files is not None:
                previous = versions["regions"].get(region)
                validate_snapshot(region, {"path": "/offline/master/", "entry": previous, "files": old_files}, live)
            else:
                require(region not in versions["regions"], "metadata names a missing old snapshot")
            previous = dict(versions["regions"].get(region, {}))
            entry = dict(metadata["entry"])
            previous.pop("verified_at", None)
            comparable = dict(entry)
            comparable.pop("verified_at", None)
            if old_files == metadata["files"] and previous == comparable:
                return "committed" if recovered else "unchanged"
            versions["regions"][region] = entry
            new_metadata = (json.dumps(versions, ensure_ascii=False, indent=2) + "\n").encode()
            identity = uuid.uuid4().hex
            transaction = self.state / identity
            transaction.mkdir()
            journal = {"schema_version": 1, "id": identity, "region": region, "state": "preparing",
                       "base_head": self.head(), "index_path": index.relative_to(self.repository).as_posix(),
                       "old_files": old_files, "old_metadata_sha256": digest(old_metadata) if old_metadata is not None else None,
                       "old_index_sha256": self.file_hash(index), "new_files": metadata["files"],
                       "new_metadata_sha256": digest(new_metadata), "git_started": False, "new_git_blobs": {}}
            self.save(transaction, journal)
            self.write(self.state / "active.json", (json.dumps({"transaction": identity}) + "\n").encode())
            try:
                self.checkpoint("preparing-journal")
                if old_files is not None:
                    self.copy_tree(live, transaction / "old-region", old_files, "backup-file-written")
                if old_metadata is not None:
                    self.write(transaction / "old-current_version.json", old_metadata)
                if journal["old_index_sha256"] is not None:
                    self.write(transaction / "old-index", index.read_bytes())
                self.verify_backups(transaction, journal)
                self.checkpoint("backups-written")
                self.copy_tree(Path(directory), transaction / "incoming", metadata["files"], "incoming-file-written")
                validate_snapshot(region, metadata, transaction / "incoming")
                self.copy_tree(transaction / "incoming", transaction / "install-region", metadata["files"], "install-file-written")
                self.write(transaction / "new-current_version.json", new_metadata)
                self.write(transaction / "install-current_version.json", new_metadata)
                self.checkpoint("metadata-prepared")
                for name in metadata["files"]:
                    journal["new_git_blobs"][region + "/" + name] = self.git_blob(region + "/" + name,
                                                                                  (transaction / "incoming" / name).read_bytes())
                journal["new_git_blobs"]["current_version.json"] = self.git_blob("current_version.json", new_metadata)
                require(self.head() == journal["base_head"] and self.file_hash(index) == journal["old_index_sha256"],
                        "Git changed during preparation")
                require(self.tree_hashes(live) == old_files and self.file_hash(version_path) == journal["old_metadata_sha256"],
                        "old snapshot changed during preparation")
                require(not index.with_name(index.name + ".lock").exists(), "Git index.lock exists")
                self.verify_backups(transaction, journal)
                journal["state"] = "installing"
                self.save(transaction, journal)
                self.checkpoint("installing-journal")
                if live.exists():
                    self.move(live, transaction / "displaced-old-region", "old-region-moved")
                self.move(transaction / "install-region", live, "region-installed")
                self.move(transaction / "install-current_version.json", version_path, "metadata-installed")
                journal["state"] = "installed"
                self.save(transaction, journal)
                self.checkpoint("installed-journal")
                require(self.head() == journal["base_head"] and self.file_hash(index) == journal["old_index_sha256"],
                        "Git changed before staging")
                require(not index.with_name(index.name + ".lock").exists(), "Git index.lock exists")
                journal["state"] = "staging"
                journal["git_started"] = True
                self.save(transaction, journal)
                self.checkpoint("before-stage")
                self.git("add", "-A", "--", region, "current_version.json")
                self.checkpoint("staged")
                require(self.index_matches_owned_stage(journal), "unexpected staged contents")
                journal["state"] = "committing"
                self.save(transaction, journal)
                self.checkpoint("before-commit")
                self.git("commit", "-q", "-m", region + ":" + entry["resource_version"],
                         "-m", "master: " + entry["version"], "-m", "MoeNotes-Transaction: " + identity)
                self.checkpoint("committed")
                require(self.is_committed(journal), "commit did not advance HEAD")
                self.finish(transaction, journal, "committed")
                return "committed"
            except Exception as error:
                try:
                    committed = self.recover_active()
                except Exception as recovery_error:
                    raise SnapshotError("update failed and recovery remains pending; backups retained: "
                                        + str(recovery_error)) from error
                if committed:
                    return "committed"
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=".", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("recover", help="recover pending work; print 0 or 1 owned commits to publish")
    publish = commands.add_parser("publish", help="validate, replace and commit one region locally")
    publish.add_argument("region")
    publish.add_argument("metadata", type=Path)
    publish.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        updater = SnapshotUpdater(arguments.repository)
        if arguments.command == "recover":
            print(updater.recover())
        else:
            print(updater.publish(arguments.region, load_json(arguments.metadata), arguments.directory))
    except (SnapshotError, OSError) as error:
        print(f"snapshot update failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
