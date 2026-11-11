# TENSOR handoff through Inkly

Keep using TENSOR on Windows to prepare calculations and analyze results. Export
one self-contained Gaussian `.gjf` or `.com`, then run the helper once. Inkly on
the HPC prepares the trusted cluster script, asks before submission, monitors
the recorded job and returns its verified `.log` for TENSOR.

The standalone `scripts/tensor_bridge.py` needs Python 3.9+ and native OpenSSH
on Windows; it does not install Inkly on Windows or change TENSOR. Install the
matching Inkly version on the HPC. TENSOR's native SSH/HPC window is separate:
its publisher's [archived SSH guide](https://github.com/jaredscottwood/TENSOR-releases/blob/568b0e82b99e8d2018707df3e0b58df30bc2933d/SSH_HPC.md)
excludes interactive MFA. This helper uses the existing local authentication
dialog and one SSH connection for the handoff.

## Setup

Verify the server host key through the established site procedure first. Strict
host checking stays enabled; existing OpenSSH aliases work. The default
keyboard-interactive authentication matches the Cuttlefish MFA helper. Use
`--auth configured` when your site uses existing OpenSSH key/agent settings.
For the established personal Cuttlefish dialog helper, prepare its executable
in PowerShell (the personal helper remains outside Git):

```powershell
$env:SSH_ASKPASS = & "$env:LOCALAPPDATA\CuttlefishSsh\connect_cuttlefish.ps1" -BuildOnly
$env:SSH_ASKPASS_REQUIRE = 'force'
$ssh = "$env:WINDIR\System32\OpenSSH\ssh.exe"
```

Keep input, receipts and results outside the source repository. Input is limited
to 5 MiB. Filenames must start with a letter/digit and contain only letters,
digits, dots, underscores or hyphens. Linked stages are supported. External
includes/checkpoints, distributed execution and split/path-based RWF files are
unsupported. No password, key or MFA response belongs in command arguments.

## One handoff, followed by analysis in TENSOR

Export the input in TENSOR, then use:

```powershell
py scripts/tensor_bridge.py --ssh $ssh run C:\Research\sample_1.gjf --host YOUR_USER@YOUR_CLUSTER
```

The helper shows the exported settings, selected CPUs, Gaussian runtime memory,
total Slurm allocation and wall-time limit. The original input stays unchanged.
The default time limit is **one hour**, not a recommended duration for research;
set `--hours` as needed (1–336). The prompt is:

```text
Submit this calculation? [y/N/s=show SBATCH]
```

Press Enter or `n` to stop after preparation, `s` to inspect the generated
script, or `y` to submit the reviewed calculation. Submission requires an
interactive terminal. It never happens merely because preparation succeeded.

After submission the same connection watches that job and retrieves its log
when both Slurm and Gaussian report successful completion. The final message
gives the verified file path. In TENSOR choose **Analysis & 3D view → Add logs**
and select that file; TENSOR continues to handle the scientific analysis.

By default, the local recovery receipt is `sample_1.gjf.inkly.json`, and results
go under `returned` beside the input. Use `--receipt` or `--out` to choose other
locations. Existing receipts/results are never overwritten.

## Reusing exported resources

Automatic reuse requires explicit, matching CPU and memory settings in **every**
Link1 stage. CPU settings may be `%nprocshared` or `%nproc`; memory must be a
positive whole number of `MB` or `GB` (1 GB = 1024 MB). Missing directives,
conflicting values and other units require an explicit override; no Gaussian
default or inheritance between stages is assumed.

Inkly derives the total Slurm allocation from exported Gaussian memory using
the trusted cluster's conservative memory fraction. With the current 75%
policy, `%mem=2GB` becomes **2048 MB Gaussian memory and 2731 MiB total Slurm
memory**. The preview exposes that adjustment before confirmation.

If reuse is unavailable, or you deliberately want different resources:

```powershell
py scripts/tensor_bridge.py --ssh $ssh run C:\Research\sample_1.gjf --host YOUR_USER@YOUR_CLUSTER --cpus 2 --memory-mib 1024 --hours 1
```

These are small test values. `--memory-mib` means **total Slurm memory**; the
example gives Gaussian 768 MB under the 75% policy. Overrides and exported
values are both shown. Runtime resource directives in every stage use the
reviewed values. Scientific routes, coordinates and title/scaling metadata are
retained; only the temporary runtime copy changes. The original bytes remain
protected by a checksum.

For inspection without any submission prompt:

```powershell
py scripts/tensor_bridge.py --ssh $ssh run C:\Research\sample_1.gjf --host YOUR_USER@YOUR_CLUSTER --preview
```

## Interruptions and recovery

Ctrl+C stops the helper's waiting, **not the Slurm job**. Keep the receipt:

```powershell
py scripts/tensor_bridge.py --ssh $ssh resume C:\Research\sample_1.gjf.inkly.json
```

`resume` checks the existing bundle. A merely prepared bundle shows its preview
and asks before submission. An already submitted bundle is monitored and
retrieved without another submission. Unknown, failed or ambiguous states stop
with a message; they do not trigger a retry. `SUBMISSION_UNKNOWN` means an
attempt may have reached Slurm: inspect the queue before preparing anything new.

If settings need changing, prepare a new receipt explicitly. After an existing
result was already retrieved, use `resume --out` with a new directory rather
than overwrite it. If SSH fails, check the network/VPN, login, host trust and
matching HPC version. An interrupted preparation can leave an unused private
bundle; no remote deletion or retention policy is added.

## Advanced individual operations and scope

The separate `prepare`, `submit RECEIPT --confirm`, `status RECEIPT` and
`fetch RECEIPT --out DIRECTORY` commands remain available for manual operation
and older receipts. `prepare` also infers resources when they are unambiguous.
These individual commands each authenticate separately.

HPC bundles live under `~/.inkly/tensor/ID`, with owner-only directories/files.
Checkpoints remain in the private bundle; Gaussian RWF uses job scratch and is
cleaned on exit. Scheduler diagnostics are separate from the matching `.log`.
Fetch requires Slurm `COMPLETED / 0:0`, normal termination for each linked stage,
no error termination and a matching SHA-256. Logs are limited to 64 MiB and
published atomically without overwriting results. Failed calculations stay on
the HPC for inspection.

This is a bounded file handoff, with explicit confirmation. Conversational job
generation, general job management/cancellation and automatic documentation
database provisioning remain separate work. Independent shared-logging access
validation is also outstanding. None is implied by completing this workflow.
