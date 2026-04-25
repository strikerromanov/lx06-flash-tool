#!/bin/bash
# Unlock bootloader for recovery access
#
# Sets bootdelay in U-Boot environment to allow serial console access
# for recovery from bad flashes.
#
# The official aml-flash-tool.sh uses 'save' but most Amlogic U-Boot
# builds also accept 'saveenv'. We try both.

set -e

UPDATE="${UPDATE:-update}"
BOOTDELAY="${1:-15}"

echo "Unlocking bootloader (setting bootdelay=$BOOTDELAY)..."

# Set bootdelay
$UPDATE bulkcmd "setenv bootdelay $BOOTDELAY"

# Save environment (try saveenv first, then save)
$UPDATE bulkcmd "saveenv" || $UPDATE bulkcmd "save"

echo "Bootloader unlocked. bootdelay=$BOOTDELAY"
