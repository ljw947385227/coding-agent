# Docker execution sandbox

The runtime keeps local execution as the default. Build the offline base image and opt in:

```powershell
docker build -f docker/sandbox/python.Dockerfile -t kama-sandbox-python:3.12-v1 .
$env:KAMA_EXECUTION_BACKEND = "docker"
```

Restart `kama-core` after changing configuration. The equivalent project configuration is:

```toml
[execution]
backend = "docker"

[execution.docker]
image = "kama-sandbox-python:3.12-v1"
network = "none"
memory_mb = 512
cpus = 1.0
pids_limit = 64
tmpfs_mb = 256
user = "10001:10001"
```

The base image contains Python 3.12 and can be built without package-network access once
`python:3.12-slim` is present locally. It is enough for shell and standard-library smoke tests.
It deliberately does not copy the host virtual environment into the Linux container.

For this repository's full verification toolchain, build the optional project image while
Docker has package-network access, then select it:

```powershell
$fingerprint = uv run python -c "from pathlib import Path; from kama_claude.core.sandbox.info import environment_fingerprint; print(environment_fingerprint(Path('.').resolve())[0])"
docker build -f docker/sandbox/python-project.Dockerfile `
  --build-arg "KAMA_ENVIRONMENT_FINGERPRINT=$fingerprint" `
  -t kama-sandbox-python-project:3.12-v1 .
$env:KAMA_DOCKER_IMAGE = "kama-sandbox-python-project:3.12-v1"
```

For another repository, create an image containing that project's OS packages and language
dependencies and point `execution.docker.image` at it. Project files are mounted read/write at
`/workspace`; dependencies come from the image. By default the container has no network, uses a
read-only root filesystem and a non-root user, and receives CPU, memory, PID and tmpfs limits.
Sensitive paths are covered by empty read-only mounts.

The `sandbox_info` tool compares the current dependency-input fingerprint with the image label,
then reports safe host metadata, image identity and a read-only runtime probe. It never exposes
host environment variables or secret values. Rebuild the image with a fresh fingerprint after
changing a Dockerfile, package manifest or lockfile.

Run the real-engine integration checks explicitly:

```powershell
$env:KAMA_DOCKER_INTEGRATION = "1"
uv run pytest tests/integration/test_sandbox_docker.py -q
```
