# M2 golden image (EC2 Mac host + tart VM)

Builds the `raven-golden` tart VM that `Ec2TartProvisioner` clones per session.

Two layers:

1. **Host (EC2 Mac dedicated host, mac2.metal)** — runs `tart`; each session is a
   `tart clone raven-golden raven-<session-id>`.
2. **Guest (macOS VM)** — contains Xcode, Simulator runtimes, xcodebuildmcp, and
   the Raven worker daemon (`apps/worker`) listening on `:8787`.

## Usage

On a freshly launched EC2 Mac instance (from the standard macOS AMI):

```bash
# 1. Prepare the host: Homebrew, tart, socat, pull base image
./01-setup-host.sh

# 2. Create the golden VM from the base image and boot it
tart clone ghcr.io/cirruslabs/macos-sequoia-xcode:latest raven-golden
tart run --no-graphics raven-golden &
VM_IP=$(tart ip raven-golden)

# 3. Provision inside the VM (Xcode already in the cirruslabs image;
#    adds runtimes, xcodebuildmcp, and the worker daemon)
scp 02-setup-guest.sh admin@"$VM_IP":/tmp/
ssh admin@"$VM_IP" 'bash /tmp/02-setup-guest.sh'

# 4. Smoke-test the worker RPC, then stop the VM to freeze the image
ssh admin@"$VM_IP" 'FURNACE_WORKER_TOKEN=smoke python3 -m apps.worker & sleep 3; \
  curl -fsS http://localhost:8787/healthz'
tart stop raven-golden
```

Notes:

- The `cirruslabs/macos-*-xcode` base images ship with Xcode preinstalled,
  which avoids the slow manual Xcode install; `02-setup-guest.sh` verifies it
  and adds the pieces Raven needs.
- The worker daemon is **not** auto-started in the golden image; the
  provisioner's `SshTartRuntime.start_worker` starts it per session with a
  per-session bearer token.
- Rebuild by deleting `raven-golden` and re-running from step 2.
