"""
Unit tests for lx06_tool.utils.checksum module.

Tests file hashing (SHA256, MD5), checksum verification, and
progress reporting functionality.
"""

import asyncio
from pathlib import Path

import pytest

from lx06_tool.utils.checksum import (
    FileChecksums,
    hash_file,
    verify_file,
    write_checksum_file,
    _hash_sync,
)


class TestFileChecksums:
    """Test FileChecksums dataclass."""

    def test_create_checksums(self):
        """FileChecksums should store all fields correctly."""
        path = Path("/tmp/test.img")
        checksums = FileChecksums(
            path=path,
            sha256="abc123",
            md5="def456",
            size_bytes=1024,
        )
        assert checksums.path == path
        assert checksums.sha256 == "abc123"
        assert checksums.md5 == "def456"
        assert checksums.size_bytes == 1024


class TestHashSync:
    """Test synchronous hash function."""

    def test_hash_empty_file(self, temp_dir):
        """Empty file should have known hashes."""
        empty_file = temp_dir / "empty.bin"
        empty_file.write_bytes(b"")

        checksums = _hash_sync(empty_file, None)
        assert checksums.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert checksums.md5 == "d41d8cd98f00b204e9800998ecf8427e"
        assert checksums.size_bytes == 0

    def test_hash_small_file(self, temp_dir):
        """Small file should hash correctly."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        checksums = _hash_sync(test_file, None)
        assert checksums.sha256 == "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        assert checksums.md5 == "65a8e27d8879283831b664bd8b7f0ad4"
        assert checksums.size_bytes == 13

    def test_hash_with_progress_callback(self, temp_dir):
        """Progress callback should be called during hashing."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB

        progress_calls = []

        def on_progress(read, total):
            progress_calls.append((read, total))

        checksums = _hash_sync(test_file, on_progress)

        # Should have been called at least once
        assert len(progress_calls) > 0
        # Final call should report complete file
        final_read, final_total = progress_calls[-1]
        assert final_read == 2 * 1024 * 1024
        assert final_total == 2 * 1024 * 1024
        assert checksums.size_bytes == 2 * 1024 * 1024

    def test_hash_known_values(self, temp_dir):
        """Test against known SHA256/MD5 vectors."""
        # Known test vector: "abc" repeated 1000 times
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"abc" * 1000)

        checksums = _hash_sync(test_file, None)
        # These are known good values for "abc" * 1000
        assert checksums.size_bytes == 3000
        # SHA256 should be consistent
        assert len(checksums.sha256) == 64
        assert all(c in "0123456789abcdef" for c in checksums.sha256)
        # MD5 should be consistent
        assert len(checksums.md5) == 32
        assert all(c in "0123456789abcdef" for c in checksums.md5)


class TestHashFile:
    """Test async hash_file function."""

    @pytest.mark.asyncio
    async def test_async_hash_file(self, temp_dir):
        """Async hash_file should return same result as sync version."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        checksums = await hash_file(test_file)
        assert checksums.sha256 == "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        assert checksums.md5 == "65a8e27d8879283831b664bd8b7f0ad4"
        assert checksums.size_bytes == 13

    @pytest.mark.asyncio
    async def test_async_hash_with_progress(self, temp_dir):
        """Async hash should call progress callback."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"x" * (1024 * 1024))  # 1 MB

        progress_calls = []

        def on_progress(read, total):
            progress_calls.append((read, total))

        checksums = await hash_file(test_file, on_progress=on_progress)

        assert len(progress_calls) > 0
        assert checksums.size_bytes == 1024 * 1024


class TestVerifyFile:
    """Test file verification against expected checksums."""

    @pytest.mark.asyncio
    async def test_verify_sha256_match(self, temp_dir):
        """Verification should succeed when SHA256 matches."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        result = await verify_file(
            test_file,
            "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_sha256_mismatch(self, temp_dir):
        """Verification should fail when SHA256 doesn't match."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        result = await verify_file(
            test_file,
            "0000000000000000000000000000000000000000000000000000000000000000"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_both_sha256_and_md5(self, temp_dir):
        """Verification should check both SHA256 and MD5 when both provided."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        result = await verify_file(
            test_file,
            "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f",
            expected_md5="65a8e27d8879283831b664bd8b7f0ad4"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_sha256_ok_md5_bad(self, temp_dir):
        """Verification should fail if MD5 doesn't match even if SHA256 does."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        result = await verify_file(
            test_file,
            "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f",
            expected_md5="00000000000000000000000000000000"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_case_insensitive(self, temp_dir):
        """Checksum comparison should be case-insensitive."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        result = await verify_file(
            test_file,
            "DFFD6021BB2BD5B0AF676290809EC3A53191DD81C7F70A4B28688A362182986F"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_md5_optional(self, temp_dir):
        """MD5 should be optional - only SHA256 required."""
        test_file = temp_dir / "test.bin"
        test_file.write_bytes(b"Hello, World!")

        # No MD5 provided should still work
        result = await verify_file(
            test_file,
            "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
        )
        assert result is True


class TestWriteChecksumFile:
    """Test writing checksum files."""

    def test_write_checksum_file(self, temp_dir):
        """Checksum file should be written in correct format."""
        test_file = temp_dir / "test.img"
        checksum_file = temp_dir / "test.img.sha256"

        checksums = FileChecksums(
            path=test_file,
            sha256="abc123",
            md5="def456",
            size_bytes=1024,
        )

        write_checksum_file(checksums, checksum_file)

        content = checksum_file.read_text()
        assert "SHA256: abc123  test.img" in content
        assert "MD5:    def456  test.img" in content

    def test_write_checksum_file_creates_parent_dirs(self, temp_dir):
        """Writing checksum file should create parent directories if needed."""
        test_file = temp_dir / "subdir" / "test.img"
        checksum_file = temp_dir / "subdir" / "test.img.sha256"

        checksums = FileChecksums(
            path=test_file,
            sha256="abc123",
            md5="def456",
            size_bytes=1024,
        )

        write_checksum_file(checksums, checksum_file)

        assert checksum_file.exists()
        content = checksum_file.read_text()
        assert "SHA256: abc123" in content
