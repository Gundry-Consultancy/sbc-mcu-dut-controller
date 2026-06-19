# Project State & Tachyon Device Configuration

## Tachyon Device Details
- **Host IP**: `192.168.1.169`
- **Username**: `particle`
- **OS**: Ubuntu 24.04 (`6.8.0-1058-particle` kernel)
- **SSH Key**: `~/.ssh/id_ed25519` on the host

## HIL Controller Service
- The HIL Controller runs on the Tachyon as a systemd system service:
  - **Service Name**: `hil-controller.service`
  - **Start/Restart**: `sudo systemctl restart hil-controller`
  - **Status check**: `systemctl status hil-controller`
  - **Web UI / API Port**: `http://192.168.1.169:8080/`
  - **API Token**: `dev-token-change-me`

## Git Updates & Credentials
- To pull updates on the Tachyon, you must configure git to authenticate. 
- A Personal Access Token (PAT) with access to the `gundry-consultancy` and `tyeth-ai-assisted` repositories is configured on the remotes.
- Active PAT: `github_pat_REDACTED`
- Remote URL configurations on the Tachyon:
  - `usbip-hil-controller`: `https://github_pat_<token>@github.com/tyeth-ai-assisted/usbip-hil-controller.git`
