# Testing Guide for LX06-Flash-Tool

This guide covers testing the lx06-flash-tool at multiple levels, from safe smoke tests to full hardware integration tests.

## Level 1: Safe Smoke Tests (No Hardware Required)

These tests verify basic functionality without requiring actual hardware.

### 1.1 Application Startup Test

```bash
# Activate virtual environment
source venv/bin/activate

# Test help output
lx06-tool --help

# Expected: Should show usage information
```

### 1.2 Module Import Tests

```bash
# Test all core modules import correctly
python -c "
from lx06_tool import app, config, constants, exceptions, state
from lx06_tool.modules import backup, bootloader, firmware, flasher
from lx06_tool.ui.screens import backup, welcome, environment
print('✓ All modules import successfully')
"

# Expected: No errors, success message
```

### 1.3 Backup Report Generation Test

```bash
# Test the fixed generate_backup_report function
python -c "
from lx06_tool.config import BackupSet, PartitionBackup
from lx06_tool.modules.backup import generate_backup_report

backup = BackupSet(
    timestamp='2026-04-26_120000',
    partitions={
        'mtd0': PartitionBackup(
            name='mtd0',
            label='bootloader',
            size_bytes=1048576,
            expected_size=1048576,
            sha256='abc123' + '0' * 56,
            verified=True
        ),
        'mtd5': PartitionBackup(
            name='mtd5',
            label='system1',
            size_bytes=33554432,
            expected_size=33554432,
            sha256='def456' + '0' * 56,
            verified=True
        )
    },
    all_verified=True
)

report = generate_backup_report(backup)
print(report)
print('✓ Backup report generation works')
"

# Expected: Formatted report with all partitions listed
```

### 1.4 Partition Map Completeness Test

```bash
# Test that all 7 partitions are defined
python -c "
from lx06_tool.constants import PARTITION_MAP, AB_SYSTEM_SLOTS, AB_BOOT_SLOTS

print(f'Total partitions: {len(PARTITION_MAP)}')
assert len(PARTITION_MAP) == 7, 'Should have 7 partitions'

# Verify A/B slots exist
for slot in AB_BOOT_SLOTS:
    assert any(p['label'] == slot for p in PARTITION_MAP.values()), f'Missing {slot}'

for slot in AB_SYSTEM_SLOTS:
    assert any(p['label'] == slot for p in PARTITION_MAP.values()), f'Missing {slot}'

# Verify mtd5 (system1) exists
assert 'mtd5' in PARTITION_MAP, 'Missing mtd5 (system1)'
assert PARTITION_MAP['mtd5']['label'] == 'system1', 'mtd5 should be system1'

print('✓ All partitions correctly defined')
print('✓ A/B slot support verified')
"

# Expected: All assertions pass
```

### 1.5 Configuration Persistence Test

```bash
# Test config save/load
python -c "
from lx06_tool.config import AppConfig
from pathlib import Path
import tempfile

with tempfile.TemporaryDirectory() as tmpdir:
    cfg_path = Path(tmpdir) / 'test_config.yaml'

    # Create and save config
    cfg = AppConfig()
    cfg.use_docker_build = False
    cfg.proxy = 'http://test:8080'
    cfg.save(cfg_path)

    # Load and verify
    cfg2 = AppConfig.load(cfg_path)
    assert cfg2.use_docker_build == False
    assert cfg2.proxy == 'http://test:8080'

print('✓ Configuration persistence works')
"

# Expected: Config saves and loads correctly
```

## Level 2: UI Navigation Tests (No Hardware)

These tests start the TUI but don't perform dangerous operations.

### 2.1 Welcome Screen Test

```bash
# Start the app and navigate through welcome screens
source venv/bin/activate
lx06-tool

# Manual steps:
# 1. Should see welcome screen with safety info
# 2. Press Enter/Space to continue
# 3. Navigate through screens using arrow keys
# 4. Press 'q' to quit safely
```

### 2.2 Environment Check Test

```bash
# Run environment check only (no TUI)
lx06-tool --check

# Expected: Shows dependency status, USB detection, etc.
```

## Level 3: Hardware-Aware Tests (Requires Device)

⚠️ **WARNING: These tests require actual hardware. Be careful!**

### 3.1 USB Detection Test

```bash
# Start app and check if it can detect the device
lx06-tool

# Manual steps:
# 1. Plug in LX06 device via USB-A to USB-A cable
# 2. Put device in USB burning mode (hold button + plug in)
# 3. App should detect Amlogic USB device
# 4. Verify device info shows correct chip (AXG)
```

