# TENSOR handoff through Inkly

Use TENSOR on your Windows laptop to prepare a calculation, export one
self-contained Gaussian `.gjf` or `.com`, and analyze the returned `.log`.
**Clone this repository on the laptop to get the transfer scripts, and install
the matching Inkly version on the HPC to receive their requests.**

The included `scripts/windows/tensor.ps1` launcher runs the standalone
`scripts/tensor_bridge.py` transport. The bridge reads your local export, sends
its contents through authenticated SSH, and asks HPC-side Inkly to write the
input and generate `job.sbatch`. After confirmation, Inkly submits to Slurm.
The bridge watches the recorded job and retrieves the successful log onto your
laptop. You import that local log into TENSOR yourself.

This workflow uses no chat-model call. TENSOR's own SSH/HPC window is separate;
its interactive MFA limitation is why this companion uses native OpenSSH.
No Windows installation of Inkly's Linux runtime is needed.

## Requirements and supported systems

| Laptop | HPC |
| --- | --- |
| Windows, Windows PowerShell 5.1 (`powershell.exe`), Git, Python 3.9+ and Windows OpenSSH Client | Your own account, Linux, Slurm and permission to run Gaussian |
| This source repository, including `scripts/windows/` and `scripts/tensor_bridge.py` | A matching installed Inkly runtime with the TENSOR endpoint |
| TENSOR obtained separately; network/VPN access required by your site | The supported, verified Gaussian cluster profile and its software/scratch setup |

The current generator supports the verified Cuttlefish profile. Other clusters
need maintainer/admin validation and generator/profile support before use;
editing the SSH hostname alone is insufficient. Use your own account and
site-approved authentication. Never copy another person's private connection
files, receipts or HPC installation.

## 1. Prepare the laptop

All commands in this section run in **PowerShell on the Windows laptop**.
Install Git, Python 3.9+ and the Windows OpenSSH Client through your normal
software-installation process if they are missing. The bridge uses only Python's
standard library: do not run `setup.sh` or install the Linux requirements locally.

Clone the repository into your chosen source folder:

```powershell
git clone https://github.com/thealice-lab/hpc-ink-setup.git
Set-Location hpc-ink-setup
git rev-parse HEAD
Copy-Item .\scripts\windows\tensor-bridge.example.json .\scripts\windows\tensor-bridge.local.json
notepad .\scripts\windows\tensor-bridge.local.json
```

Edit the three fields, preserving valid JSON:

```json
{
  "host": "YOUR_USERNAME@YOUR_HPC_HOST",
  "ink_path": "~/.inkly/bin/ink",
  "auth": "keyboard-interactive"
}
```

- `host`: your own `username@hostname` from the site, or an existing alias in
  your OpenSSH configuration. Ports, jump hosts and other SSH options belong
  in your local OpenSSH config, not this field.
- `ink_path`: the installed HPC launcher. The default is `~/.inkly/bin/ink`;
  change it only when your maintainer provides another matching launcher.
- `auth`: `keyboard-interactive` for password/MFA prompts using the included
  Windows dialog, or `configured` to let OpenSSH use existing site-approved
  key/agent/authentication settings.

Keep credentials out of this file. The local JSON and `*.inkly.json` receipts
are Git-ignored. These still contain account/connection metadata: keep them
private and do not share them as examples. Inputs and results belong in a
research folder outside the clone.

Check the local setup without connecting or submitting:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action check
```

For keyboard-interactive authentication, this compiles the included generic
`ssh_askpass.cs` into a cached executable under `%LOCALAPPDATA%\Inkly\Ssh`.
Later actions select that dialog automatically. Password/MFA responses pass to
OpenSSH and are not saved to disk. Existing authentication environment settings
are restored when the launcher finishes. No private personal helper is required.

The commands explicitly use Windows PowerShell 5.1. `-ExecutionPolicy Bypass`
applies to that process only; it does not change the persistent execution policy.
If institutional policy blocks execution or compilation, use an approved SSH
authentication method with `auth: configured` or contact your administrator.

If Python is not found, add `-Python "C:\Path\To\python.exe"` to the launcher
commands. Otherwise the launcher selects `py -3`, falling back to `python.exe`.
Use `-ConfigPath "C:\Private\tensor-bridge.local.json"` if you keep settings
elsewhere. Relative script paths below assume you are in the laptop clone.

## 2. Install and verify the HPC side

From laptop PowerShell, connect using the configured account:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action connect
```

On first contact, compare the SSH host-key fingerprint with the one provided
through your site's established procedure before accepting it. OpenSSH records
the verified key in your local `known_hosts`. Later bridge actions require that
trusted key and do not accept unknown/changed host keys automatically. Complete
the site's password/MFA prompts in the local dialog as directed by the site.

