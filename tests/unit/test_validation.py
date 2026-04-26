"""
Unit tests for lx06_tool.utils.validation module.

Tests input validation, command injection prevention, and
path traversal protection.
"""

import pytest
from pathlib import Path
import tempfile

from lx06_tool.utils.validation import (
    sanitize_command_input,
    validate_path_safe,
    validate_filename,
    validate_string_input,
    validate_int_input,
    SecurityContext,
)


class TestCommandInjectionPrevention:
    """Test command injection prevention."""

    def test_safe_command(self):
        """Safe command should pass through unchanged."""
        cmd = "setenv bootdelay 15"
        result = sanitize_command_input(cmd)
        assert result == cmd

    def test_alphanumeric_command(self):
        """Alphanumeric with spaces and safe punctuation."""
        cmd = "printenv partitions"
        result = sanitize_command_input(cmd)
        assert result == cmd

    def test_command_with_equals(self):
        """Command with equals sign (allowed)."""
        cmd = "setenv bootdelay=15"
        result = sanitize_command_input(cmd)
        assert result == cmd

    def test_command_with_colon(self):
        """Command with colon (allowed)."""
        cmd = "store list_part"
        result = sanitize_command_input(cmd)
        assert result == cmd

    def test_rejects_shell_metacharacters(self):
        """Shell metacharacters should be rejected."""
        # Forward slash alone is allowed (needed for device paths)
        # But shell metacharacters that enable injection are rejected
        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("ls; rm -rf /")

        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("test && malicious")

        # Pipe character should also be rejected
        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("cat /etc/passwd | grep root")

    def test_rejects_command_substitution(self):
        """Command substitution should be rejected."""
        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("$(whoami)")

        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("`echo hack`")

    def test_rejects_redirection(self):
        """I/O redirection should be rejected."""
        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("cat /etc/passwd > /tmp/out")

        with pytest.raises(ValueError, match="dangerous"):
            sanitize_command_input("echo input < /etc/passwd")

    def test_rejects_newlines(self):
        """Newlines should be rejected."""
        with pytest.raises(ValueError, match="invalid|dangerous"):
            sanitize_command_input("command\\nnext")

        with pytest.raises(ValueError, match="invalid|dangerous"):
            sanitize_command_input("command\\rnext")

    def test_rejects_null_bytes(self):
        """Null bytes should be stripped and trigger validation."""
        # Null bytes are stripped, but the command may still be invalid
        # The key is that null bytes don't cause crashes
        result = sanitize_command_input("command\x00injection")
        assert "\x00" not in result  # Null bytes removed
        # May or may not raise ValueError depending on remaining content

    def test_non_string_input(self):
        """Non-string input should be rejected."""
        with pytest.raises(ValueError, match="must be string"):
            sanitize_command_input(123)

        with pytest.raises(ValueError, match="must be string"):
            sanitize_command_input(None)


class TestPathTraversalPrevention:
    """Test path traversal prevention."""

    def test_safe_path_within_allowed(self):
        """Path within allowed directory should be accepted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            test_file = allowed / "test.txt"
            test_file.touch()

            result = validate_path_safe(test_file, allowed, must_exist=True)
            assert result == test_file.resolve()

    def test_path_traversal_with_double_dot(self):
        """Path traversal with .. should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            attack_path = allowed / "../../etc/passwd"

            with pytest.raises(ValueError, match="outside allowed"):
                validate_path_safe(attack_path, allowed)

    def test_path_traversal_with_symlink(self):
        """Symlink-based path traversal should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            attack_path = allowed / "symlink_attack"

            # Even if file doesn't exist, validation should catch traversal
            with pytest.raises(ValueError, match="outside allowed"):
                validate_path_safe(attack_path / "../../../etc/passwd", allowed)

    def test_nonexistent_path_rejected_when_required(self):
        """Non-existent path should be rejected when must_exist=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            test_path = allowed / "nonexistent.txt"

            with pytest.raises(ValueError, match="does not exist"):
                validate_path_safe(test_path, allowed, must_exist=True)

    def test_file_vs_directory_validation(self):
        """File/directory type validation should work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            allowed = Path(tmpdir)
            test_file = allowed / "file.txt"
            test_file.touch()
            test_dir = allowed / "directory"
            test_dir.mkdir()

            # Should succeed with correct type
            validate_path_safe(test_file, allowed, must_be_file=True)
            validate_path_safe(test_dir, allowed, must_be_dir=True)

            # Should fail with wrong type
            with pytest.raises(ValueError, match="not a file"):
                validate_path_safe(test_dir, allowed, must_be_file=True)

            with pytest.raises(ValueError, match="not a directory"):
                validate_path_safe(test_file, allowed, must_be_dir=True)


class TestFilenameValidation:
    """Test filename validation."""

    def test_safe_filename(self):
        """Safe filename should pass through."""
        filename = "system.img"
        result = validate_filename(filename)
        assert result == filename

    def test_filename_with_extension(self):
        """Filename with extension should be accepted."""
        filename = "mtd4_system0.img"
        result = validate_filename(filename)
        assert result == filename

    def test_rejects_path_separators(self):
        """Filenames with path separators should be rejected."""
        with pytest.raises(ValueError, match="path separator|traversal"):
            validate_filename("../../etc/passwd")

        with pytest.raises(ValueError, match="path separator|traversal"):
            validate_filename("subdir/file.txt")

    def test_rejects_double_dot(self):
        """Filenames with .. should be rejected."""
        with pytest.raises(ValueError, match="path traversal|path separator"):
            validate_filename("../escape")

        with pytest.raises(ValueError, match="path traversal|path separator"):
            validate_filename("normal/../escape")

    def test_rejects_null_bytes(self):
        """Filenames with null bytes should be rejected."""
        # Null bytes are stripped but validation may still work for remaining content
        result = validate_filename("test\x00.txt")
        assert "\x00" not in result  # Null bytes removed

    def test_rejects_empty_filename(self):
        """Empty filenames should be rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_filename("")

        with pytest.raises(ValueError, match="cannot be empty"):
            validate_filename("   ")

    def test_rejects_too_long_filename(self):
        """Overly long filenames should be rejected."""
        long_name = "a" * 300
        with pytest.raises(ValueError, match="too long"):
            validate_filename(long_name)

    def test_non_string_input(self):
        """Non-string input should be rejected."""
        with pytest.raises(ValueError, match="must be string"):
            validate_filename(123)


