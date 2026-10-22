# TENSOR handoff through Inkly

TENSOR prepares Gaussian inputs and analyzes returned logs on Windows. Inkly
validates and prepares the cluster job on Cuttlefish. The standalone
`scripts/tensor_bridge.py` uses native OpenSSH; it needs Python 3.9+ on Windows,
not an Inkly installation. It does not change TENSOR or use its SSH/HPC button.
The publisher's [archived SSH guide](https://github.com/jaredscottwood/TENSOR-releases/blob/568b0e82b99e8d2018707df3e0b58df30bc2933d/SSH_HPC.md)
excludes interactive MFA. This helper uses the existing local authentication
dialog instead. No passwords, keys or MFA responses belong in command arguments.

## Setup

Install the matching Inkly feature version on the HPC through the usual installer.
Verify the server host key using the established site procedure before using the
helper; strict host checking remains enabled. Existing OpenSSH aliases and agent
settings work. For the established local Cuttlefish dialog helper, prepare its
askpass executable in PowerShell (the personal helper remains outside Git):

```powershell
$env:SSH_ASKPASS = & "$env:LOCALAPPDATA\CuttlefishSsh\connect_cuttlefish.ps1" -BuildOnly
$env:SSH_ASKPASS_REQUIRE = 'force'
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
```

Save TENSOR's exported `.gjf` outside the source repository. Each handoff accepts
one self-contained `.gjf` or `.com`, up to 5 MiB, including linked stages. Names
must use letters, digits, dots, underscores or hyphens and start with a letter or
digit. External includes, external checkpoints, distributed execution directives
and split/path-based RWF files are unsupported. Use a new receipt for each input.

## Prepare, inspect, submit, retrieve

```powershell
py scripts/tensor_bridge.py --ssh $ssh prepare C:\Research\sample_1.gjf --host YOUR_USER@YOUR_CLUSTER --receipt C:\Research\sample_1.receipt.json --cpus 2 --memory-mib 1024 --hours 1
py scripts/tensor_bridge.py --ssh $ssh submit C:\Research\sample_1.receipt.json --confirm
py scripts/tensor_bridge.py --ssh $ssh status C:\Research\sample_1.receipt.json
py scripts/tensor_bridge.py --ssh $ssh fetch C:\Research\sample_1.receipt.json --out C:\Research\returned
```

**Inspect the preparation preview before submitting.** Resource values above are
small test examples, not recommendations for research calculations. Memory is
the total Slurm MiB allocation; Gaussian receives the trusted profile's 75%
allocation in MB. CPU/memory directives in every runtime Link1 stage use the
explicit preview values, replacing TENSOR's exported resource settings. Increase
the allocation appropriately for the intended calculation. No scientific route,
coordinates, title scaling metadata or original input bytes are changed.

Each action may prompt for local authentication. `prepare` never submits;
`submit --confirm` is a separate action. HPC bundles live under
`~/.inkly/tensor/ID`, with owner-only directories/files. Gaussian's RWF is placed
in job scratch and cleaned on exit; checkpoints remain in the private bundle.
The Gaussian output is `sample_1.log`; scheduler diagnostics are separate.
Status queries only the bundle's recorded Slurm job ID. No cancellation command
is provided. Broader job management remains in team issues #11 and #12.

Fetch requires Slurm `COMPLETED / 0:0` and normal termination for every linked
Gaussian stage. It verifies the output SHA-256 and atomically creates the local
`.log` without overwriting existing results. Open it using TENSOR's **Analysis &
3D view → Add logs**. Maximum returned log size is 64 MiB. Failed calculations
stay on the HPC for inspection and are not returned as successful results.

If SSH fails, check login, host trust and the remote Inkly version. Keep the
receipt even if a connection drops. Run `status` after an interrupted submission.
`SUBMISSION_UNKNOWN` means an attempt may have reached Slurm: inspect your queue
before making another bundle; the helper will not retry that submission. An
interrupted preparation can leave an unused private bundle. Do not edit its
receipt or generated script. No automatic remote deletion or log retention
policy is added. Keep receipts, research files and runtime artifacts outside Git.
