#!/usr/bin/env python3
"""Validate publisher snapshots without accessing the game or other services."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SUPPORTED_REGIONS = frozenset({"en", "hk-tw-mo", "jp", "kr"})
JSON_NAME = re.compile(r"[A-Za-z0-9_-]+\.json\Z")
BIN_NAME = re.compile(r"[A-Za-z0-9_-]+\.bin\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MASTER_PATH = re.compile(r"/[a-z]+/master/\Z")
MANIFEST_NAME = "MasterManifest.json"


class SnapshotError(ValueError):
    """The snapshot is incomplete, inconsistent, or lacks provenance."""


def require(condition, message):
    if not condition:
        raise SnapshotError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise SnapshotError(f"non-finite JSON number: {value}")


def parse_json(raw, label):
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except (ValueError, UnicodeError) as error:
        raise SnapshotError(f"{label}: {error}") from error


def load_json(path):
    return parse_json(Path(path).read_bytes(), str(path))


def valid_hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def validate_metadata(region, metadata):
    require(region in SUPPORTED_REGIONS, f"unsupported region: {region}")
    require(isinstance(metadata, dict), f"{region}: region metadata must be an object")
    path = metadata.get("path")
    require(isinstance(path, str) and MASTER_PATH.fullmatch(path),
            f"{region}: invalid masterdata path")
    entry = metadata.get("entry")
    require(isinstance(entry, dict), f"{region}: version entry must be an object")
    for key in ("version", "resource_version"):
        require(isinstance(entry.get(key), str) and entry[key].strip(),
                f"{region}: missing {key}")
    for key in ("data_path", "snapshot_path"):
        require(entry.get(key) == region, f"{region}: {key} must match the region")
    require(type(entry.get("table_count")) is int and entry["table_count"] > 0,
            f"{region}: invalid table_count")
    require(type(entry.get("record_count")) is int and entry["record_count"] >= 0,
            f"{region}: invalid record_count")
    require(valid_hash(entry.get("manifest_sha256")), f"{region}: invalid manifest_sha256")
    files = metadata.get("files")
    require(isinstance(files, dict) and files, f"{region}: missing file inventory")
    for name, checksum in files.items():
        require(isinstance(name, str) and JSON_NAME.fullmatch(name),
                f"{region}: invalid file name: {name}")
        require(valid_hash(checksum), f"{region}: invalid SHA256 for {name}")
    require(MANIFEST_NAME in files, f"{region}: missing {MANIFEST_NAME}")
    require(files[MANIFEST_NAME] == entry["manifest_sha256"],
            f"{region}: manifest hash does not match the version entry")
    require(len(files) == entry["table_count"] + 1,
            f"{region}: file inventory does not match table_count")
    return entry, files


def validate_index(index):
    require(isinstance(index, dict), "index must be an object")
    regions = index.get("regions")
    require(isinstance(regions, dict) and regions, "index must contain region entries")
    for region, metadata in regions.items():
        validate_metadata(region, metadata)
    return len(regions)


def validate_snapshot(region, metadata, directory):
    entry, files = validate_metadata(region, metadata)
    directory = Path(directory)
    require({path.name for path in directory.iterdir()} == set(files),
            f"{region}: downloaded file inventory differs from the index")
    decoded = {}
    for name, expected_hash in files.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), f"{region}: not a regular file: {name}")
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected_hash,
                f"{region}: SHA256 mismatch for {name}")
        decoded[name] = parse_json(raw, name)

    manifest = decoded[MANIFEST_NAME]
    require(isinstance(manifest, dict), f"{region}: manifest must be an object")
    require(manifest.get("version") == entry["version"], f"{region}: manifest version mismatch")
    binaries = manifest.get("files")
    require(isinstance(binaries, list), f"{region}: manifest files must be an array")
    table_names = set()
    for item in binaries:
        require(isinstance(item, dict), f"{region}: invalid manifest file entry")
        name = item.get("name")
        require(isinstance(name, str) and BIN_NAME.fullmatch(name),
                f"{region}: invalid manifest binary name")
        require(valid_hash(item.get("hash")), f"{region}: invalid binary hash for {name}")
        require(type(item.get("size")) is int and item["size"] >= 0,
                f"{region}: invalid binary size for {name}")
        table_name = name[:-4] + ".json"
        require(table_name not in table_names, f"{region}: duplicate manifest table: {table_name}")
        table_names.add(table_name)
    require(len(table_names) == entry["table_count"], f"{region}: manifest table_count mismatch")
    require(table_names == set(files) - {MANIFEST_NAME}, f"{region}: manifest/table inventory mismatch")

    record_count = 0
    for name in sorted(table_names):
        table = decoded[name]
        require(isinstance(table, dict) and set(table) == {"_allData"},
                f"{region}: invalid table envelope: {name}")
        rows = table["_allData"]
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
                f"{region}: invalid table records: {name}")
        record_count += len(rows)
    require(record_count == entry["record_count"], f"{region}: record_count mismatch")
    return len(table_names), record_count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="validate the publisher's region inventory")
    index.add_argument("path", type=Path)
    region = commands.add_parser("region", help="validate region metadata and optional downloaded files")
    region.add_argument("region")
    region.add_argument("metadata", type=Path)
    region.add_argument("--directory", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "index":
            count = validate_index(load_json(arguments.path))
            print(f"validated {count} region entries")
        else:
            metadata = load_json(arguments.metadata)
            if arguments.directory is None:
                validate_metadata(arguments.region, metadata)
            else:
                tables, records = validate_snapshot(arguments.region, metadata, arguments.directory)
                print(f"validated {arguments.region}: {tables} tables, {records} records")
    except (SnapshotError, OSError) as error:
        print(f"snapshot validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
