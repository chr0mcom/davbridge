"""hmac.key is only accepted as a regular, owner-only file of the effective user, opened without following symbolic links."""

import base64
import os
import secrets

import pytest

from davbridge.config import ConfigError, read_key


def write_key(path, mode=0o600, data=None):
    path.write_bytes(data if data is not None else base64.b64encode(secrets.token_bytes(32)) + b"\n")
    os.chmod(path, mode)
    return path


def test_a_private_regular_file_is_accepted(tmp_path):
    assert len(read_key(write_key(tmp_path / "hmac.key"))) == 32
    assert read_key(write_key(tmp_path / "raw.key", data=b"k" * 32)) == b"k" * 32


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666, 0o601])
def test_group_or_other_permission_bits_are_refused(tmp_path, mode):
    with pytest.raises(ConfigError, match="accessible to group or others"):
        read_key(write_key(tmp_path / "hmac.key", mode))


def test_a_symbolic_link_is_refused_even_to_a_good_file(tmp_path):
    real = write_key(tmp_path / "real.key")
    link = tmp_path / "hmac.key"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="symbolic link"):
        read_key(link)
    link.unlink()
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(ConfigError, match="symbolic link"):
        read_key(link)


def test_directories_and_pipes_are_refused_without_blocking(tmp_path):
    directory = tmp_path / "dir.key"
    directory.mkdir()
    with pytest.raises(ConfigError, match="not a regular file"):
        read_key(directory)
    pipe = tmp_path / "pipe.key"
    os.mkfifo(pipe, 0o600)
    with pytest.raises(ConfigError, match="not a regular file"):
        read_key(pipe)


def test_a_file_of_another_user_is_refused(tmp_path, monkeypatch):
    path = write_key(tmp_path / "hmac.key")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ConfigError, match="owned by uid"):
        read_key(path)


def test_missing_and_malformed_keys_keep_their_messages(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        read_key(tmp_path / "absent.key")
    with pytest.raises(ConfigError, match="32 bytes"):
        read_key(write_key(tmp_path / "short.key", data=b"short"))
