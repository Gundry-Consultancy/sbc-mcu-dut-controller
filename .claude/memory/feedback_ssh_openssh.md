---
name: feedback-ssh-openssh
description: Use Windows OpenSSH with StrictHostKeyChecking=accept-new when SSHing from the Bash tool; prefer git-based sync over scp for deploying files
metadata:
  type: feedback
---

Use `"C:\Windows\System32\OpenSSH\ssh.exe"` (Windows OpenSSH, double-quoted) when running SSH/SCP commands via the Bash tool. Always add `-o StrictHostKeyChecking=accept-new` to avoid hangs on first connection to a host not yet in known_hosts.

**Why:** The Bash tool uses Git Bash (`/usr/bin/ssh`) by default, which lacks the Windows SSH agent keys. Windows OpenSSH has the correct keys. New hosts (e.g. rpi-displays at 192.168.1.234) will hang indefinitely without `StrictHostKeyChecking=accept-new` because the interactive host-key prompt is never shown.

**How to apply:** Any SSH or SCP command: use `"C:\Windows\System32\OpenSSH\ssh.exe" -o StrictHostKeyChecking=accept-new` and `"C:\Windows\System32\OpenSSH\scp.exe" -o StrictHostKeyChecking=accept-new`. After first connection the host is added to known_hosts and the flag is a no-op.

## Prefer git sync over scp for deploys

When deploying file changes to a remote host (e.g. rpi-displays), prefer pulling from git on the remote rather than scp'ing the file across. After committing+pushing locally, SSH in and `git pull` (or `git fetch && git reset --hard origin/<branch>`) in the checkout on the host, then restart the relevant service.

**Why:** Keeps the remote host's working tree honest — no drift between what's deployed and what's in the repo, no orphan edits that only exist on one machine, and the deploy is reproducible from history. scp silently overwrites whatever was there.

**How to apply:** If the remote already has a clone of the repo, push first, then `ssh <host> "cd <repo> && git pull && sudo systemctl restart <service>"`. Only fall back to scp if the host has no clone or git access. If you're unsure whether a clone exists on the remote, ask before reaching for scp.
