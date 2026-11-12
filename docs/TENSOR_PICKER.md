# Choose TENSOR exports with a short launcher

After TENSOR generates your Gaussian inputs, open PowerShell in this repository
and run:

```powershell
.\tensor.cmd
```

You can also double-click `tensor.cmd` in File Explorer. It keeps the console
open so you can read the result. Python 3.9+ with Tcl/Tk and native OpenSSH must
already be installed. The launcher uses only Python's standard library.

1. Choose **1 Preview only** (or press Enter).
2. In the file picker, open TENSOR's `gjf_files` folder. Select one `.gjf` or
   `.com`, or hold Ctrl/Shift to select several. Cancel makes no connection.
3. On first use, enter your own HPC username/host, the matching HPC Inkly
   launcher path, authentication method and time limit. These preferences are
   saved under `%LOCALAPPDATA%\Inkly\tensor-picker.json`, outside the repository.
   Never put credentials in this file or the console setup questions.
4. Complete your site's normal sign-in. The preview sends the selected inputs
   to private HPC bundles and generates their SBATCH scripts. **It submits no
   calculation.** One SSH connection handles the entire selection.
5. Read the table: CPUs, Gaussian memory, total Slurm memory, maximum hours,
   stage count and state. Type `s` if you want to read the generated scripts.

The time limit is a maximum per job, not an estimated runtime. New handoffs use
the saved setting; prepared handoffs always keep their original settings.
Existing receipts are reused, never overwritten. Selecting an edited input
with an old receipt stops rather than running an earlier version silently.

## Run and return results

Run the same short command again, choose **2 Run selected calculations**, and
select the files to run. Inkly prepares new files and checks saved handoffs.
After every file is reviewed, the launcher lists the new jobs and their combined
CPU/memory request if they all run at once. To approve two jobs, type exactly
`submit 2`. Pressing Enter leaves them prepared. There is no automatic approval.

Each input becomes one independent Slurm job, including all its linked Gaussian
stages. The launcher submits the approved jobs before monitoring them together;
Slurm decides when each starts. There is no array or concurrency throttle. Choose
a small subset for a demo rather than selecting an entire conformer collection.
All existing Cuttlefish generator, group-access and Gaussian checks still apply.

Successful logs are checksum-verified and saved under `returned` beside each
input (or the directory recorded in an older receipt). Original inputs and
existing results are never overwritten. In TENSOR, open **Analysis & 3D view →
Add logs** and select the returned files. TENSOR handles scientific preparation
and analysis; Inkly handles the cluster handoff.

## Resume safely

Ctrl+C or closing the window stops monitoring, **not running HPC jobs**. Start
the launcher, choose **3 Resume saved handoffs**, and select the same original
inputs. Keep their adjacent `*.inkly.json` recovery receipts.

Resume checks each recorded job before acting. It only asks about submissions
for files still marked PREPARED. Submitted jobs are watched without being
resubmitted. Already returned logs are checked against the remote checksum and
left untouched. Failed/unknown job states stop the selection for review, as does
an authentication interruption. Other submitted jobs may continue on the HPC.
An interrupted preparation may need maintainer inspection; do not delete a
receipt and blindly retry. Different accounts/endpoints must be resumed in
separate selections. Identical result filenames in the same output directory
are rejected before connecting.

## Connection and local setup

The HPC needs the matching released Inkly TENSOR endpoint. The normal default
is `~/.inkly/bin/ink`; use a different path only when supplied by your maintainer.
The endpoint and generator remain specific to the verified Cuttlefish profile.
Changing the host alone does not add support for another cluster.

With `keyboard-interactive`, this launcher reuses your existing `SSH_ASKPASS`.
On this established Windows setup it can also prepare the existing personal
`%LOCALAPPDATA%\CuttlefishSsh\connect_cuttlefish.ps1` helper with `-BuildOnly`.
The helper stays outside Git and is not modified. Passwords/MFA stay in the local
dialog. Other users need their own site-approved helper, or `configured`
authentication to use their existing OpenSSH settings. This feature does not
ship a new authentication implementation or bypass institutional policy.
Host-key verification remains strict; establish trust by the site's procedure.

Local checks, without a file picker or network connection:

```powershell
.\tensor.cmd --check
```

Edit saved preferences:

```powershell
.\tensor.cmd --configure
```

For a new selection with a different time limit:

```powershell
.\tensor.cmd --hours 2
```

If Tcl/Tk is unavailable, use `python scripts/tensor_picker.py --mode preview
--files "C:\Research\sample_1.gjf" "C:\Research\sample_2.gjf"` instead. Explicit
CPU/memory overrides, alternate receipts and advanced recovery remain in the
[original bridge CLI](TENSOR.md). The picker reuses unambiguous exported
resources; it does not guess missing scientific settings.
