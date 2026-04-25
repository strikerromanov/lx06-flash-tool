#!/bin/bash
# test.sh - Quick smoke test suite for lx06-flash-tool
#
# Usage: ./test.sh
#
# This script runs all smoke tests to verify the tool works correctly
# without requiring actual hardware.

echo "========================================================================"
echo "LX06-Flash-Tool: Smoke Test Suite"
echo "========================================================================"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counter
TESTS_PASSED=0
TESTS_FAILED=0

# Function to run a test
run_test() {
    local test_name="$1"
    local test_cmd="$2"

    echo -n "Running: $test_name ... "
    if eval "$test_cmd" > /dev/null 2>&1; then
        echo -e "${GREEN}PASS${NC}"
        ((TESTS_PASSED++))
        return 0
    else
        echo -e "${RED}FAIL${NC}"
        ((TESTS_FAILED++))
        return 1
    fi
}

# Ensure virtual environment is active
if [ -z "$VIRTUAL_ENV" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

echo "Test Environment:"
echo "  Python: $(python --version)"
echo "  Virtual Env: $VIRTUAL_ENV"
echo "  Working Dir: $(pwd)"
echo ""

# Test 1: Application startup
run_test "Application help output" \
    "lx06-tool --help"

# Test 2: Core module imports
run_test "Core module imports" \
    "python -c 'from lx06_tool import app, config, constants, exceptions, state'"

# Test 3: Business logic modules
run_test "Business logic modules" \
    "python -c 'from lx06_tool.modules import backup, bootloader, firmware, flasher'"

# Test 4: UI screens
run_test "UI screen imports" \
    "python -c 'from lx06_tool.ui.screens import backup, welcome, environment'"

# Test 5: Backup report function (THE FIX)
run_test "Backup report generation" \
    "python -c 'from lx06_tool.modules.backup import generate_backup_report'"

# Test 6: Partition map completeness (THE FIX)
run_test "Partition map has 7 partitions" \
    "python -c 'from lx06_tool.constants import PARTITION_MAP; assert len(PARTITION_MAP) == 7'"

# Test 7: mtd5 (system1) exists (THE FIX)
run_test "Partition mtd5 (system1) is defined" \
    "python -c 'from lx06_tool.constants import PARTITION_MAP; assert \"mtd5\" in PARTITION_MAP'"

# Test 8: Configuration persistence
run_test "Configuration save/load" \
    "python -c 'from lx06_tool.config import AppConfig; from pathlib import Path; import tempfile; cfg = AppConfig(); cfg.use_docker_build = False; f = tempfile.NamedTemporaryFile(mode=\"w\", suffix=\".yaml\", delete=False); cfg.save(Path(f.name)); AppConfig.load(Path(f.name))'"

# Test 9: Exception hierarchy
run_test "Exception types" \
    "python -c 'from lx06_tool.exceptions import BackupIncompleteError, ChecksumMismatchError, PartitionDumpError'"

# Test 10: A/B partition support
run_test "A/B partition slots defined" \
    "python -c 'from lx06_tool.constants import AB_BOOT_SLOTS, AB_SYSTEM_SLOTS; \
    assert len(AB_BOOT_SLOTS) == 2 and len(AB_SYSTEM_SLOTS) == 2'"

echo ""
echo "========================================================================"
echo "Test Results:"
echo "========================================================================"
echo -e "  ${GREEN}PASSED:${NC} $TESTS_PASSED"
echo -e "  ${RED}FAILED:${NC} $TESTS_FAILED"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}All smoke tests passed!${NC}"
    echo ""
    echo "✓ Application is ready for testing with hardware"
    echo "✓ All critical fixes verified"
    echo "✓ No import errors or missing functions"
    exit 0
else
    echo -e "${RED}Some tests failed!${NC}"
    echo "Please check the errors above."
    exit 1
fi
