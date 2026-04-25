#!/bin/bash
# Dump all NAND partitions for LX06 backup
#
# Uses direct NAND→host transfer:
#   update mread store <label> normal <file>
#
# This avoids the broken two-step RAM approach
# (store read.part → mread mem) which caused heap corruption
# at address 0x03000000 on AXG SoCs.
#
# Source: Radxa aml-flash-tool aml-flash-tool.sh

set -e

UPDATE="${UPDATE:-update}"
OUTPUT_DIR="${1:-./backups}"

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

    # Direct NAND→host transfer (correct approach)
    $UPDATE mread store "$label" normal "$outfile" || \
    $UPDATE mread "$label" "$outfile" || \
    $UPDATE mread store "$label" normal "$size" "$outfile" || {
        echo "ERROR: Failed to dump $label"
        continue
    }

    if [ -f "$outfile" ]; then
        fsize=$(stat -c%s "$outfile" 2>/dev/null || stat -f%z "$outfile")
        # Check magic bytes
        magic=$(xxd -l 4 -p "$outfile" 2>/dev/null | head -1)
        case "$magic" in
            68737173) fmt="squashfs (LE)" ;;
            73716873) fmt="squashfs (BE)" ;;
            00000000) fmt="EMPTY (zeros)" ;;
            ffffffff*) fmt="UNREAD (0xFF)" ;;
            *) fmt="unknown ($magic)" ;;
        esac
        echo "  → $outfile ($fsize bytes, $fmt)"
    else
        echo "  → ERROR: $outfile not created"
    fi
done

echo "All partitions dumped to $OUTPUT_DIR"
