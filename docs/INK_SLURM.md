# Live Slurm answers in `ink`

`ink` can answer scheduler questions from live Slurm data. It only reads:
it never submits, cancels, holds or changes a job, and it cannot run any
command outside a fixed list.

## What you can ask

| Question | Plugin | Slurm command |
|---|---|---|
| What are my jobs doing? Is my job running? How much time is left? | `my_jobs` | `squeue` for your user |
| Why is my job pending? When will it start? | `my_jobs` | `squeue` for your user |
| How did my recent jobs go? Why did my job fail? How efficient were they? | `my_recent_jobs` | `sacct` for your user |
| How busy is the cluster? How many nodes or CPUs are idle? | `queue_status` | `squeue` (states only), `sinfo` |
| What partitions are there? What is the maximum or default time limit? | `node_info` | `sinfo` |

Examples:

```bash
ink "why is my job pending?"
ink "when will job 123456 start?"
ink "how efficient were my jobs this week?"
ink "show my jobs from the last 14 days"
ink "how many idle nodes are there?"
ink "what is the maximum walltime on each partition?"
```

### Your jobs now (`my_jobs`)

For each of your queued or running jobs: partition, CPUs, nodes, memory,
time used and left. For pending jobs: the reason in plain words (for
example `Priority`, `Resources`, `QOSMaxJobsPerUserLimit`) and the
scheduler's expected start time. Start times are estimates and often move
as other jobs finish or arrive. A job ID in the question puts that job
first.

### Your recent jobs (`my_recent_jobs`)

Your jobs from the last 7 days, or the number of days the question names
(up to 31). It counts how they ended and lists the most recent finished
ones with:

- the end state, exit code or signal, and run time against the time limit;
- **CPU use:** CPU time used ÷ (elapsed time × CPUs allocated);
- **memory peak:** the largest step's peak memory ÷ the job's allocated memory.

Jobs shorter than a minute get no percentages. Slurm samples memory, so
a short spike (such as the one that ends an out-of-memory job) may not
show in the peak.

### The cluster now and partition limits (`queue_status`, `node_info`)

Running and pending job counts, and for each partition the nodes and CPUs
in use, idle and unavailable. The counts cover the jobs your account can
see; clusters that hide other users' jobs show only yours. `node_info`
lists each partition's nodes, CPUs and memory per node, GPUs, maximum and
default time limit and the number of nodes a job may use. Per-partition
memory defaults and QOS limits aren't available from `sinfo`; `ink` says
so rather than guessing.

## The commands behind an answer

An answer built from live Slurm data ends with the commands that produced
it, for example:

```text
From these read-only Slurm commands:
  squeue --user $USER --format '%i|%T|%P|%r|%M|%L|%l|%S|%D|%C|%m'
```

Paste one into your terminal to see the same data with column headers.
The list is added by Inkly, not written by the model, so it is exact.

## Safety and privacy

- **Read-only allowlist.** Every command is a fixed template in
  `inkly/slurm_readonly.py`. Only `squeue`, `sinfo` and `sacct` can appear,
  and tests check that no template names a command that changes anything
  and that anything outside the list is refused.
- **No shell, no text from your question in commands.** Commands run
  without a shell. You are identified by your numeric user ID from the
  operating system. The only other values are a number of days (1 to 31)
  and numeric job IDs that `ink` read from your own accounting records.
- **Limits.** Each command has a 10 second time limit and a 1 MiB output
  cap. Variables that change Slurm's output format (`SQUEUE_FORMAT`,
  `SACCT_FORMAT`, `SLURM_TIME_FORMAT` and similar) are ignored.
- **Your jobs only.** Job details come only from your own jobs. Cluster
  answers give counts, never other users' names or jobs.
- **No job names.** `ink` never asks Slurm for job names, working folders,
  commands or comments, so they can't appear in an answer. If your
  `config.toml` shares conversations with your lab (`[logging.shared]
  archive_conversations` or `share_conversations`), the shared copy holds
  your question and `ink`'s answer, which can include your job IDs, states
  and resource figures. Raw Slurm output is never stored.

## Other clusters

The plugins never hard-code partitions or limits; they read them from
Slurm. When a command is missing (for example on a cluster that doesn't
use Slurm), times out or fails, `ink` says which command and why instead
of guessing. Older and newer Slurm versions are handled: users are
selected by user ID rather than `--me` (added in Slurm 20.11), and memory
comes from `AllocTRES`, because `ReqMem` changed meaning in Slurm 21.08.
