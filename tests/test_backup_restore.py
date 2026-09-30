"""Guardrails for operator tooling; actual restore is scripts/check_restore.py."""

import json
import stat

import pytest

from scripts import backup, restore


class PostgreSQL:
    database = "source_database"

    def __init__(self, *, failed=False, exists=False):
        self.failed, self.exists, self.calls = failed, exists, []

    def pg(self, command, *args, **kwargs):
        self.calls.append(command)
        if command == "pg_dump":
            kwargs["stdout"].write(b"synthetic private archive")
            if self.failed:
                raise RuntimeError("synthetic export failure")
        elif command == "psql":
            return b"1\n" if self.exists else b""
        return b""


def test_backup_publishes_complete_checksummed_private_bundle(tmp_path):
    destination = backup.backup(PostgreSQL(), tmp_path)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for path in destination.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    archive = restore.verify_bundle(destination)
    assert archive.read_bytes() == b"synthetic private archive"
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["database"] == "source_database"


def test_failed_dump_never_publishes_a_backup(tmp_path):
    with pytest.raises(RuntimeError):
        backup.backup(PostgreSQL(failed=True), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_corrupt_archive_is_refused_before_database_creation(tmp_path):
    database = PostgreSQL()
    destination = backup.backup(database, tmp_path)
    (destination / "database.dump").write_bytes(b"corrupted")
    database.calls.clear()
    with pytest.raises(ValueError):
        restore.restore(database, destination, "new_database")
    assert database.calls == []


@pytest.mark.parametrize("target", ["source_database", "postgres", "template0", "template1"])
def test_restore_refuses_configured_and_system_databases(tmp_path, target):
    database = PostgreSQL()
    with pytest.raises(ValueError):
        restore.restore(database, tmp_path, target)
    assert database.calls == []


def test_restore_refuses_existing_target_even_if_empty(tmp_path):
    database = PostgreSQL(exists=True)
    destination = backup.backup(database, tmp_path)
    database.calls.clear()
    with pytest.raises(ValueError):
        restore.restore(database, destination, "new_database")
    assert database.calls == ["psql"]


def test_restore_never_uses_clean_or_create_archive_commands(tmp_path):
    database = PostgreSQL()
    destination = backup.backup(database, tmp_path)
    database.calls.clear()
    restore.restore(database, destination, "new_database")
    assert database.calls == ["psql", "createdb", "pg_restore"]


@pytest.mark.parametrize(
    "value", ["x; DROP DATABASE postgres", "-options", "postgres/other", "X", "a" * 64]
)
def test_database_name_cannot_be_sql_or_connection_options(value):
    with pytest.raises(ValueError):
        backup.identifier(value)


def test_backup_rejects_public_directory_and_symlink(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        backup.backup(PostgreSQL(), public)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        backup.backup(PostgreSQL(), link)
