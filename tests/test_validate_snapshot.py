import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_snapshot import SnapshotError, parse_json, validate_index, validate_metadata, validate_snapshot


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="moenotes-masterdata-test-")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.directory = self.root / "en"
        self.directory.mkdir()
        self.manifest = {
            "version": "sample-v1",
            "files": [{"name": "MasterExample.bin", "hash": "a" * 64, "size": 32}],
        }
        self.metadata = {
            "path": "/en/master/",
            "entry": {
                "version": "sample-v1", "resource_version": "1.0.0.1",
                "snapshot_path": "en", "data_path": "en",
                "table_count": 1, "record_count": 2,
                "manifest_sha256": "",
            },
            "files": {},
        }
        self.write_json("MasterManifest.json", self.manifest)
        self.write_json("MasterExample.json", {"_allData": [{"_id": 1}, {"_id": 2}]})
        self.refresh_hashes()

    def write_json(self, name, value):
        (self.directory / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def refresh_hashes(self):
        self.metadata["files"] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.directory.iterdir()
        }
        self.metadata["entry"]["manifest_sha256"] = self.metadata["files"]["MasterManifest.json"]

    def assert_hash_gate_passes(self):
        # This is the old workflow's sole downloaded-data acceptance condition.
        for name, expected in self.metadata["files"].items():
            self.assertEqual(hashlib.sha256((self.directory / name).read_bytes()).hexdigest(), expected)

    def test_valid_snapshot_for_each_supported_region(self):
        for region in ("en", "hk-tw-mo", "jp", "kr"):
            with self.subTest(region=region):
                metadata = copy.deepcopy(self.metadata)
                metadata["entry"].update(data_path=region, snapshot_path=region)
                metadata["path"] = "/hk/master/" if region == "hk-tw-mo" else f"/{region}/master/"
                self.assertEqual(validate_snapshot(region, metadata, self.directory), (1, 2))
                self.assertEqual(validate_index({"regions": {region: metadata}}), 1)

    def test_valid_empty_table_is_preserved(self):
        self.write_json("MasterExample.json", {"_allData": []})
        self.metadata["entry"]["record_count"] = 0
        self.refresh_hashes()
        self.assertEqual(validate_snapshot("en", self.metadata, self.directory), (1, 0))

    def test_missing_or_empty_index_is_rejected(self):
        for index in (None, {}, {"regions": None}, {"regions": []}, {"regions": {}}):
            with self.subTest(index=index), self.assertRaises(SnapshotError):
                validate_index(index)

    def test_index_cannot_target_repository_code_directories(self):
        for region in ("scripts", "tests", "--", "../en"):
            with self.subTest(region=region), self.assertRaisesRegex(SnapshotError, "unsupported region"):
                validate_index({"regions": {region: self.metadata}})

    def test_hash_correct_snapshot_without_version_entry_is_rejected(self):
        self.metadata["entry"] = None
        self.assert_hash_gate_passes()
        with self.assertRaisesRegex(SnapshotError, "version entry"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_hash_correct_snapshot_with_other_region_provenance_is_rejected(self):
        for key in ("data_path", "snapshot_path"):
            metadata = copy.deepcopy(self.metadata)
            metadata["entry"][key] = "kr"
            self.assert_hash_gate_passes()
            with self.subTest(key=key), self.assertRaisesRegex(SnapshotError, "must match the region"):
                validate_snapshot("en", metadata, self.directory)

    def test_required_version_fields_and_counts_are_checked_before_download(self):
        cases = [("version", None), ("resource_version", ""), ("table_count", 0),
                 ("table_count", True), ("record_count", -1), ("record_count", 2.0)]
        for key, value in cases:
            metadata = copy.deepcopy(self.metadata)
            metadata["entry"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(SnapshotError):
                validate_metadata("en", metadata)

    def test_missing_manifest_and_empty_inventory_are_rejected(self):
        for files in ({}, {"MasterExample.json": "a" * 64}):
            metadata = copy.deepcopy(self.metadata)
            metadata["files"] = files
            with self.subTest(files=files), self.assertRaises(SnapshotError):
                validate_metadata("en", metadata)

    def test_manifest_hash_must_belong_to_the_version_entry(self):
        self.metadata["entry"]["manifest_sha256"] = "b" * 64
        self.assert_hash_gate_passes()
        with self.assertRaisesRegex(SnapshotError, "manifest hash does not match"):
            validate_metadata("en", self.metadata)

    def test_unsafe_file_names_and_invalid_checksums_are_rejected(self):
        cases = [("../MasterExample.json", "a" * 64), ("MasterExample.json", "not-a-hash")]
        for name, checksum in cases:
            metadata = copy.deepcopy(self.metadata)
            metadata["files"].pop("MasterExample.json")
            metadata["files"][name] = checksum
            with self.subTest(name=name, checksum=checksum), self.assertRaises(SnapshotError):
                validate_metadata("en", metadata)

    def test_missing_downloaded_table_is_rejected(self):
        (self.directory / "MasterExample.json").unlink()
        with self.assertRaisesRegex(SnapshotError, "downloaded file inventory"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_unexpected_downloaded_file_is_rejected(self):
        self.write_json("MasterUnexpected.json", {"_allData": []})
        with self.assertRaisesRegex(SnapshotError, "downloaded file inventory"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_corrupt_download_cannot_reuse_expected_hash(self):
        path = self.directory / "MasterExample.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(SnapshotError, "SHA256 mismatch"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_invalid_json_is_rejected_even_with_matching_transport_hash(self):
        (self.directory / "MasterExample.json").write_bytes(b'{"_allData": [')
        self.refresh_hashes()
        self.assert_hash_gate_passes()
        with self.assertRaises(SnapshotError):
            validate_snapshot("en", self.metadata, self.directory)

    def test_manifest_version_must_match_entry_even_with_matching_file_hashes(self):
        self.manifest["version"] = "other-version"
        self.write_json("MasterManifest.json", self.manifest)
        self.refresh_hashes()
        self.assert_hash_gate_passes()
        with self.assertRaisesRegex(SnapshotError, "manifest version mismatch"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_manifest_cannot_omit_a_table_with_correct_transport_hashes(self):
        self.manifest["files"] = []
        self.write_json("MasterManifest.json", self.manifest)
        self.refresh_hashes()
        self.assert_hash_gate_passes()
        with self.assertRaisesRegex(SnapshotError, "manifest table_count"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_manifest_table_names_must_match_downloads(self):
        self.manifest["files"][0]["name"] = "MasterOther.bin"
        self.write_json("MasterManifest.json", self.manifest)
        self.refresh_hashes()
        with self.assertRaisesRegex(SnapshotError, "manifest/table inventory"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_duplicate_manifest_table_is_rejected(self):
        self.manifest["files"].append(copy.deepcopy(self.manifest["files"][0]))
        self.write_json("MasterManifest.json", self.manifest)
        self.refresh_hashes()
        with self.assertRaisesRegex(SnapshotError, "duplicate manifest table"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_manifest_binary_metadata_is_checked(self):
        for key, value in (("name", "../MasterExample.bin"), ("hash", "bad"), ("size", True), ("size", -1)):
            manifest = copy.deepcopy(self.manifest)
            manifest["files"][0][key] = value
            self.write_json("MasterManifest.json", manifest)
            self.refresh_hashes()
            with self.subTest(key=key, value=value), self.assertRaises(SnapshotError):
                validate_snapshot("en", self.metadata, self.directory)

    def test_conversion_envelope_and_record_types_are_checked(self):
        for table in ([], {"rows": []}, {"_allData": {}}, {"_allData": [1]}):
            self.write_json("MasterExample.json", table)
            self.refresh_hashes()
            self.assert_hash_gate_passes()
            with self.subTest(table=table), self.assertRaises(SnapshotError):
                validate_snapshot("en", self.metadata, self.directory)

    def test_record_count_must_match_converted_rows(self):
        self.metadata["entry"]["record_count"] = 3
        self.assert_hash_gate_passes()
        with self.assertRaisesRegex(SnapshotError, "record_count mismatch"):
            validate_snapshot("en", self.metadata, self.directory)

    def test_json_cannot_silently_drop_duplicate_keys_or_accept_nonfinite_numbers(self):
        for raw in (b'{"_allData": [], "_allData": []}', b'{"_allData": [{"value": NaN}]}'):
            with self.subTest(raw=raw), self.assertRaises(SnapshotError):
                parse_json(raw, "table")

    def test_validation_can_recover_after_a_failed_download_is_replaced(self):
        self.write_json("MasterExample.json", {"_allData": [1]})
        self.refresh_hashes()
        with self.assertRaises(SnapshotError):
            validate_snapshot("en", self.metadata, self.directory)
        self.write_json("MasterExample.json", {"_allData": [{"_id": 1}, {"_id": 2}]})
        self.refresh_hashes()
        self.assertEqual(validate_snapshot("en", self.metadata, self.directory), (1, 2))

    def test_failed_workflow_validation_preserves_old_files_and_never_commits_or_pushes(self):
        if sys.platform == "win32":
            bash = Path(r"C:\Program Files\Git\bin\bash.exe")
            if not bash.is_file():
                self.skipTest("requires Git Bash for the workflow failure boundary test")
            bash = str(bash)
        else:
            bash = shutil.which("bash")
            if bash is None:
                self.skipTest("requires Bash for the workflow failure boundary test")
        repo = Path(__file__).resolve().parents[1]
        workflow = (repo / ".github" / "workflows" / "pull-masterdata.yml").read_text(encoding="utf-8")
        start = workflow.index("          for region in $(jq -r ")
        end = workflow.index('          echo "$commits region commit(s)"')
        publication = textwrap.dedent(workflow[start:end])
        # Run the actual retry/publication loop. The region enumerator, download
        # transport and git are mocks; snapshot validation and Bash are real.
        # No network or real repository mutation is allowed in this test.
        preamble = r'''
set -euo pipefail
metadata="$1"
snapshot="$2"
mutation_log="$3"
validation_python="$4"
validation_script="$5"
work="$6"
commits=0
jq() {
  if [ "$#" -eq 3 ] && [ "$1" = "-r" ] && [ "$2" = '.regions | keys_unsorted[]' ]; then
    printf 'en\n'
  else
    echo 'unexpected jq call after rejected snapshot' >&2
    return 99
  fi
}
sleep() { :; }
git() { printf '%s\n' "$*" >> "$mutation_log"; }
download() {
  "$validation_python" "$validation_script" region "$1" "$metadata" --directory "$snapshot"
}
'''
        original = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        metadata = copy.deepcopy(self.metadata)
        cases = ("null_entry", "wrong_region", "record_count", "missing_input",
                 "bad_hash", "invalid_json", "manifest_version")
        for case in cases:
            for name, raw in original.items():
                (self.directory / name).write_bytes(raw)
            self.metadata = copy.deepcopy(metadata)
            if case == "null_entry":
                self.metadata["entry"] = None
            elif case == "wrong_region":
                self.metadata["entry"]["data_path"] = "kr"
            elif case == "record_count":
                self.metadata["entry"]["record_count"] = 3
            elif case == "missing_input":
                (self.directory / "MasterExample.json").unlink()
            elif case == "bad_hash":
                (self.directory / "MasterExample.json").write_bytes(b"corrupt")
            elif case == "invalid_json":
                (self.directory / "MasterExample.json").write_bytes(b'{"_allData": [')
                self.refresh_hashes()
            elif case == "manifest_version":
                self.write_json("MasterManifest.json", dict(self.manifest, version="other-version"))
                self.refresh_hashes()
            with self.subTest(case=case):
                live = self.root / ("publication-" + case)
                old_region = live / "en"
                old_region.mkdir(parents=True)
                (old_region / "old.json").write_bytes(b"old usable data\n")
                (live / "current_version.json").write_bytes(b'{"old": "version"}\n')
                old_bytes = {path.relative_to(live): path.read_bytes()
                             for path in live.rglob("*") if path.is_file()}
                metadata_path = self.root / (case + ".json")
                metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
                mutations = self.root / (case + "-mutations.log")
                # The loop can only target the synthetic en directory below
                # this verified temporary working directory.
                self.assertEqual(live.resolve().parent, self.root.resolve())
                self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
                result = subprocess.run([
                    bash, "--noprofile", "--norc", "-c", preamble + publication,
                    "offline-publication-test", metadata_path.as_posix(),
                    self.directory.as_posix(), mutations.as_posix(), sys.executable.replace("\\", "/"),
                    (repo / "scripts" / "validate_snapshot.py").as_posix(), self.root.as_posix(),
                ], cwd=live, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no consistent en snapshot", result.stdout)
                self.assertFalse(mutations.exists(), "rejected input must not reach git mutations")
                self.assertEqual({path.relative_to(live): path.read_bytes()
                                  for path in live.rglob("*") if path.is_file()}, old_bytes)

    def test_cli_failure_returns_nonzero_without_changing_inputs(self):
        self.metadata["entry"] = None
        metadata_path = self.root / "metadata.json"
        metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        script = Path(__file__).resolve().parents[1] / "scripts" / "validate_snapshot.py"
        result = subprocess.run([sys.executable, str(script), "region", "en", str(metadata_path),
                                 "--directory", str(self.directory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("version entry", result.stderr)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