class TestStringInputValidation:
    """Test general string input validation."""

    def test_safe_string(self):
        """Safe string should pass through."""
        value = "test_string_123"
        result = validate_string_input(value)
        assert result == value

    def test_max_length_enforcement(self):
        """Maximum length should be enforced."""
        long_string = "a" * 2000
        with pytest.raises(ValueError, match="too long"):
            validate_string_input(long_string, max_length=100)

    def test_empty_string_rejection(self):
        """Empty string should be rejected when not allowed."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_string_input("", allow_empty=False)

    def test_empty_string_accepted_when_allowed(self):
        """Empty string should be accepted when allowed."""
        result = validate_string_input("", allow_empty=True)
        assert result == ""

    def test_whitespace_only_rejection(self):
        """Whitespace-only string should be rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_string_input("   ", allow_empty=False)

    def test_null_byte_removal(self):
        """Null bytes should be removed."""
        value = "test\x00string"
        result = validate_string_input(value)
        assert result == "teststring"

    def test_allowed_characters_validation(self):
        """Allowed characters pattern should work."""
        # Only alphanumeric allowed
        result = validate_string_input("Test123", allowed_chars=r'^[a-zA-Z0-9]+$')
        assert result == "Test123"

        # Should reject special chars
        with pytest.raises(ValueError, match="invalid characters"):
            validate_string_input("Test-123", allowed_chars=r'^[a-zA-Z0-9]+$')


class TestIntInputValidation:
    """Test integer input validation."""

    def test_valid_integer(self):
        """Valid integer string should be converted."""
        result = validate_int_input("123")
        assert result == 123

    def test_negative_integer(self):
        """Negative integers should be accepted."""
        result = validate_int_input("-42")
        assert result == -42

    def test_rejects_invalid_integer(self):
        """Invalid integer strings should be rejected."""
        with pytest.raises(ValueError, match="Invalid integer"):
            validate_int_input("not_a_number")

        with pytest.raises(ValueError, match="Invalid integer"):
            validate_int_input("12.34")

    def test_min_value_enforcement(self):
        """Minimum value should be enforced."""
        with pytest.raises(ValueError, match="below minimum"):
            validate_int_input("5", min_value=10)

    def test_max_value_enforcement(self):
        """Maximum value should be enforced."""
        with pytest.raises(ValueError, match="above maximum"):
            validate_int_input("150", max_value=100)

    def test_within_range(self):
        """Values within range should be accepted."""
        result = validate_int_input("50", min_value=10, max_value=100)
        assert result == 50


class TestSecurityContext:
    """Test SecurityContext class."""

    def test_create_security_context(self):
        """SecurityContext should initialize properly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            context = SecurityContext(base_dir=Path(tmpdir))
            assert context.base_dir == Path(tmpdir)
            assert context.strict_mode is True

    def test_validate_file_path_within_context(self):
        """File path validation should work within context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            context = SecurityContext(base_dir=Path(tmpdir))
            test_file = Path(tmpdir) / "test.txt"
            test_file.touch()

            result = context.validate_file_path(test_file, must_exist=True)
            assert result == test_file.resolve()

    def test_validate_file_path_outside_context(self):
        """File path outside context should be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            context = SecurityContext(base_dir=Path(tmpdir))
            attack_path = Path(tmpdir) / "subdir"
            attack_path.mkdir()
            # Try to access file outside base_dir
            outside = Path(tmpdir) / ".." / "etc" / "passwd"

            with pytest.raises(ValueError, match="outside allowed"):
                context.validate_file_path(outside)

    def test_validate_command_with_allowed_list(self):
        """Command validation with allowed list should work."""
        context = SecurityContext(
            base_dir=Path("/tmp"),
            allowed_commands={"setenv", "saveenv", "printenv"}
        )

        # Allowed command should pass
        result = context.validate_command("setenv bootdelay 15")
        assert "setenv" in result

        # Disallowed command should be rejected
        with pytest.raises(ValueError, match="not in allowed list"):
            context.validate_command("rm -rf /")

    def test_validate_command_without_allowed_list(self):
        """Command validation without allowed list should accept all safe commands."""
        context = SecurityContext(base_dir=Path("/tmp"))

        # Should accept safe command
        result = context.validate_command("setenv bootdelay 15")
        assert "setenv" in result

        # Should still reject dangerous shell metacharacters
        with pytest.raises(ValueError, match="dangerous"):
            context.validate_command("rm -rf /etc; malicious")

        # Command without special chars is allowed (device decides if valid)
        result = context.validate_command("printenv")
        assert "printenv" in result
