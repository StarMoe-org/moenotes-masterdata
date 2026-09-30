import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
from update_snapshot import SnapshotUpdater
from validate_snapshot import SnapshotError

PRECOMMIT_CHECKPOINTS = (
    "preparing-journal", "backup-file-written", "backups-written", "incoming-file-written",
    "install-file-written", "metadata-prepared", "installing-journal", "old-region-moved",
    "region-installed", "metadata-installed", "installed-journal", "before-stage", "staged", "before-commit",
)
POSTCOMMIT_CHECKPOINTS = ("committed", "final-journal", "finalized")
ROLLBACK_CHECKPOINTS = ("rollback-file-written", "rollback-live-moved", "rollback-region-installed",
                        "rollback-metadata-installed", "rollback-index-installed", "final-journal")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.repository = root / "repository"
        self.incoming = root / "incoming"
        self.repository.mkdir(parents=True)
        self.incoming.mkdir()
        hooks = root / "empty-hooks"
        hooks.mkdir()
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        for key, value in (("core.autocrlf", "false"), ("core.hooksPath", hooks.as_posix()),
                           ("user.name", "Offline Test"), ("user.email", "offline@example.invalid")):
            self.git("config", key, value)
        self.old_entry, self.old_files = self.snapshot(self.repository / "en", "old-version", 1)
        self.new_entry, self.new_files = self.snapshot(self.incoming, "new-version", 2)
        self.old_metadata = json_bytes({"schema_version": 1, "regions": {"en": self.old_entry},
                                        "other_metadata": {"preserve": True}})
        (self.repository / "current_version.json").write_bytes(self.old_metadata)
        (self.repository / "untouched.txt").write_bytes(b"unrelated old data\n")
        (self.repository / ".gitignore").write_bytes(b".moenotes-transactions/\n")
        self.git("add", "--", "en", "current_version.json", "untouched.txt", ".gitignore")
        self.git("commit", "-q", "-m", "baseline fixture")
        self.old_head = self.git("rev-parse", "HEAD").decode().strip()
        self.old_index = (self.repository / ".git" / "index").read_bytes()
        self.metadata = {"path": "/en/master/", "entry": self.new_entry, "files": self.new_files}
        self.metadata_path = root / "incoming-metadata.json"
        self.metadata_path.write_bytes(json_bytes(self.metadata))
        self.old_live = self.live()

    def git(self, *arguments):
        result = subprocess.run(["git", *arguments], cwd=self.repository, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        return result.stdout

    def snapshot(self, directory, version, record_id):
        directory.mkdir(exist_ok=True)
        manifest = {"version": version, "files": [{"name": "MasterExample.bin", "hash": "a" * 64, "size": 32}]}
        (directory / "MasterManifest.json").write_bytes(json_bytes(manifest))
        (directory / "MasterExample.json").write_bytes(json_bytes({"_allData": [{"_id": record_id}]}))
        files = {path.name: sha(path.read_bytes()) for path in directory.iterdir()}
        entry = {"version": version, "resource_version": "1.0.0.1", "verified_at": "2026-09-30T00:00:00+00:00",
                 "data_path": "en", "snapshot_path": "en", "table_count": 1, "record_count": 1,
                 "manifest_sha256": files["MasterManifest.json"]}
        return entry, files

    def live(self):
        region = self.repository / "en"
        files = {path.name: path.read_bytes() for path in region.iterdir()} if region.exists() else None
        metadata = self.repository / "current_version.json"
        return files, metadata.read_bytes() if metadata.exists() else None

    def updater(self, checkpoint=None):
        return SnapshotUpdater(self.repository, checkpoint)

    def publish(self, updater=None):
        return (updater or self.updater()).publish("en", self.metadata, self.incoming)

    def active(self):
        path = self.repository / ".moenotes-transactions" / "active.json"
        if not path.exists():
            return None
        return path.parent / json.loads(path.read_bytes())["transaction"]

    def archives(self):
        directory = self.repository / ".moenotes-transactions"
        return [path for path in directory.iterdir() if path.is_dir()] if directory.exists() else []


class SnapshotTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="moenotes-masterdata-txn-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.fixture = Fixture(self.root / "default")

    def assert_old(self, fixture=None):
        fixture = fixture or self.fixture
        self.assertEqual(fixture.live(), fixture.old_live)
        self.assertEqual(fixture.git("rev-parse", "HEAD").decode().strip(), fixture.old_head)
        self.assertEqual((fixture.repository / ".git" / "index").read_bytes(), fixture.old_index)
        self.assertFalse(list(fixture.repository.glob("*.tmp-*")))

    def assert_backup(self, fixture=None):
        fixture = fixture or self.fixture
        archive = fixture.archives()[-1]
        self.assertEqual({path.name: path.read_bytes() for path in (archive / "old-region").iterdir()},
                         fixture.old_live[0])
        self.assertEqual((archive / "old-current_version.json").read_bytes(), fixture.old_metadata)
        self.assertEqual((archive / "old-index").read_bytes(), fixture.old_index)
        return archive

    def interrupt(self, fixture, checkpoint, rollback=False):
        program = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from update_snapshot import SnapshotUpdater
from validate_snapshot import load_json
point=sys.argv[5]
def checkpoint(name):
    if sys.argv[6] == "rollback" and name == "before-commit":
        raise OSError("injected commit failure")
    if name == point:
        os._exit(77)
SnapshotUpdater(sys.argv[2], checkpoint).publish("en", load_json(sys.argv[3]), Path(sys.argv[4]))
'''
        result = subprocess.run([sys.executable, "-B", "-c", program, str(SOURCE_ROOT),
                                 str(fixture.repository), str(fixture.metadata_path), str(fixture.incoming),
                                 checkpoint, "rollback" if rollback else "publish"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)

    def test_success_retains_verified_old_backups_and_commits_only_snapshot(self):
        self.assertEqual(self.fixture.publish(), "committed")
        self.assert_backup()
        self.assertIsNone(self.fixture.active())
        current = json.loads(self.fixture.live()[1])
        self.assertEqual(current["regions"]["en"], self.fixture.new_entry)
        self.assertEqual(current["other_metadata"], {"preserve": True})
        self.assertEqual(self.fixture.live()[0], {path.name: path.read_bytes() for path in self.fixture.incoming.iterdir()})
        committed = self.fixture.git("show", "--pretty=", "--name-only", "HEAD").decode().splitlines()
        self.assertEqual(set(committed), {"en/MasterExample.json", "en/MasterManifest.json", "current_version.json"})
        self.assertFalse(self.fixture.git("ls-files", "--", ".moenotes-transactions").strip())
        self.assertEqual(self.fixture.updater().recover(), 1)

    def test_unchanged_snapshot_preserves_verification_time_and_creates_no_transaction(self):
        self.fixture.metadata = {"path": "/en/master/", "entry": dict(self.fixture.old_entry, verified_at="later"),
                                 "files": self.fixture.old_files}
        self.fixture.incoming = self.fixture.repository / "en"
        self.assertEqual(self.fixture.publish(), "unchanged")
        self.assert_old()
        self.assertEqual(self.fixture.archives(), [])

    def test_existing_staged_changes_are_rejected_and_preserved(self):
        (self.fixture.repository / "untouched.txt").write_bytes(b"user staged data\n")
        self.fixture.git("add", "--", "untouched.txt")
        index = (self.fixture.repository / ".git" / "index").read_bytes()
        with self.assertRaisesRegex(SnapshotError, "staged"):
            self.fixture.publish()
        self.assertEqual((self.fixture.repository / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.fixture.live(), self.fixture.old_live)
        self.assertIsNone(self.fixture.active())

    def test_target_uncommitted_and_untracked_changes_are_rejected_without_overwrite(self):
        for kind in ("uncommitted", "untracked"):
            fixture = Fixture(self.root / kind)
            target = fixture.repository / "en" / ("MasterExample.json" if kind == "uncommitted" else "user-note.json")
            target.write_bytes(b"user work\n")
            before = fixture.live()
            with self.subTest(kind=kind), self.assertRaisesRegex(SnapshotError, kind):
                fixture.publish()
            self.assertEqual(fixture.live(), before)
            self.assertIsNone(fixture.active())

    def test_existing_index_lock_is_rejected_without_deleting_it(self):
        lock = self.fixture.repository / ".git" / "index.lock"
        lock.write_bytes(b"another writer")
        with self.assertRaisesRegex(SnapshotError, "index.lock"):
            self.fixture.publish()
        self.assertEqual(lock.read_bytes(), b"another writer")
        self.assert_old()

    def test_input_partial_write_failure_leaves_old_pair_and_no_scratch_files(self):
        original = Path.open
        injected = []
        class BrokenWrite:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *ignored):
                self.stream.close()
            def write(self, data):
                self.stream.write(data[:max(1, len(data) // 2)])
                raise OSError("injected partial input write")
        def broken_open(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            if "incoming" in path.parts and ".tmp-" in path.name and not injected:
                injected.append(True)
                return BrokenWrite(stream)
            return stream
        with mock.patch.object(Path, "open", new=broken_open), self.assertRaises(OSError):
            self.fixture.publish()
        self.assertTrue(injected)
        self.assert_old()
        self.assertIsNone(self.fixture.active())
        self.assertFalse(list((self.fixture.repository / ".moenotes-transactions").rglob("*.tmp-*")))

    def test_failed_journal_write_before_mutation_preserves_old_pair(self):
        updater = self.fixture.updater()
        original = updater.write
        def fail(path, raw):
            if path.name == "journal.json" and json.loads(raw).get("state") == "installed":
                raise OSError("injected installed journal write")
            return original(path, raw)
        updater.write = fail
        with self.assertRaises(OSError):
            self.fixture.publish(updater)
        self.assert_old()
        self.assert_backup()
        self.assertIsNone(self.fixture.active())

    def test_directory_and_metadata_move_failures_restore_old_pair(self):
        for point in ("before-old-region-moved", "before-region-installed", "before-metadata-installed"):
            fixture = Fixture(self.root / point)
            def checkpoint(name, target=point):
                if name == target:
                    raise OSError("injected move failure")
            with self.subTest(point=point), self.assertRaises(OSError):
                fixture.publish(fixture.updater(checkpoint))
            self.assert_old(fixture)
            self.assert_backup(fixture)
            self.assertIsNone(fixture.active())

    def test_missing_source_during_preparation_does_not_change_live_data(self):
        def checkpoint(name):
            if name == "backups-written":
                (self.fixture.incoming / "MasterExample.json").unlink()
        with self.assertRaises(OSError):
            self.fixture.publish(self.fixture.updater(checkpoint))
        self.assert_old()
        self.assertIsNone(self.fixture.active())

    def test_stage_and_commit_failures_restore_old_data_metadata_and_index(self):
        for operation in ("add", "commit"):
            fixture = Fixture(self.root / ("failed-" + operation))
            updater = fixture.updater()
            original = updater.git
            def failed(*arguments, raw=None, target=operation):
                if arguments[0] == target:
                    if target == "add":
                        original(*arguments, raw=raw)
                    raise OSError("injected Git failure")
                return original(*arguments, raw=raw)
            updater.git = failed
            with self.subTest(operation=operation), self.assertRaises(OSError):
                fixture.publish(updater)
            self.assert_old(fixture)
            self.assert_backup(fixture)
            self.assertIsNone(fixture.active())

    def test_commit_success_followed_by_command_error_is_completed_without_rollback(self):
        updater = self.fixture.updater()
        original = updater.git
        def uncertain(*arguments, raw=None):
            result = original(*arguments, raw=raw)
            if arguments[0] == "commit":
                raise OSError("command failed after commit advanced HEAD")
            return result
        updater.git = uncertain
        self.assertEqual(self.fixture.publish(updater), "committed")
        self.assert_backup()
        self.assertEqual(json.loads(self.fixture.live()[1])["regions"]["en"], self.fixture.new_entry)
        self.assertIsNone(self.fixture.active())

    def test_all_precommit_process_exit_checkpoints_recover_exact_old_pair_and_index(self):
        for point in PRECOMMIT_CHECKPOINTS:
            fixture = Fixture(self.root / ("exit-" + point))
            with self.subTest(point=point):
                self.interrupt(fixture, point)
                self.assertIsNotNone(fixture.active())
                self.assertEqual(fixture.updater().recover(), 0)
                self.assert_old(fixture)
                self.assertIsNone(fixture.active())
                if point not in ("preparing-journal", "backup-file-written"):
                    self.assert_backup(fixture)

    def test_postcommit_process_exit_checkpoints_finish_new_pair_without_reverting_head(self):
        for point in POSTCOMMIT_CHECKPOINTS:
            fixture = Fixture(self.root / ("exit-" + point))
            with self.subTest(point=point):
                self.interrupt(fixture, point)
                committed_head = fixture.git("rev-parse", "HEAD")
                self.assertNotEqual(committed_head.decode().strip(), fixture.old_head)
                self.assertEqual(fixture.updater().recover(), 1)
                self.assertEqual(fixture.git("rev-parse", "HEAD"), committed_head)
                self.assertEqual(json.loads(fixture.live()[1])["regions"]["en"], fixture.new_entry)
                self.assert_backup(fixture)
                self.assertIsNone(fixture.active())

    def test_rollback_process_exit_checkpoints_can_be_retried(self):
        for point in ROLLBACK_CHECKPOINTS:
            fixture = Fixture(self.root / ("exit-" + point))
            with self.subTest(point=point):
                self.interrupt(fixture, point, rollback=True)
                self.assert_backup(fixture)
                self.assertEqual(fixture.updater().recover(), 0)
                self.assert_old(fixture)
                self.assertIsNone(fixture.active())

    def test_rollback_failure_retains_journal_and_backups_then_recovers_on_retry(self):
        def checkpoint(name):
            if name in ("before-commit", "rollback-region-installed"):
                raise OSError("injected update/rollback failure")
        with self.assertRaisesRegex(SnapshotError, "recovery remains pending"):
            self.fixture.publish(self.fixture.updater(checkpoint))
        self.assertIsNotNone(self.fixture.active())
        self.assert_backup()
        self.assertEqual(self.fixture.updater().recover(), 0)
        self.assert_old()
        self.assertIsNone(self.fixture.active())

    def test_index_lock_during_recovery_is_preserved_and_recovery_can_retry(self):
        self.interrupt(self.fixture, "staged")
        lock = self.fixture.repository / ".git" / "index.lock"
        lock.write_bytes(b"unowned lock")
        with self.assertRaisesRegex(SnapshotError, "index.lock"):
            self.fixture.updater().recover()
        self.assertEqual(self.fixture.live(), self.fixture.old_live)
        self.assertEqual(lock.read_bytes(), b"unowned lock")
        self.assertIsNotNone(self.fixture.active())
        self.assert_backup()
        lock.unlink()  # Test withdraws its own injected lock, never another writer's lock.
        self.assertEqual(self.fixture.updater().recover(), 0)
        self.assert_old()

    def test_external_head_change_is_not_reset_and_old_backups_are_retained(self):
        def checkpoint(name):
            if name == "metadata-installed":
                self.fixture.git("commit", "--allow-empty", "-q", "-m", "external writer")
        with self.assertRaisesRegex(SnapshotError, "HEAD changed outside"):
            self.fixture.publish(self.fixture.updater(checkpoint))
        changed_head = self.fixture.git("rev-parse", "HEAD")
        self.assertNotEqual(changed_head.decode().strip(), self.fixture.old_head)
        self.assertIsNotNone(self.fixture.active())
        self.assert_backup()
        with self.assertRaisesRegex(SnapshotError, "HEAD changed outside"):
            self.fixture.updater().recover()
        self.assertEqual(self.fixture.git("rev-parse", "HEAD"), changed_head)

    def test_external_index_change_is_preserved_with_old_pair_and_pending_recovery(self):
        changed_index = []
        def checkpoint(name):
            if name == "metadata-installed":
                (self.fixture.repository / "untouched.txt").write_bytes(b"user staged work\n")
                self.fixture.git("add", "--", "untouched.txt")
                changed_index.append((self.fixture.repository / ".git" / "index").read_bytes())
        with self.assertRaisesRegex(SnapshotError, "index changed outside"):
            self.fixture.publish(self.fixture.updater(checkpoint))
        self.assertEqual(self.fixture.live(), self.fixture.old_live)
        self.assertEqual((self.fixture.repository / ".git" / "index").read_bytes(), changed_index[0])
        self.assertIsNotNone(self.fixture.active())
        self.assert_backup()
        # The test's external writer withdraws only its own simulated staging.
        (self.fixture.repository / ".git" / "index").write_bytes(self.fixture.old_index)
        self.assertEqual(self.fixture.updater().recover(), 0)
        self.assert_old()

    def test_corrupt_journal_index_path_cannot_write_outside_repository(self):
        self.interrupt(self.fixture, "region-installed")
        transaction = self.fixture.active()
        journal_path = transaction / "journal.json"
        journal = json.loads(journal_path.read_bytes())
        outside = self.root / "outside-index"
        outside.write_bytes(b"untouched sentinel")
        for value in ("../outside-index", str(outside), "untouched.txt"):
            journal["index_path"] = value
            journal_path.write_bytes(json_bytes(journal))
            with self.subTest(index_path=value), self.assertRaisesRegex(SnapshotError, "index_path"):
                self.fixture.updater().recover()
            self.assertEqual(outside.read_bytes(), b"untouched sentinel")
            self.assertIsNotNone(self.fixture.active())

    def test_corrupt_backup_is_detected_before_recovery_changes_live_paths(self):
        self.interrupt(self.fixture, "region-installed")
        archive = self.fixture.active()
        backup = archive / "old-region" / "MasterExample.json"
        backup.write_bytes(b"damaged")
        before = self.fixture.live()
        with self.assertRaisesRegex(SnapshotError, "backup is damaged"):
            self.fixture.updater().recover()
        self.assertEqual(self.fixture.live(), before)
        self.assertIsNotNone(self.fixture.active())
        self.assertEqual({path.name: path.read_bytes() for path in (archive / "displaced-old-region").iterdir()},
                         self.fixture.old_live[0])
        backup.write_bytes(self.fixture.old_live[0]["MasterExample.json"])
        self.assertEqual(self.fixture.updater().recover(), 0)
        self.assert_old()

    def test_journal_contains_no_absolute_repository_or_input_paths(self):
        self.interrupt(self.fixture, "installed-journal")
        text = (self.fixture.active() / "journal.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.root.as_posix(), text)
        journal = json.loads(text)
        self.assertEqual(journal["index_path"], ".git/index")
        self.assertEqual(self.fixture.updater().recover(), 0)
        self.assert_old()


if __name__ == "__main__":
    unittest.main()
