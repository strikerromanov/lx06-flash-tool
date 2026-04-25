#!/bin/bash
# Dump all NAND partitions for LX06 backup
#
# This uses the two-step NAND dump process:
#   1. store read.part <label> <addr> 0 <size>   (NAND → device RAM)
#   2. mread mem <addr> normal <size> <file>      (device RAM → host)
#
# Source: Radxa aml-flash-tool aml-flash-tool.sh

set -e

UPDATE="${UPDATE:-update}"
OUTPUT_DIR="${1:-./backups}"
ADDR="0x03000000"

mkdir -p "$OUTPUT_DIR"

# LX06 partition layout (Amlogic AXG NAND)
# Format: label:size_hex
PARTITIONS=(
    "bootloader:0x100000"    #  1 MB  (mtd0)
    "tpl:0x200000"           #  2 MB  (mtd1)
    "boot0:0x800000"         #  8 MB  (mtd2)
    "boot1:0x800000"         #  8 MB  (mtd3)
    "system0:0x2000000"      # 32 MB  (mtd4)
    "system1:0x2000000"      # 32 MB  (mtd5)
    "data:0x800000"          #  8 MB  (mtd6)
)

for entry in "${PARTITIONS[@]}"; do
    label="${entry%%:*}"
    size="${entry##*:}"
    outfile="$OUTPUT_DIR/${label}.img"

    echo "Dumping $label ($size bytes)..."

    # Step 1: Read NAND partition into device RAM
    $UPDATE bulkcmd "store read.part $label $ADDR 0 $size" || \
    $UPDATE bulkcmd "store read.part $label $ADDR $size" || \
    $UPDATE bulkcmd "nand read.part $label $ADDR $size" || {
        echo "ERROR: Failed to read $label from NAND"
        continue
    }

    # Step 2: Dump device memory to host file
    $UPDATE mread mem $ADDR normal $size "$outfile" || {
        echo "ERROR: Failed to dump $label to $outfile"
        continue
    }

    echo "  → $outfile ($(stat -c%s "$outfile") bytes)"
done

echo "All partitions dumped to $OUTPUT_DIR"
