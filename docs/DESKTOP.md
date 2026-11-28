# Inkly Desktop for Windows

Open **InklyDesktop.exe**. The packaged application includes Python, its window
toolkit and the generic SSH sign-in dialog. Researchers do not need to type a
command or install Python to use this build. Windows OpenSSH Client, access to
the site's network/VPN and an account with Gaussian access are still required.
If your HPC account does not have Inkly yet, or has an older copy, Desktop can
install or update it for you: see [Set up Inkly on the HPC](#set-up-inkly-on-the-hpc).

This is a development build for workflow review, not a signed installer.
Follow your institution's software distribution process; code signing and
automatic updates are not part of this change.

## Download and open Inkly Desktop

The EXE is **not committed to Git**. The helper pins the
[Inkly Desktop 0.6.0 (preview)](https://github.com/thealice-lab/hpc-ink-setup/releases/tag/desktop-v0.6.0)
release, built by GitHub Actions from commit
`6f95d03a2e7740de29e09b69dff71b50eafc388d`. It never silently selects a newer
build. Do not substitute another EXE or bypass a hash failure.

1. In this checkout, double-click **Get-InklyDesktop-Test.cmd** at the repository
   root. Windows PowerShell 5.1 is required. Python and administrator rights
   are not needed, and the helper does not change persistent execution policy
   or install Windows components.
2. It downloads `InklyDesktop-0.6.0-windows.zip` from the release. Release
   downloads are public: no GitHub sign-in, and they do not expire. If the
   download is blocked (for example by a proxy), the helper opens the release
   page instead: download the ZIP there, return to the helper and press Enter,
   or paste the ZIP's full path. Enter **Q** to cancel. The helper never
   collects credentials and never chooses another build.
3. It extracts only `InklyDesktop.exe`, verifies its SHA-256, and places it at
   `%LOCALAPPDATA%\Inkly\DesktopBuilds\0.6.0\InklyDesktop.exe`. This is
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
5813D970CEDA56656BCDEFC2A0BEC5760ADF3EBAC35475629019777A3A357CD5
```

Anyone can also confirm that GitHub Actions built it from this repository with
`gh attestation verify InklyDesktop.exe --repo thealice-lab/hpc-ink-setup`.

For standalone use without a clone, download both `Get-InklyDesktop-Test.cmd`
and `scripts/windows/get_inklydesktop_test.ps1` from the same reviewed source
revision and keep them beside one another. The `.cmd` supports both layouts.
For an already downloaded ZIP, or a local check without launching the full GUI:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\get_inklydesktop_test.ps1 -ArtifactZip 'C:\path\to\InklyDesktop-0.6.0-windows.zip' -NoLaunch
```

A ZIP with the wrong contents stops verification, and a bad existing cached EXE
is left untouched for review. If OpenSSH is missing, enable **OpenSSH Client** in
Windows Optional Features according to your institution's policy and retry.
Do not override Windows security warnings for an unverified source. Opening
`InklyDesktop.exe` straight from a downloaded ZIP can show a Windows SmartScreen
warning because the build is not Authenticode-signed; the helper checks the
EXE's exact SHA-256 before running it.

**Newer releases:** the packaged app checks the public GitHub release list in
the background, at most once a day. If a newer Desktop release is available
(including a preview), a link under the contact line opens that release's
download page. Nothing is downloaded or installed automatically. The check
needs no GitHub sign-in or HPC connection and is silent when unavailable.
To turn it off, untick **Check for new Desktop releases** in Connection
settings; the choice is saved immediately on this computer. Administrators can
also set `INKLY_DESKTOP_UPDATE_CHECK=0`. Source runs and the packaged smoke
test do not check for releases.

Maintainers: after publishing a new release (see **Publish a release** below),
update the helper's version, source commit and EXE SHA-256 (from the release's
`SHA256SUMS.txt`) together, along with this page, the README and
`tests/test_windows_desktop_download.py`. Do not commit ZIP/EXE binaries or
treat this helper as an HPC deployment tool.

## Set up Inkly on the HPC

From Inkly Desktop 0.6.0, you do not need the HPC command line to install
Inkly. Desktop installs or updates **full Inkly** on your own HPC account, the
same as cloning the repository and running `bash setup.sh` yourself. Afterwards
both Desktop and `ink` in an HPC terminal work.

**Before you start, sign in to your HPC account once the usual way**, for
example with `ssh <username>@<host>` in a terminal, as your administrator
describes when they create the account. That first sign-in makes this
computer trust the HPC's host key and completes any first-login steps on the
account (such as a password change). Desktop does not do these for you. If you
have never signed in from this computer, use **First sign-in** (see
[First sign-in on a new computer](#first-sign-in-on-a-new-computer)).

Open **Connection settings**. The **Inkly on the HPC** section shows what
**Test connection** last found and has three buttons:

- **Set up Inkly on the HPC** (it reads **Update** when the HPC copy is older
  than this Desktop) installs this Desktop's release. If the HPC is already
  up to date, nothing is changed.
- **Repair** runs `setup.sh` again even when the HPC is up to date, for
  example after files in `~/.inkly` were removed by mistake.
- **First sign-in** is for a computer that has not connected to this HPC
  before (see below).

Each button first saves the settings shown. You don't have to look for this
section: when Test connection, Load my jobs, Job history or a preview finds
Inkly missing on the HPC, or too old for this Desktop, Desktop asks whether
to set it up now. An older copy that still offers everything this Desktop
needs keeps working; the Test connection message then says an update is
available.

### What setup does on the HPC

Before anything runs, Desktop shows exactly what will happen and waits for
**Set up**, **Update** or **Repair**. It then signs in **once** (password
and Duo as usual), and the same connection continues as your normal Desktop
session. Setup:

- downloads this Desktop's exact release commit from
  github.com/thealice-lab/hpc-ink-setup into `~/.inkly/src/hpc-ink-setup`
  (public: no GitHub account is needed), then runs its own `setup.sh`;
- creates or reuses `~/.inkly`: the Python environment (`venv`), Inkly itself
  (`lib`), the `ink` launcher (`bin`), and your settings and history;
- installs Python packages from PyPI, and the Gaussian docs scraper in
  `~/.inkly/src/gaussian-docs-scraper`;
- adds `~/.inkly/bin` to `PATH` in `~/.bashrc` if it is not there yet, so `ink`
  works in an HPC terminal after a new login.

It runs **no Slurm commands**, **deletes nothing**, never downgrades a newer
HPC copy and never overwrites your own changes. It usually takes under a
minute, and each step appears in Activity. A full log stays on the HPC in
`~/.inkly/logs/desktop-setup-<time>.log`. Your own clone in
`~/hpc-ink-setup`, if you have one, is not used or changed.

**Shared logging.** In a new install, Inkly's default configuration shares
your Inkly chats with your lab's Unix group in a folder on scratch, to help
improve Inkly. The confirmation explains this and has a checkbox to turn it
off. The checkbox only affects a new `~/.inkly/config.toml`; an existing one
is kept as it is. You can change it later in that file. Chats may contain
personal details, cluster information and unpublished work, so never enter
passwords, credentials, tokens or private keys.

**After setup**, if Desktop was using another launcher (for example an older
copy elsewhere), it asks whether to use `~/.inkly/bin/ink` for new jobs.
Saved jobs keep the launcher recorded in their receipts, so you can still
check and return them.

### If setup stops

The message says what happened, and running setup again is always safe: it
continues where it stopped. Common reasons:

| Message | What to do |
| --- | --- |
| The HPC copy is newer than this Desktop | Get the newer Inkly Desktop release. Setup never downgrades. |
| `~/.inkly/src/hpc-ink-setup` has local changes, or is another repository | Commit or move those changes, or move the folder aside, then retry. Setup never overwrites them. |
| Another Inkly setup is already running | Wait for it to finish (for example in another Desktop window), then retry. |
| Cannot reach GitHub or the Python package index | The login node needs internet access for setup. Ask your site how to install software from login nodes. |
| Python 3.9 or newer with venv is needed, or too little free space | Load a newer Python as your site describes, or free space in your home folder (1 GB is needed). |

### First sign-in on a new computer

Desktop's connections always check the HPC's SSH host key strictly. On a
computer that has never connected to this HPC, Desktop stops with a message
that the host key is not trusted, and offers **First sign-in**. That opens
plain OpenSSH in a console window, which shows the HPC's key fingerprint and
asks whether to trust it. Type `yes` only if it matches the fingerprint your
HPC site publishes, then sign in once. OpenSSH remembers the key, and Desktop
uses it from then on.

### Other HPCs and running from source

- **Other HPCs:** setup works on any Linux HPC whose login nodes have bash 4
  or newer, Git, Python 3.9 or newer with `venv`, and access to GitHub and
  PyPI. Submitting jobs also needs a supported cluster profile; today that is
  Cuttlefish. The shared-logging defaults are specific to the Cuttlefish lab
  and report "unavailable" elsewhere.
- **Running Desktop from source:** setup installs the checkout's committed
  `HEAD`, which must already be on GitHub. A packaged Desktop installs the
  commit it was built from.

## First calculation

Use **Test connection** beside Connection settings before adding inputs. It
signs in through the configured SSH connection and asks the Inkly endpoint for
its version and supported handoff operations. A successful check keeps the
connection open for Preview. It does not prepare a bundle or run a Slurm
command. The result says whether the HPC copy of Inkly offers every operation
this Desktop needs and names optional features, such as Cancel job, that stay
unavailable until that copy is updated. An HPC copy from an earlier release is
fine when it offers those operations. When Inkly is missing or too old, Desktop
offers to [set it up](#set-up-inkly-on-the-hpc) instead. This checks access to the endpoint, not
Gaussian runtime availability, scheduler capacity or whether a calculation will
succeed.

1. **Connection settings:** enter your own username/host, the HPC Inkly launcher
   (`~/.inkly/bin/ink` after [Set up Inkly on the HPC](#set-up-inkly-on-the-hpc)),
   authentication method and maximum hours per new job. Settings stay in `%LOCALAPPDATA%\Inkly\tensor-picker.json`. Existing
   file-picker settings are reused. Never enter credentials in these fields.
2. **Add files** selects one or several Gaussian `.gjf`/`.com` exports.
   **Add folder** adds inputs directly in that folder, without searching
   subfolders. Begin with one or two representative inputs.
3. **Preview files** connects through native OpenSSH and prepares the selected
   inputs. Complete the separate password/MFA dialog when prompted. The table
   shows CPU count, Gaussian memory, total Slurm memory, time limit and stage
   count. **Preview never submits a job.** To ask for other CPUs or memory, see
   [Change CPUs and memory](#change-cpus-and-memory-before-submitting).
4. Select a row and use **View script** to inspect its generated SBATCH script,
   or to change it before submitting (see
   [Change a job's script](#change-a-jobs-script)). Choose **Submit prepared
   jobs** to review the job count and combined resource request. The
   confirmation defaults to No. Confirm only the jobs you intend to run. Every
   file in the table is included; remove files before preview to narrow the
   selection. Each input becomes one independent Slurm job.
5. The app watches all approved jobs and retrieves successful, checksum-verified
   logs. A **RETURNED** row has a verified local result. Select that row and
   choose **Open results folder**, then import the logs in TENSOR's
   **Analysis & 3D view → Add logs**, or use the researcher's analysis tools.

While the app watches, Windows shows a notification when a job returns or
ends without a result (failed, timed out, out of memory and similar), and the
taskbar button flashes until you bring Desktop forward. Jobs that finish at the
same check share one notification. Nothing is shown while Desktop is the window
in front, because the table already shows it, and cancelled jobs never notify.
Notifications name the input files, and Windows' own notification settings
apply. To turn them off, untick **Notify me when watched jobs finish** beside
Activity; the choice is kept in `%LOCALAPPDATA%\Inkly\desktop-preferences.json`.
After the first notification, Desktop's icon stays in the notification area
until the app closes.

You can keep working while the app watches. **Add files** or **Add folder**,
then **Preview files** and **Submit prepared jobs** for the new inputs; **Run
again**, **Cancel job** and **CPUs & memory…** work too. These run on the connection that is
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

## Change CPUs and memory before submitting

Preview takes each job's CPUs and memory from the input's own `%nprocshared`
and `%mem` lines, plus the cluster's memory headroom. To ask for something
else, select one or more **PREPARED** rows and choose **CPUs & memory…**
beside Run again:
- **CPUs:** a whole number from 1 to 1024.
- **Memory:** the total Slurm memory in GB (1 GB = 1024 MiB, as in the
  table's **Slurm MiB** column), from 1 to 4096 GB. Gaussian gets the
  cluster's usual share of it as `%mem`.

Leave a box as it is to keep that value; when the selected rows differ, it
starts empty. The time limit stays the value in Connection settings. Choose
**Prepare again**: each changed file is prepared as a **new** job with your
values, as Run again does. Desktop keeps a copy of the old receipt beside the
input (`….inkly.json.resources-….bak`), reads the list again and updates the
table and the combined request. **Nothing is submitted**: review the preview,
then choose **Submit prepared jobs**. A row that already uses the values you
entered is left as it is.

Your input file is never changed: the HPC writes the matching `%nprocshared`
and `%mem` into its own copy of the input when the job starts. Slurm still
decides whether the cluster can run the request, and one that no node can meet
fails at submission.

Only rows that have not been submitted can change. For a job that has ended,
choose **Run again** first; Run again keeps the CPUs and memory you chose for
the earlier job. For an input edited after preview, choose Run again to prepare
the edited file first. If a row's job script was edited on the HPC, Desktop
asks before changing it, because preparing again discards the edit. This works
with the HPC copy of Inkly you already have, also while the app watches other
jobs. The earlier, unsubmitted preparation stays on the HPC, where Job history
can still list it as not submitted.

## Change a job's script

**View script** can change the SBATCH script of a **PREPARED** row before it is
submitted. Any line may change, and **Load from file…** replaces the whole
script with one of your own. Submitted jobs, and rows whose input was edited
after preview, can only be viewed.

1. Choose **Edit…** or **Load from file…**. The first time, Desktop explains
   which of Inkly's lines matter and asks before you go on: they check that
   the job runs the input you prepared, start Gaussian on the CPUs Slurm gives
   the job, and write `NAME.log`, the file that **Check & return results**
   brings back.
2. Choose **Save on the HPC…**. Desktop shows what changed against the
   generated script. Nothing is sent until you confirm.
3. The HPC asks Slurm whether it would accept the script
   (`sbatch --test-only`; no job is created) and saves it. If Slurm would
   refuse it, for example because of an unknown partition, nothing is saved
   and your text stays in the window so you can fix it.
4. Saved scripts come back with any warnings, for example: the script no
   longer writes `NAME.log`, so the result cannot come back automatically;
   `--cpus-per-task` or `--mem` no longer match what Gaussian is started
   with; the time limit is longer than the partition allows; or `--array`,
   `--output` or `--chdir` are set. Warnings never stop a save; you decide.

**Reset to generated** brings back the script Inkly prepared. If an edited
script is already saved on the HPC, it is replaced as soon as you confirm.

Edited rows show **(script edited)** in the table, and the table and the
**Submit prepared jobs** confirmation show the CPUs, memory and time limit the
edited script asks for. Where an edited script leaves memory or time to
Slurm's defaults, Desktop says so instead of a number. **Job details**, **Job
history** and the Activity line for the submission also mark jobs whose
script you edited.

For more CPUs or memory, [CPUs & memory…](#change-cpus-and-memory-before-submitting)
is safer than editing those lines by hand: it keeps Slurm's request and
Gaussian's own settings in step. It prepares the file again, so on a row whose
script you edited it asks first (**Discard script edits?**, default No).

**Run again** always prepares a fresh script from the input. If the earlier
job's script was edited, the confirmation says so; your edits are not carried
over, and the earlier script stays on the HPC with its job.

Saving a script needs a copy of Inkly on the HPC that supports it. With an
older copy, Desktop says so and changes nothing; update Inkly on the HPC from
**Connection settings**.

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

If the connection is lost while the app is open, for example because the
computer slept or the network changed, Activity says so. Jobs keep running on
the HPC: choose **Check & return results** to sign in again and continue.

### Load my jobs and Job history

**Load my jobs**, in the top bar beside Job history, fills the list from the
HPC instead of choosing files. It adds your pending and running Inkly jobs and jobs that
ended in the last 3 days, except cancelled jobs, then checks them with a normal preview, so each row
shows the job's current state. Nothing is submitted. Choose **Check & return
results** to watch them and download finished results.

Desktop keeps a small list of which input file each job belongs to, and of
the folders you add files from, in `%LOCALAPPDATA%\Inkly\desktop-jobs.json`.
It is updated whenever you add or preview a file. Load my jobs finds a job's
file in three ways, in this order:
1. **Its list of files.** Files it has previewed are located where they are.
   Matching originals take preference over generated copies. For legacy copy
   mappings that have never been checked for originals, Desktop offers a
   one-time Find files / Use copies choice.
2. **Folders used before.** For other jobs, it looks beside your inputs in
   folders you have added files from, for the `*.inkly.json` receipt that
   records that job, and adds the input next to it.
3. **Your choice.** For jobs still not found, for example ones submitted before
   Desktop kept its list, it asks:
   - **Find files…** searches a folder you pick and its subfolders (up to 8
     levels; hidden, system and tool folders are skipped, and links are not
     followed). Folders where inputs are found are remembered, so the next
     Load my jobs finds them without asking. The result names the searched
     folder and gives found/missing counts, any switches from copies to
     originals, and older handoffs left in History. If jobs remain missing,
     the prompt groups repeated names by state, shows their count and
     submission dates, and explains when a search limit was reached.
   - **Use copies / Use or make copies** reuses existing copies, or is for jobs whose files are not on this computer, such as
     ones submitted from another computer. Desktop asks once for a folder for
     copies. Each job gets its own subfolder there, named after the input and
     the start of its bundle ID, with an exact copy of the input taken from the
     HPC and its receipt. Its result is returned to `returned\` in that
     subfolder. Choosing copies, or searching once without finding an
     original, is remembered so later loads do not ask again. Copies remain
     on disk when a job is pointed at its original.
   - **Skip** leaves those jobs out this time.

Desktop never overwrites an input or receipt. Search reads only matching
sidecars and, when checking whether an ended job was replaced, a bounded set
of archived sidecars. It does not follow links. Generated copy subfolders
are remembered as one copies root instead of crowding out project folders.

A file that Run again gave a newer job is not loaded twice; the older job stays
in Job history. Discovery can also recognize a newer handoff using the known
job dates and the original's archived receipt or an existing path mapping.
A filename match alone never hides a job, and active or uncertain jobs are
not hidden by this rule. Cancelled jobs remain explicitly selectable in
Job history; they are simply excluded from default Load my jobs.

**Job history**, in the top bar, lists every recorded Inkly job for the
configured connection, newest first, with its submission and end times shown
in this computer's time:
- **Show** filters by state. Finished and cancelled is the default; Completed;
  Failed, timed out or out of memory; Cancelled; Pending or running; Needs
  checking; Not submitted; All.
- **Submitted** limits the period: the last 7, 30 or 90 days, or all time.
- **File or job ID** matches part of a file name or a whole job ID.

Select jobs and choose **Add selected to list** to work with them, for example
to return an older result. An explicit history selection may load an older
job into its own copy, preserving its receipt and result location.

The **Local file** column distinguishes originals, copies and missing or
replaced mappings. **Find files…** is always available here: select jobs to
locate, or leave the selection empty to search the currently shown jobs.
Choose a project folder to re-point mappings even when Load my jobs already
finds every job. A matching original replaces its visible copy row rather
than adding the same bundle twice. Jobs not already in the main list are not
added by this search. **Copies folder…** changes only where future copies
are kept; it is separate from finding originals.

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

**Cancel job**, **Run again** and **CPUs & memory…** act on the **selected
rows**; the other buttons use the whole list. Select rows with a click (Ctrl or Shift for
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
connection and results folder and uses your current time-limit setting. CPUs
and memory you [chose yourself](#change-cpus-and-memory-before-submitting) for
the earlier job are kept; otherwise they come from the input again.
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
fingerprint once, with **First sign-in** in Connection settings (see
[First sign-in on a new computer](#first-sign-in-on-a-new-computer)) or
through the established connection procedure in the
[Windows onboarding guide](TENSOR.md). Changing
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
CPUs & memory adds `desktop_resources` (approved or prepared; count).
Saving a job script adds `desktop_script` (saved, reset or refused, and the
number of warnings); the script itself is never logged.
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
