#!/usr/bin/env bash
# FDM throwaway test target — first-boot provisioning (DOO-42).
# Run ONCE inside the fresh Ubuntu VM, from the Hyper-V console, as the
# user created during Ubuntu install:
#
#   sudo bash provision-test-target.sh
#
# Idempotent: safe to re-run after a checkpoint restore or rebuild.
set -euo pipefail

FDM_USER="fdm"
FDM_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL2BMagoxjdR7Ul1v4Z+YVg0t/mdOlWFG1ISCdH8KXWZ fdm-test-target@doosly"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq openssh-server

# Service account the FDM control plane connects as. Passwordless sudo is
# deliberate: this VM is throwaway by policy and hosts nothing valuable.
if ! id "$FDM_USER" &>/dev/null; then
    adduser --disabled-password --gecos "FDM test service account" "$FDM_USER"
fi
usermod -aG sudo "$FDM_USER"
echo "$FDM_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$FDM_USER"
chmod 440 "/etc/sudoers.d/90-$FDM_USER"

install -d -m 700 -o "$FDM_USER" -g "$FDM_USER" "/home/$FDM_USER/.ssh"
grep -qxF "$FDM_PUBKEY" "/home/$FDM_USER/.ssh/authorized_keys" 2>/dev/null \
    || echo "$FDM_PUBKEY" >> "/home/$FDM_USER/.ssh/authorized_keys"
chmod 600 "/home/$FDM_USER/.ssh/authorized_keys"
chown "$FDM_USER:$FDM_USER" "/home/$FDM_USER/.ssh/authorized_keys"

hostnamectl set-hostname fdm-test
systemctl enable --now ssh

echo
echo "== Provisioning done. Report this back on DOO-42: =="
echo "   user: $FDM_USER"
ip -4 -o addr show scope global | awk '{print "   ip:   " $4}'
echo
echo "Now take the baseline checkpoint from the Windows host (PowerShell):"
echo '   Checkpoint-VM -Name fdm-test -SnapshotName baseline'