You are now in an **HPC Linux shell**. Follow [the HPC installation guide](CUTTLEFISH_SETUP.md).
For a new installation, its core commands are:

```bash
git clone https://github.com/thealice-lab/hpc-ink-setup.git
cd hpc-ink-setup
git rev-parse HEAD
bash setup.sh
```

Use the same release/revision on the laptop and HPC. For an existing checkout,
update it through your normal maintenance procedure and rerun `bash setup.sh`
to refresh the installed runtime. Pulling new source alone leaves an old
`~/.inkly/bin/ink` installation unchanged.

Verify the **installed** endpoint and detected profile in the HPC shell:

```bash
~/.inkly/venv/bin/python - <<'PY'
import inspect
from pathlib import Path
import sys

runtime = Path.home() / ".inkly" / "lib"
assert (runtime / "inkly" / "gaussian" / "tensor.py").is_file(), "Install the matching Inkly runtime"
sys.path.insert(0, str(runtime))
from inkly.gaussian import tensor
from inkly.cluster_profile import resolve_cluster_profile

assert runtime.resolve() in Path(tensor.__file__).resolve().parents, "Expected the installed runtime"
assert "session" in inspect.signature(tensor.main).parameters, "Update the installed Inkly runtime"
profile = resolve_cluster_profile()
assert profile and profile.get("name") == "Cuttlefish", "A supported verified profile is required"
print("Installed TENSOR session endpoint and supported profile: ready")
PY
```

This readiness check does not submit a job. It does not establish Gaussian
license/group access, available resources or scientific suitability; verify
those prerequisites with your site's setup guide. A model service is used by
Inkly's chat features; the TENSOR file handoff itself does not call it.

Type `exit` to return to **laptop PowerShell**. The remaining commands run there.

## 3. Export from TENSOR and preview the transfer

In TENSOR's job setup, choose the molecule and scientific settings, then export
the Gaussian input into a research folder, for example `C:\Research\sample_1.gjf`.
Do not put scientific data in the source checkout. Filenames must start with a
letter/digit and contain only letters, digits, dots, underscores or hyphens.
Quoted directory paths may contain spaces.

Upload and prepare the job without submitting it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action preview -InputFile "C:\Research\sample_1.gjf" -Hours 2
```

The launcher opens its own SSH connection; no separate HPC terminal is needed.
Complete sign-in. The preview shows the exported settings, selected CPUs,
Gaussian memory, total Slurm memory, time limit and generated SBATCH script.
The original file stays unchanged. `-Hours 2` is an illustrative maximum,
not a predicted duration; choose a suitable limit. The default is one hour,
and supported explicit limits are 1-336 hours.

Record the printed recovery receipt. With these example paths the defaults are:

```text
Input:    C:\Research\sample_1.gjf
Receipt:  C:\Research\sample_1.gjf.inkly.json
Result:   C:\Research\returned\sample_1.log  (after successful completion)
```

Preview sends the input and creates a private HPC bundle, but no Slurm job or
result. Continue using that receipt rather than starting another preview.
Use `-Receipt "C:\Research\custom.inkly.json"` or `-Out "C:\Research\results"`
during preparation to choose other local destinations. Existing receipts and
results are never overwritten.

## 4. Submit, monitor and automatically return the log

Continue the reviewed preview using its printed receipt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action resume -Receipt "C:\Research\sample_1.gjf.inkly.json"
```

After sign-in, a prepared calculation shows the resource preview and asks:

```text
Submit this calculation? [y/N/s=show SBATCH]
```

Enter or `n` leaves it prepared. `s` displays the script. `y` submits the
reviewed calculation. Submission requires an interactive terminal and never
happens just because upload succeeded.

The command prints the recorded job ID and watches its status. **Leave the
laptop awake, connected to the required network/VPN, and the command running.**
After successful completion, the same connection carries the verified log back
to your laptop and prints:

```text
Verified output saved: C:\Research\returned\sample_1.log
In TENSOR: Analysis & 3D view -> Add logs -> select this file.
```

The HPC is not making an inbound connection to your laptop. The laptop bridge
requests and receives the result over the SSH connection it opened.

