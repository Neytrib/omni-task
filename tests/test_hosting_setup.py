"""Private setup files must stay private and survive repeated setup."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "configure_hosting", ROOT / "scripts/configure_hosting.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_private_files_share_key_and_existing_values_are_preserved(tmp_path):
    templates = tmp_path / "deploy/railway"
    templates.mkdir(parents=True)
    for service in module.SERVICES:
        (templates / f"{service}.env.example").write_text(
            f"BOT_API_KEY={module.SECRET_PLACEHOLDER}\nPROVIDER_KEY=\n"
        )
    (tmp_path / ".env").write_text("PROVIDER_KEY=must-not-copy\n")
    paths = module.prepare(tmp_path)
    assert len({path.read_text().splitlines()[0] for path in paths}) == 1
    assert len(paths[0].read_text().splitlines()[0].split("=", 1)[1]) == 64
    assert all("must-not-copy" not in path.read_text() for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert paths[0].parent.stat().st_mode & 0o777 == 0o700
    paths[1].write_text(paths[1].read_text() + "USER_SETTING=keep\n")
    before = [path.read_bytes() for path in paths]
    assert module.prepare(tmp_path) == paths
    assert [path.read_bytes() for path in paths] == before


def test_partial_or_symlink_configuration_is_not_overwritten(tmp_path):
    private = tmp_path / "private/railway"
    private.mkdir(parents=True)
    api = private / "api.env"
    api.write_text("keep me")
    with pytest.raises(ValueError, match="Partial"):
        module.prepare(tmp_path)
    assert api.read_text() == "keep me"
    (private / "bot.env").symlink_to(api)
    with pytest.raises(ValueError, match="symlinks"):
        module.prepare(tmp_path)
