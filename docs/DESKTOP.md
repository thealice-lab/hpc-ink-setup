# Inkly Desktop for Windows

Open **InklyDesktop.exe**. The packaged application includes Python, its window
toolkit and the generic SSH sign-in dialog. Researchers do not need to type a
command or install Python to use this build. Windows OpenSSH Client, access to
the site's network/VPN and an account with Gaussian access are still required.
The HPC must already have the matching Inkly TENSOR endpoint installed.

This is a development build for workflow review, not a signed installer.
Follow your institution's software distribution process; code signing and
automatic updates are not part of this change.

## Download and open Inkly Desktop

The EXE is **not committed to Git**. The helper pins the
[Inkly Desktop 0.5.1 (preview)](https://github.com/thealice-lab/hpc-ink-setup/releases/tag/desktop-v0.5.1)
release, built by GitHub Actions from commit
`7dc3a0a5dc3cc25e655f8237e6b7b9f6f8710f8e`. It never silently selects a newer
build. Do not substitute another EXE or bypass a hash failure.

1. In this checkout, double-click **Get-InklyDesktop-Test.cmd** at the repository
   root. Windows PowerShell 5.1 is required. Python and administrator rights
   are not needed, and the helper does not change persistent execution policy
   or install Windows components.
2. It downloads `InklyDesktop-0.5.1-windows.zip` from the release. Release
   downloads are public: no GitHub sign-in, and they do not expire. If the
   download is blocked (for example by a proxy), the helper opens the release
   page instead: download the ZIP there, return to the helper and press Enter,
   or paste the ZIP's full path. Enter **Q** to cancel. The helper never
   collects credentials and never chooses another build.
3. It extracts only `InklyDesktop.exe`, verifies its SHA-256, and places it at
   `%LOCALAPPDATA%\Inkly\DesktopBuilds\0.5.1\InklyDesktop.exe`. This is
   separate from the checkout and other builds. It preserves existing settings
   and refuses to replace an unverified EXE already at that path.
4. It runs a fresh **local-only** smoke test of the real window, bundled sign-in
   dialog, packaged Python and local OpenSSH availability, then launches the
   GUI. Each run saves `local-smoke-test-<id>.json` beside the versioned EXE.
   Neither the helper nor this smoke test connects to an HPC or submits a job.
   Opening the GUI also does not submit a job; later cluster actions require
   the connection prerequisites described below.

The helper checks the **EXE** against this exact SHA-256, also listed in the
release's `SHA256SUMS.txt`:

```text
728EAABCFB8DDC52F7AE6447A4E6682B510A947F941B71E00EAAE7A6B0E3D1BD
```

Anyone can also confirm that GitHub Actions built it from this repository with
`gh attestation verify InklyDesktop.exe --repo thealice-lab/hpc-ink-setup`.

For standalone use without a clone, download both `Get-InklyDesktop-Test.cmd`
and `scripts/windows/get_inklydesktop_test.ps1` from the same reviewed source
revision and keep them beside one another. The `.cmd` supports both layouts.
For an already downloaded ZIP, or a local check without launching the full GUI:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\get_inklydesktop_test.ps1 -ArtifactZip 'C:\path\to\InklyDesktop-0.5.1-windows.zip' -NoLaunch
```

A ZIP with the wrong contents stops verification, and a bad existing cached EXE
is left untouched for review. If OpenSSH is missing, enable **OpenSSH Client** in
Windows Optional Features according to your institution's policy and retry.
Do not override Windows security warnings for an unverified source. Opening
`InklyDesktop.exe` straight from a downloaded ZIP can show a Windows SmartScreen
warning because the build is not Authenticode-signed; the helper checks the
EXE's exact SHA-256 before running it.

Maintainers: after publishing a new release (see **Publish a release** below),
update the helper's version, source commit and EXE SHA-256 (from the release's
`SHA256SUMS.txt`) together, along with this page, the README and
`tests/test_windows_desktop_download.py`. Do not commit ZIP/EXE binaries or
treat this helper as an HPC deployment tool.

## First calculation

Use **Test connection** beside Connection settings before adding inputs. It
signs in through the configured SSH connection and asks the Inkly endpoint for
its version and supported handoff operations. A successful check keeps the
connection open for Preview. It does not prepare a bundle or run a Slurm
command. The result says whether the HPC copy of Inkly offers every operation
this Desktop needs and names optional features, such as Cancel job, that stay
unavailable until that copy is updated. An HPC copy from an earlier release is
fine when it offers those operations. This checks access to the endpoint, not
Gaussian runtime availability, scheduler capacity or whether a calculation will
succeed.

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

You can keep working while the app watches. **Add files** or **Add folder**,
then **Preview files** and **Submit prepared jobs** for the new inputs; **Run
again** and **Cancel job** work too. These run on the connection that is
already open, so there is no new sign-in, and the app keeps watching every
job, old and new. Adding or removing files keeps the rows already shown and
marks only new files "Not previewed"; submitting always needs a fresh preview
of the current list. **Remove selected**, **Clear list** and **Connection
settings** wait until the app is not watching.

The summary below the table follows the rows. Before submission it shows the
prepared job count and the combined request if every job starts together. Once
you confirm, it counts each row's latest status (for example `1 PENDING`,
`1 RUNNING`, `1 RETURNED`) and lists the requested limits of jobs still on the
HPC. Those are Slurm requests, not measured CPU or memory use. If a submission
is interrupted, the summary says previously PREPARED rows may already be
submitted; preview the same files to read their recorded state before trying
again.

Slurm schedules the independent jobs. There is no array or concurrency throttle.
The table and confirmation show demand if every new job runs simultaneously.
Original inputs and existing results are never overwritten. Scientific routes,
structures and analysis stay under the researcher's control.

## Pending and failed-job details

Select one previewed row and choose **Job details** above Activity. It works
while watching, using the same SSH connection. For a pending job it shows
Slurm's reason, estimated start in the HPC's local time, elapsed run time and
the observation time in UTC. Start estimates can change; unavailable estimates
are shown explicitly. Elapsed run time excludes time waiting in the queue.
These fields use [squeue](https://slurm.schedmd.com/squeue.html) and
[sacct](https://slurm.schedmd.com/sacct.html) for that recorded job only.

For **FAILED**, **TIMEOUT** and **OUT_OF_MEMORY**, Job details reads at most
the final 16 KiB / 60 lines of each known Gaussian and Slurm output file and
highlights the last Gaussian error-termination line in that excerpt. Missing,
unreadable or unsafe files are reported as unavailable. A scheduler kill may
leave no Gaussian error line; the excerpts do not establish a scientific cause.
Partial failed output is never marked RETURNED or imported as a verified result.

Log excerpts appear only in the requested details window. They can contain
research data and are not written to Activity, diagnostic events or receipts.
This operation does not modify the bundle or submit, cancel or retry jobs.
An older HPC endpoint keeps working and displays an upgrade notice for details.
Full details require a matching endpoint with the `job_details` capability.

## Returning later

### Finding an uncertain submission

New Desktop endpoint submissions carry the Slurm comment `inkly:<bundle>`.
The bundle is the generated ID in the saved `.inkly.json` receipt; the comment
contains no input name or file path. If the submission reply is lost, use your
own read-only Slurm queue to find that exact comment before deciding what to
do. For example, on the HPC:

```sh
squeue --me --format='%.18i %.12T %k'
```

Compare the entire comment with the bundle in your receipt. Scheduler comment
visibility and accounting retention depend on cluster configuration. Absence
from the queue does not prove no job was submitted; completed jobs may already
have left it. Keep the uncertain receipt and ask the maintainer to inspect
accounting. Inkly does not automatically resubmit, adopt a matching job, or
rewrite a receipt based on comments. Existing jobs are not retagged. This
requires the updated HPC endpoint; the Desktop executable alone cannot add tags.

**Stop watching** ends the watch after the check in progress and keeps the
connection open, so **Check & return results** starts watching again without a
new sign-in. If the watch does not end within 20 seconds, or you click Stop
watching again, the app disconnects instead. Neither cancels Slurm jobs.
Closing the app disconnects as well.

### Load my jobs and Job history

**Load my jobs**, in the top bar beside Job history, fills the list from the
HPC instead of choosing files. It adds your pending and running Inkly jobs and jobs that
ended in the last 3 days, then checks them with a normal preview, so each row
shows the job's current state. Nothing is submitted. Choose **Check & return
results** to watch them and download finished results.

Desktop keeps a small list of which input file each job belongs to, and of
the folders you add files from, in `%LOCALAPPDATA%\Inkly\desktop-jobs.json`.
It is updated whenever you add or preview a file. Load my jobs finds a job's
file in three ways, in this order:
1. **Its list of files.** Files it has previewed are added from where they are.
2. **Folders used before.** For other jobs, it looks beside your inputs in
   folders you have added files from, for the `*.inkly.json` receipt that
   records that job, and adds the input next to it.
3. **Your choice.** For jobs still not found, for example ones submitted before
   Desktop kept its list, it asks:
   - **Find files…** searches a folder you pick and its subfolders (up to 8
     levels; hidden, system and tool folders are skipped, and links are not
     followed). Folders where inputs are found are remembered, so the next
     Load my jobs finds them without asking.
   - **Make copies** is for jobs whose files are not on this computer, such as
     ones submitted from another computer. Desktop asks once for a folder for
     copies. Each job gets its own subfolder there, named after the input and
     the start of its bundle ID, with an exact copy of the input taken from the
     HPC and its receipt. Its result is returned to `returned\` in that
     subfolder.
   - **Skip** leaves those jobs out this time.

Desktop never overwrites a file, and the search reads only receipts named
after the inputs it is looking for.

A file that Run again gave a newer job is not loaded twice; the older job stays
in Job history.

**Job history**, in the top bar, lists every recorded Inkly job for the
configured connection, newest first, with its submission and end times shown
in this computer's time:
- **Show** filters by state. Finished and cancelled is the default; Completed;
  Failed, timed out or out of memory; Cancelled; Pending or running; Needs
  checking; Not submitted; All.
- **Submitted** limits the period: the last 7, 30 or 90 days, or all time.
- **File or job ID** matches part of a file name or a whole job ID.

Select jobs and choose **Add selected to list** to work with them, for example
to return an older result. **Change folder…** changes where copies are kept.

The list comes from the handoff receipts on the HPC, and Slurm is asked only
about the job IDs they record, so jobs submitted outside Inkly never appear. A
job with no accounting record shows UNKNOWN; that does not prove it never ran.
Both buttons work while watching, on the open connection, and need an updated
HPC copy of Inkly. With an older copy, Desktop says so and Add files works as
before.

Keep the `*.inkly.json` receipts beside the original inputs. Reopen the app,
choose **Load my jobs**, or add the same files and choose **Preview files**, to
read their saved handoffs.
**Check & return results** watches existing jobs and downloads successful
results; it signs in again if the connection has closed, and it never submits
a prepared job.

Previously submitted jobs are not submitted again. Already returned logs are
verified and left unchanged. A job whose state is unknown or whose submission
is uncertain, changed resource previews and output collisions stop the workflow
for inspection. Jobs that are running or have ended (completed, failed, timed
out, out of memory or cancelled) do not stop other files from being submitted,
so a failure loaded with Load my jobs can stay in the list. While watching, a
job that ended without a result is reported once in Activity, with a pointer
to Job details, and the other jobs keep being watched.

## Cancel a pending job or run a file again

**Cancel job** and **Run again** act on the **selected rows**; the other
buttons use the whole list. Select rows with a click (Ctrl or Shift for
several). Each asks for confirmation first, and the confirmation defaults to
No.

**Cancel job** is available when every selected row shows **PENDING** (waiting
in the Slurm queue), both while Desktop is watching jobs and when it is idle.
While watching, the cancel happens on the connection that is already open, so
there is no new sign-in and watching continues. When idle, Desktop reuses its
connection if it is still open, or signs in again if it is not.

Desktop reads each selected job's current state on the HPC first and cancels
only jobs that are still waiting (`scancel --state=PENDING`), so a job that
started in the meantime keeps running. A cancelled row shows **CANCELLED**.
For any other selected job, Desktop shows a short notice instead of an error
and keeps the connection open. For example, "job 2406955 already completed,
so there was nothing to cancel", or that a job has already started, was
already cancelled, already ended or was never submitted. Your input files and
receipts are not changed, and a cancelled job is never resubmitted. **Check &
return results** treats CANCELLED rows as finished and keeps watching the
others.

Cancelling needs a copy of Inkly on the HPC that supports it. If a job's saved
handoff uses an older copy, Desktop says so in a notice and cancels nothing;
the notice includes the `scancel` command you can run on the HPC yourself.

**Run again** prepares a selected file as a **new** job. It is available for
rows that are **CANCELLED**, **FAILED**, **TIMEOUT**, **OUT_OF_MEMORY**,
**NODE_FAIL** or **PREEMPTED**, and for **PREPARED** rows whose input was
edited after preview. Desktop checks each state on the HPC first and changes
nothing if a selected job is pending, running, finished or unconfirmed. Then
it keeps a copy of each old receipt beside the input (`….inkly.json.rerun-….bak`)
and prepares the file as it is now. The new job keeps the file's original HPC
connection and results folder and uses your current time-limit setting.
**Run again never submits:** the list is previewed again, and you choose
**Submit prepared jobs** as usual. It refuses to run if a returned log with the
same name already exists, so earlier results are never overwritten. Completed
jobs are not run again; copy the input to a new file name instead.

If you edit an input after it was prepared, Preview shows the row as, for
example, **PREPARED (input edited)** instead of stopping. The recorded job is
left as it is, and the edited file is never submitted under the old
preparation. To submit the edits, select the row and choose **Run again**. A
pending or running job must be cancelled or finish first.

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

## Questions and support

The line under the window title, **Questions or issues? Contact:
rrv9177@uncw.edu**, opens a new email in your default email app. If no email
app is set up, Desktop shows the address and copies it to the clipboard so you
can write from any email service. Describe what you clicked and the message
shown, but never send passwords, MFA codes, SSH keys or unpublished research
files.

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

## Publish a release (maintainers)

Releases are built by GitHub, never on a personal computer. Set
`__version__` in `inkly/__init__.py` (for example `0.3.0`), merge it, then
create the tag `desktop-v0.3.0` on that reviewed `dev` or `main` commit. The
**Release Desktop** workflow (`.github/workflows/release-desktop.yml`) then:

1. refuses tags that are not `desktop-vX.Y.Z`, commits that are not on `dev` or
   `main`, a version that differs from `inkly/__init__.py`, or a release that
   already exists (releases are never replaced);
2. runs the Windows tests, builds the EXE and checks the packaged window;
3. publishes a **pre-release** with `InklyDesktop-<version>-windows.zip`,
   `SHA256SUMS.txt`, release notes and a signed build-provenance attestation.
   Anyone can check the attestation with
   `gh attestation verify InklyDesktop.exe --repo thealice-lab/hpc-ink-setup`.

Every release starts as a pre-release. After researcher acceptance and a matching
HPC endpoint check, mark it as a full release on the GitHub release page. The EXE
is not Authenticode-signed; removing the SmartScreen warning needs a
code-signing certificate (for example through UNCW IT). A pull request that
changes the workflow runs the same steps as a dry run and publishes nothing.

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

Cancel and Run again add `desktop_cancel` (approved, requested or unsupported;
count or job ID) and `desktop_rerun` (approved or prepared; count) events.
Older HPC copies ignore event types they do not know.

The Windows executable and the HPC `inkly` package must be upgraded together
for remote event delivery. Until the new HPC endpoint is installed, treat
remote delivery as unverified. The Desktop logs can be used locally without
any Cuttlefish deployment.

**Remaining release validation:** rebuild the Windows executable, run the
packaged GUI with actual OpenSSH/MFA, verify events on Cuttlefish under the
real lab ACL, and check a different lab member's permitted access and an
outsider's denial. Do not alter shared permissions or submit a live job just
to test logging.
