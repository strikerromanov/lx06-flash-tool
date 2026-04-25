#!/bin/bash
# Unlock bootloader for recovery access
update bulkcmd "setenv bootdelay 15"
update bulkcmd "saveenv"
echo "Bootloader unlocked. bootdelay=15"