Once familiar with the workflow, a new input can use a single guided action:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action run -InputFile "C:\Research\new_sample.gjf" -Hours 2
```

`run` combines preparation, preview, confirmation, submission, monitoring and
return in one authenticated session. Use it for a new calculation with a new
receipt, not to recover one already prepared/submitted.

## 5. Open the returned file in TENSOR

In TENSOR select **Analysis & 3D view -> Add logs**, then choose the exact local
file printed by the bridge. TENSOR handles the scientific analysis. Completion
and transfer checks establish execution/transport success, not the suitability
of the method or correctness of a scientific conclusion.

## Interruptions, status and manual retrieval

Ctrl+C stops the laptop's waiting, **not the Slurm job**. Closing the terminal,
sleeping the laptop or losing network access can interrupt monitoring/return.
Once connectivity is restored, use the same `resume` command above.

For an already submitted bundle, `resume` monitors the recorded job and returns
its successful output without another submission. For a prepared bundle, it
shows the preview and asks before submission. Recovery uses the host, launcher
and authentication mode stored in the receipt, so editing the new-run settings
does not redirect an existing job. Copy your own receipt and the scripts to a
replacement laptop only when permitted, then establish SSH trust there as usual.

Check status without offering submission or waiting:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action status -Receipt "C:\Research\sample_1.gjf.inkly.json"
```

Fetch an already successful result directly, choosing a local output directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action fetch -Receipt "C:\Research\sample_1.gjf.inkly.json" -Out "C:\Research\recovered-results"
```

Add `-Out "C:\Research\recovered-results"` to a launcher `-Action resume`
command to change the destination for that invocation. If the log already exists, choose another directory rather
than overwrite it. Separate status/fetch actions authenticate separately.

Unknown, failed or ambiguous states stop with a message; they never trigger a
retry. `SUBMISSION_UNKNOWN` means a submission may have reached Slurm: inspect
the queue/accounting with your site support before preparing anything new.
Failed jobs remain on the HPC for inspection; this bridge does not cancel or
restart them. An interrupted preparation can leave an unused private bundle;
no automatic remote deletion or retention policy is added.

## Resources and supported inputs

Automatic reuse requires explicit, matching CPU and memory settings in every
Link1 stage. CPU settings may be `%nprocshared` or `%nproc`; memory must be a
positive whole number of `MB` or `GB` (the parser uses 1 GB = 1024 MB). Missing,
conflicting or unsupported settings require explicit overrides.

Inkly derives total Slurm memory from exported Gaussian memory using the trusted
profile's conservative fraction. Under the current 75% policy, `%mem=2GB`
becomes 2048 MB Gaussian memory and 2731 MiB total Slurm memory. The preview
shows the headroom before confirmation. To explicitly override resources:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\tensor.ps1 -Action preview -InputFile "C:\Research\sample_2.gjf" -Cpus 2 -MemoryMiB 1024 -Hours 1
```

These are small test values, not research recommendations. `-MemoryMiB` means
**total Slurm memory**; Gaussian receives 768 MB with this policy. Overrides
and exports are both shown. Scientific routes, coordinates and title/scaling
metadata are retained; resource changes affect a temporary runtime input.

Inputs must be self-contained and at most 5 MiB. Linked stages are supported.
External includes/checkpoints, distributed execution and split/path-based RWF
files are unsupported. HPC bundles live under `~/.inkly/tensor/ID` with private
directories/files. Checkpoints remain there. Gaussian RWF uses job scratch and
is cleaned on exit; scheduler diagnostics are separate from the `.log`.

Fetch requires Slurm `COMPLETED / 0:0`, sufficient normal Gaussian terminations
for the linked stages, no error termination and a matching SHA-256 checksum.
Logs are limited to 64 MiB and saved atomically without overwriting results.

## Troubleshooting and lower-level CLI

| Symptom | What to check |
| --- | --- |
| Local settings missing/placeholders remain | Copy the example JSON, fill in your own host and select the correct launcher/authentication mode. |
| OpenSSH/Python missing | Install the Windows OpenSSH Client and Python 3.9+; use `-Python` for a specific interpreter. |
| Host cannot be resolved or SSH times out | Restore site network/VPN/DNS access; keep the receipt and check status before retrying. |
| Host key is unknown/changed | Verify the fingerprint through the site; use `connect` for first trust. Never disable host checking to proceed. |
| Authentication denied/dialog build blocked | Check your account/MFA with the site; use site-approved configured authentication if required by policy. |
| Invalid Inkly response/missing endpoint | Verify `ink_path`, update/reinstall matching HPC source and repeat the installed endpoint check. |
| Unsupported/missing cluster profile | Ask the maintainer to validate profile/generator support; do not select another cluster's profile to bypass the check. |
| Output not available/failed/timeout | Read the recorded status and HPC diagnostics; fetch accepts only successful jobs. |
| Receipt/result already exists | Resume the existing job; use a different result directory for another retrieval. A deliberate new calculation needs its own receipt. |

The Python CLI remains available for advanced callers:

```powershell
python .\scripts\tensor_bridge.py --help
```

Its `run`, `resume`, `prepare`, `submit RECEIPT --confirm`, `status` and `fetch`
operations use the same protocol. Direct callers must arrange their own
OpenSSH authentication environment. The Windows launcher packages that setup
and preserves receipt-based recovery; no TENSOR interface modification is added.
