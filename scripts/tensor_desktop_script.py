"""View script: read, change or replace a prepared job's SBATCH script.

Any line may change and the whole script may be replaced. The HPC checks a
saved script with Slurm (nothing is submitted) and lists anything risky;
Reset to generated brings back the script Inkly prepared.
"""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tensor_desktop_status import (
    MAX_SCRIPT,
    SCRIPT_WARNING as WARNING,
    diff_text,
    editable,
    read_script_file,
)


class ScriptWindow:
    def __init__(self, app, path, row):
        self.app, self.path, self.row = app, path, row
        self.warned = self.editing = self.saving = False
        self.review_window = None
        window = self.window = tk.Toplevel(app.root)
        window.title("Job script — " + row["name"])
        window.geometry("920x720")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.note = ttk.Label(window, padding=(10, 8), wraplength=880)
        self.note.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.text = tk.Text(window, wrap="none", font=("Consolas", 10), undo=True)
        self.text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(window, orient="vertical", command=self.text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.findings = tk.Text(window, height=7, wrap="word", font=("Segoe UI", 9))
        self.findings.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        actions = ttk.Frame(window, padding=(10, 8))
        actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.edit_button = ttk.Button(actions, text="Edit…", command=self.edit)
        self.edit_button.pack(side="left")
        self.load_button = ttk.Button(
            actions, text="Load from file…", command=self.load
        )
        self.load_button.pack(side="left", padx=8)
        self.reset_button = ttk.Button(
            actions, text="Reset to generated", command=self.reset
        )
        self.reset_button.pack(side="left")
        ttk.Button(actions, text="Close", command=self.close).pack(side="right")
        self.save_button = ttk.Button(
            actions, text="Save on the HPC…", command=self.save
        )
        self.save_button.pack(side="right", padx=8)
        self.show(row)

    # What is shown -------------------------------------------------------

    def generated(self):
        if self.row.get("script_edited"):
            return self.row.get("generated_script") or ""
        return self.row.get("script") or ""

    def current(self):
        return self.text.get("1.0", "end-1c")

    def set_text(self, value):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        if not self.editing:
            self.text.configure(state="disabled")

    def set_findings(self, value):
        self.findings.configure(state="normal")
        self.findings.delete("1.0", "end")
        self.findings.insert("1.0", value)
        self.findings.configure(state="disabled")

    def show(self, row, findings=None):
        self.row, self.editing = row, False
        self.set_text(row.get("script") or "")
        if not editable(row):
            note = (
                "This job was submitted or its input changed, so its script can "
                "only be viewed."
                if row.get("script_sha256")
                else "View only. Preview the file again to edit its job script."
            )
        elif row.get("script_edited"):
            note = (
                "This is your edited job script, saved on the HPC. Choose Edit… "
                "to change it, or Reset to generated to go back to Inkly's script."
            )
        else:
            note = (
                "This is the job script Inkly generated. Choose Edit… to change "
                "it or Load from file… to use your own script."
            )
        self.note.configure(text=note)
        self.set_findings(findings or "")
        self.buttons()

    def buttons(self):
        can = editable(self.row) and not self.saving and not self.app.script_unsupported
        for button, enabled in (
            (self.edit_button, can and not self.editing),
            (self.load_button, can),
            (
                self.reset_button,
                can and (self.row.get("script_edited") or self.editing),
            ),
            (self.save_button, can and self.editing),
        ):
            button.configure(state="normal" if enabled else "disabled")

    # Actions --------------------------------------------------------------

    def allowed(self):
        """Warn once before the first change; refuse where saving cannot work."""
        if self.app.script_unsupported:
            messagebox.showinfo(
                "Update Inkly on the HPC",
                "The Inkly copy on the HPC is too old to save job script changes. "
                "Update it from Connection settings, then open View script again.",
                parent=self.window,
            )
            return False
        if self.warned:
            return True
        log = Path(self.row["name"]).with_suffix(".log").name
        self.warned = messagebox.askyesno(
            "Change the job script?",
            WARNING.format(log=log),
            parent=self.window,
            default="no",
        )
        return self.warned

    def start_editing(self):
        self.editing = True
        self.text.configure(state="normal")
        self.note.configure(
            text="Editing. Nothing changes on the HPC until you choose Save on "
            "the HPC…, which shows what changed first."
        )
        self.buttons()

    def edit(self):
        if self.allowed():
            self.start_editing()

    def load(self):
        if not self.allowed():
            return
        chosen = filedialog.askopenfilename(
            parent=self.window,
            title="Choose your job script",
            filetypes=[
                ("Job scripts", "*.sh *.sbatch *.slurm *.job"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return
        try:
            text = read_script_file(chosen)
        except (OSError, ValueError) as error:
            messagebox.showerror("Script not loaded", str(error), parent=self.window)
            return
        self.start_editing()
        self.set_text(text)
        self.note.configure(
            text=f"Loaded {Path(chosen).name}. Review it, then choose Save on the "
            "HPC… to use it for this job."
        )

    def reset(self):
        stored = self.row.get("script_edited")
        if not messagebox.askyesno(
            "Reset to generated",
            "Replace the text with the job script Inkly generated?"
            + (
                " Your edited script on the HPC is replaced as soon as you confirm."
                if stored
                else " Your unsaved changes are discarded."
            ),
            parent=self.window,
            default="no",
        ):
            return
        if stored:
            self.send(self.generated())
        else:
            self.show(self.row)

    def save(self):
        script = self.current()
        if script == (self.row.get("script") or ""):
            messagebox.showinfo(
                "Nothing to save",
                "The script is the same as the one saved on the HPC.",
                parent=self.window,
            )
            return
        if len(script.encode("utf-8")) > MAX_SCRIPT:
            messagebox.showerror(
                "Script too large",
                "A job script can be at most 64 KiB.",
                parent=self.window,
            )
            return
        self.close_review()
        review = self.review_window = tk.Toplevel(self.window)
        review.title("Save this job script?")
        review.geometry("860x560")
        review.columnconfigure(0, weight=1)
        review.rowconfigure(1, weight=1)
        ttk.Label(
            review,
            padding=(10, 8),
            wraplength=820,
            text="Changes against the script Inkly generated. The HPC asks Slurm "
            "whether it would accept this script, then saves it for this job. "
            "Nothing is submitted.",
        ).grid(row=0, column=0, sticky="ew")
        changes = tk.Text(review, wrap="none", font=("Consolas", 10))
        changes.grid(row=1, column=0, sticky="nsew")
        changes.insert("1.0", diff_text(self.generated(), script))
        changes.configure(state="disabled")
        actions = ttk.Frame(review, padding=(10, 8))
        actions.grid(row=2, column=0, sticky="ew")
        ttk.Button(actions, text="Keep editing", command=self.close_review).pack(
            side="right"
        )
        self.confirm_button = ttk.Button(
            actions, text="Save on the HPC", command=lambda: self.send(script)
        )
        self.confirm_button.pack(side="right", padx=8)

    def close_review(self):
        if self.review_window is not None and self.review_window.winfo_exists():
            self.review_window.destroy()
        self.review_window = None

    def send(self, script):
        self.close_review()
        if not self.app.save_script(self.path, script, self.row.get("script_sha256")):
            return
        self.saving = True
        self.note.configure(text="Saving on the HPC… Nothing is submitted.")
        self.buttons()

    # Results from the HPC -------------------------------------------------

    def result(self, data):
        self.saving = False
        if data["status"] == "saved":
            row = self.app.rows.get(self.path) or {
                **self.row,
                "script": data["script"],
                "script_sha256": data["script_sha256"],
                "script_edited": data["edited"],
                "generated_script": data["generated_script"],
            }
            warnings = data.get("warnings") or []
            if not data["edited"]:
                findings = "Restored the script Inkly generated."
            elif warnings:
                findings = "Saved. Please check:\n" + "\n".join(
                    "• " + warning for warning in warnings
                )
            else:
                findings = "Saved. Slurm would accept this script; nothing looks risky."
            self.show(row, findings + "\nNothing was submitted.")
            return
        # Not saved: keep the text so it can be fixed or copied.
        self.set_findings("Not saved. " + data["message"])
        self.note.configure(text="Not saved. Your text is still here.")
        self.buttons()

    def finished(self):
        """The save step ended without a result (for example a lost connection)."""
        if self.saving:
            self.saving = False
            self.set_findings("Not saved. See Activity for what happened.")
            self.buttons()

    def exists(self):
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        if (
            self.editing
            and self.current() != (self.row.get("script") or "")
            and not messagebox.askyesno(
                "Close without saving?",
                "Your changes to the job script are not saved on the HPC. Close "
                "and discard them?",
                parent=self.window,
                default="no",
            )
        ):
            return
        self.close_review()
        if self.app.script_window is self:
            self.app.script_window = None
        self.window.destroy()
