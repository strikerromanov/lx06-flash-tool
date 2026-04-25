"""
Pytest configuration and fixtures for lx06-flash-tool tests.
"""

import sys
from pathlib import Path
import tempfile
import pytest  # noqa: F401

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture
def temp_dir():
    """Temporary directory fixture that cleans up after tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_backup_dir(temp_dir):
    """Create a sample backup directory structure."""
    backup_dir = temp_dir / "backups"
    backup_dir.mkdir()

    # Create sample partition files
    sample_files = {
        "mtd0_bootloader.img": 1024 * 1024,  # 1 MB
        "mtd1_tpl.img": 2 * 1024 * 1024,  # 2 MB
        "mtd2_boot0.img": 8 * 1024 * 1024,  # 8 MB
        "mtd3_boot1.img": 8 * 1024 * 1024,  # 8 MB
        "mtd4_system0.img": 32 * 1024 * 1024,  # 32 MB
        "mtd5_system1.img": 32 * 1024 * 1024,  # 32 MB
        "mtd6_data.img": 8 * 1024 * 1024,  # 8 MB
    }

    for filename, size in sample_files.items():
        file_path = backup_dir / filename
        file_path.write_bytes(b'\x00' * size)

    return backup_dir


@pytest.fixture
def sample_build_dir(temp_dir):
    """Create a sample build directory structure."""
    build_dir = temp_dir / "build"
    build_dir.mkdir()

    # Create subdirectories
    (build_dir / "extract").mkdir()
    (build_dir / "output").mkdir()
    (build_dir / "rootfs").mkdir()

    return build_dir