### 3.2 Backup Test (Safe)

```bash
# Test backup functionality
lx06-tool

# Manual steps:
# 1. Navigate to Backup screen
# 2. Click "Start Backup"
# 3. Enter sudo password
# 4. Should backup all 7 partitions
# 5. Verify checksums pass
# 6. Verify backup report shows all 7 partitions including mtd5 (system1)
```

### 3.3 Bootloader Unlock Test

```bash
# Test bootloader unlock
# (This is part of backup process)

# Expected:
# - bootdelay set to 15
# - Verification shows bootdelay >= 5
```

## Level 4: Unit Tests (Developer)

### 4.1 Run Unit Tests

```bash
# If tests exist
source venv/bin/activate
pytest tests/ -v

# Or run specific test modules
pytest tests/test_backup.py -v
pytest tests/test_config.py -v
```

### 4.2 Create Unit Tests

```bash
# Create test file for backup module
cat > tests/test_backup.py << 'EOF'
import pytest
from lx06_tool.config import BackupSet, PartitionBackup
from lx06_tool.modules.backup import generate_backup_report

def test_generate_backup_report():
    """Test backup report generation."""
    backup = BackupSet(
        timestamp='2026-04-26_120000',
        partitions={
            'mtd0': PartitionBackup(
                name='mtd0',
                label='bootloader',
                size_bytes=1048576,
                expected_size=1048576,
                sha256='abc123' + '0' * 56,
                verified=True
            )
        },
        all_verified=True
    )

    report = generate_backup_report(backup)

    assert 'BACKUP SUMMARY REPORT' in report
    assert 'mtd0' in report
    assert 'bootloader' in report
    assert '1.00' in report  # Size in MB
    assert 'YES' in report  # Verified status

def test_partition_map_completeness():
    """Test all 7 partitions are defined."""
    from lx06_tool.constants import PARTITION_MAP

    assert len(PARTITION_MAP) == 7
    assert 'mtd0' in PARTITION_MAP
    assert 'mtd5' in PARTITION_MAP
    assert 'mtd6' in PARTITION_MAP

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
EOF

# Run the tests
pytest tests/test_backup.py -v
```

## Level 5: Integration Tests (Advanced)

### 5.1 Full Workflow Test (Dry Run)

```bash
# Test complete workflow without actually flashing
lx06-tool --backup-only

# This will:
# 1. Check dependencies
# 2. Detect USB device
# 3. Backup all partitions
# 4. Skip flashing
```

### 5.2 Error Handling Tests

```bash
# Test error scenarios
python -c "
from lx06_tool.exceptions import (
    BackupIncompleteError,
    ChecksumMismatchError,
    PartitionDumpError,
)

# Test exception hierarchy
try:
    raise BackupIncompleteError('Test error')
except BackupIncompleteError as e:
    print(f'✓ Exception caught: {e}')

print('✓ Error handling works')
"
```

## Test Results Checklist

Run through this checklist to verify fixes:

- [ ] Application starts without errors
- [ ] Help text displays correctly
- [ ] All modules import successfully
- [ ] Backup report generates correctly
- [ ] All 7 partitions are defined (mtd0-mtd6)
- [ ] mtd5 (system1) is present
- [ ] A/B partition slots are correct
- [ ] Config saves and loads
- [ ] UI screens navigate correctly
- [ ] No ImportError messages
- [ ] No missing function errors

## Troubleshooting

### Import Errors

```bash
# If you get import errors, ensure package is installed
source venv/bin/activate
pip install -e .
```

### Permission Errors

```bash
# Ensure sudo password is set correctly
echo "Your sudo password"
# Or configure passwordless sudo for testing
```

### Module Not Found

```bash
# Ensure virtual environment is activated
source venv/bin/activate

# Verify PYTHONPATH
echo $PYTHONPATH

# Reinstall if needed
pip install --force-reinstall -e .
```

## Continuous Testing

For development, create a test script:

```bash
#!/bin/bash
# test.sh - Quick smoke test

set -e

echo "Running smoke tests..."

source venv/bin/activate

# Test 1: Imports
python -c "from lx06_tool.modules.backup import generate_backup_report"
echo "✓ Import test passed"

# Test 2: Function exists
python -c "print(generate_backup_report.__doc__)"
echo "✓ Function documentation exists"

# Test 3: Partition map
python -c "from lx06_tool.constants import PARTITION_MAP; assert len(PARTITION_MAP) == 7"
echo "✓ Partition map complete"

echo "All smoke tests passed!"
```

Make it executable:
```bash
chmod +x test.sh
./test.sh
```
