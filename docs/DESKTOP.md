# Inkly Desktop for Windows

Open **InklyDesktop.exe**. The packaged application includes Python, its window
toolkit and the generic SSH sign-in dialog. Researchers do not need to type a
command or install Python to use this build. Windows OpenSSH Client, access to
the site's network/VPN and an account with Gaussian access are still required.
The HPC must already have the matching Inkly TENSOR endpoint installed.

This is a development build for workflow review, not a signed installer.
Follow your institution's software distribution process; code signing and
automatic updates are not part of this change.

## First calculation

1. **Connection settings:** enter your own username/host, the HPC Inkly launcher
   provided by your maintainer, authentication method and maximum hours per new
   job. Settings stay in `%LOCALAPPDATA%\Inkly\tensor-picker.json`. Existing
   file-picker settings are reused. Never enter credentials in these fields.
2. **Add files** selects one or several Gaussian `.gjf`/`.com` exports.
   **Add folder** adds inputs directly in that folder, without searching
   subfolders. Begin with one or two representative inputs.
3. **Preview files** connects through native OpenSSH and prepares the selected
   inputs. Complete the separate password/MFA dialog when prompted. The table
   shows CPU count, Gaussian memory, total Slurm memory, time limit and stage
   count. **Preview never submits a job.**
4. Select a row and use **View script** to inspect its generated SBATCH script.
   Choose **Submit prepared jobs** to review the job count and combined resource
   request. The confirmation defaults to No. Confirm only the jobs you intend
   to run. Every file in the table is included; remove files before preview to
   narrow the selection. Each input becomes one independent Slurm job.
5. The app watches all approved jobs and retrieves successful, checksum-verified
   logs. A **RETURNED** row has a verified local result. Select that row and
   choose **Open results folder**, then import the logs in TENSOR's
   **Analysis & 3D view → Add logs**, or use the researcher's analysis tools.

Slurm schedules the independent jobs. There is no array or concurrency throttle.
The table and confirmation show demand if every new job runs simultaneously.
Original inputs and existing results are never overwritten. Scientific routes,
structures and analysis stay under the researcher's control.

## Returning later

**Stop watching** and closing the app disconnect its SSH monitoring; they do
not cancel Slurm jobs. Keep the `*.inkly.json` receipts beside the original
inputs. Reopen the app, add the same files and choose **Preview files** to read
their saved handoffs. **Check & return results** watches existing jobs and
downloads successful results; it never submits a prepared job.

Previously submitted jobs are not submitted again. Already returned logs are
verified and left unchanged. Unknown/failed states, changed inputs, changed
resource previews and output collisions stop the workflow for inspection.

If a saved receipt refers to a bundle that is missing on the HPC, Desktop offers
an explicit **Re-prepare** confirmation. Accepting archives the old receipt
beside the input, creates a fresh bundle ID and receipt, and previews the
original input in place. Declining leaves the input and receipt unchanged. The
recovery step never submits a Slurm job. The new preview uses the current
Desktop time-limit setting because the old HPC preparation metadata is missing.

A missing bundle does not prove that an earlier Slurm job has stopped. If the
old calculation may have been submitted, inspect the user's Slurm jobs before
approving a replacement submission. Desktop repeats this warning in the
submission confirmation for recovered handoffs. Never delete archived receipts
or the current receipt to retry. Existing receipts retain their original host,
endpoint and result directory until an explicit missing-bundle recovery.

The GUI uses one background worker and keeps an SSH session between preview,
confirmation and monitoring. The window stays responsive. Reconnecting after
an interrupted/expired session may require another sign-in.

## Authentication and scope

The app reuses an already configured SSH_ASKPASS or the established personal
Cuttlefish helper when present. Otherwise the packaged generic sign-in dialog
from the Windows onboarding code is available. It passes the response to
OpenSSH without storing it. Existing authentication environment values are
restored when the connection closes. No password is stored in GUI preferences.

Host-key verification remains strict. A new user must verify the site's host
fingerprint through the established connection procedure first. The
[Windows onboarding guide](TENSOR.md) describes those prerequisites. Changing
the hostname alone does not make another cluster supported by the generator.

Supported inputs are the same self-contained Gaussian exports as the bridge,
up to 5 MiB each. CPU/memory reuse requires consistent explicit settings across
all linked stages. Inputs generated by a researcher's Python scripts may use
the same contract, but their specific workflow must be reviewed and tested.
This is not a general-purpose Python job launcher or a replacement for TENSOR's
scientific preparation.

## Build and verify (maintainers)

Build on Windows using an isolated environment:

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r scripts/windows/requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows/build_desktop.ps1
```

The output is `dist/InklyDesktop.exe`; the build prints its SHA-256. Build files
and binaries are Git-ignored. CI tests the Windows workflow, builds the executable,
checks its real window and local OpenSSH startup without connecting, and saves
the executable as an Actions artifact. No private configuration or scientific
inputs are included in the binary or artifact.

With Python installed, double-click `Inkly Desktop.pyw` to run from source. The
source launcher reuses your existing authentication helper; building the app
also produces the generic dialog under `build/` for source development.
`tensor.cmd` and the advanced bridge CLI remain available.

Packaging follows the official [PyInstaller Windows application guidance](https://pyinstaller.org/en/stable/usage.html)
and [windowed subprocess requirements](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html).

## Desktop diagnostic events (development build)

The Desktop application records **allowlisted metadata**, not Gaussian inputs,
calculation outputs, authentication dialogs, SSH credentials, or raw exceptions.
Local Windows diagnostic files are written beneath
`%LOCALAPPDATA%\Inkly\diagnostics` (or under the user's private `~/.inkly`
fallback if that Windows variable is absent). They use the existing Inkly
versioned JSONL event schema, with bounded rotation (1 MiB per file, 5 completed
files per session). User-supplied paths and error messages are excluded.

Set `INKLY_DESKTOP_DIAGNOSTICS=0` **before launching the application** to
disable Desktop local and remote event generation. This is independent of the
cluster's `logging.enabled` setting. The HPC copy of Inkly still obeys its own
logging configuration and shared directory permissions.

During an authenticated Desktop SSH session, bounded metadata batches are sent
through the **existing** connection to the remote Inkly handoff endpoint, which
revalidates the event allowlist before writing through the existing
`DiagnosticSession` sinks. It does not open a separate SSH connection or create
a Slurm operation. Remote writes to the lab scratch location require the
configured lab group and an already provisioned authorized directory. A
remote-storage failure leaves the HPC user's private diagnostic sink available
if configured. Windows local records are kept even when SSH is unavailable.

Transport is deliberately **at most once**: events already taken for an SSH
request are never automatically resent after an uncertain interruption. This
avoids affecting submissions or accidentally duplicating an action. Local and
remote log counts may therefore differ. Slow, blocked or failed logging must
not prevent the normal safe job workflow. No event is proof that a Slurm
submission succeeded; rely on the existing receipt and verified scheduler job
state for that.

The Windows executable and the HPC `inkly` package must be upgraded together
for remote event delivery. Until the new HPC endpoint is installed, treat
remote delivery as unverified. The Desktop logs can be used locally without
any Cuttlefish deployment.

**Remaining release validation:** rebuild the Windows executable, run the
packaged GUI with actual OpenSSH/MFA, verify events on Cuttlefish under the
real lab ACL, and check a different lab member's permitted access and an
outsider's denial. Do not alter shared permissions or submit a live job just
to test logging.
