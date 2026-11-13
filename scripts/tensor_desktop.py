#!/usr/bin/env python3
"""Inkly Desktop: a windowed companion for Gaussian file handoffs."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

# Source launches and the packaged app use the same stdlib-only diagnostic code.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tensor_picker as picker
from tensor_desktop_backend import DesktopController


def bundled_askpass():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "build"))
    path = root / "InklySshAskpass.exe"
    return path if path.is_file() else None


class DesktopApp:
    def __init__(self, root, controller_factory=DesktopController, settings_path=None):
        self.root = root
        self.root.title("Inkly Desktop")
        self.root.geometry("1120x760")
        self.root.minsize(940, 640)
        self.path = settings_path or picker.config_path()
        self.settings = {
            "host": "",
            "ink_path": "~/.inkly/bin/ink",
            "auth": "keyboard-interactive",
            "hours": 1,
        }
        self.load_error = None
        if self.path.exists():
            try:
                self.settings.update(
                    picker.validate_settings(
                        json.loads(self.path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError, TypeError, KeyError) as error:
                self.load_error = str(error)
        self.files = []
        self.rows = {}
        self.review = None
        self.recovery_request = None
        self.busy = False
        self.closed = False
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.controller = controller_factory(
            lambda kind, data: self.events.put((kind, data)), askpass=bundled_askpass()
        )
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.timer = self.root.after(80, self.drain)
        if self.load_error:
            self.log("Saved connection needs attention: " + self.load_error)

    def build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure(
            "TLabel", background="#f3f6fa", foreground="#25334a", font=("Segoe UI", 10)
        )
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
        style.configure("Accent.TButton", background="#147c8a", foreground="white")
        style.map(
            "Accent.TButton",
            background=[("active", "#126b78"), ("disabled", "#d1dbe0")],
            foreground=[("disabled", "#6b7685")],
        )
        style.configure(
            "Treeview",
            font=("Segoe UI", 10),
            rowheight=30,
            background="white",
            fieldbackground="white",
        )
        style.configure(
            "Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=(6, 8)
        )
        self.root.configure(background="#f3f6fa")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        banner = tk.Frame(self.root, background="#142b45", padx=24, pady=18)
        banner.grid(row=0, column=0, sticky="ew")
        tk.Label(
            banner,
            text="Inkly Desktop",
            background="#142b45",
            foreground="white",
            font=("Segoe UI", 24, "bold"),
        ).pack(anchor="w")
        tk.Label(
            banner,
            text="Gaussian calculations on Cuttlefish",
            background="#142b45",
            foreground="#bed6e8",
            font=("Segoe UI", 11),
        ).pack(anchor="w", pady=(4, 0))
        tools = ttk.Frame(self.root, padding=(22, 14))
        tools.grid(row=1, column=0, sticky="ew")
        tools.columnconfigure(0, weight=1)
        self.connection = ttk.Label(tools, text=self.connection_text())
        self.connection.grid(row=0, column=0, sticky="w")
        self.settings_button = ttk.Button(
            tools, text="Connection settings", command=self.configure
        )
        self.settings_button.grid(row=0, column=1, rowspan=2)
        ttk.Label(
            tools,
            text="1  Choose inputs     →     2  Preview resources     →     3  Submit and return results",
        ).grid(row=1, column=0, sticky="w", pady=(9, 0))

        content = ttk.Frame(self.root, padding=(22, 0, 22, 12))
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=3)
        content.rowconfigure(6, weight=1)
        bar = ttk.Frame(content)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.add_button = ttk.Button(bar, text="+ Add files", command=self.add_files)
        self.add_button.pack(side="left")
        self.folder_button = ttk.Button(bar, text="Add folder", command=self.add_folder)
        self.folder_button.pack(side="left", padx=8)
        self.remove_button = ttk.Button(
            bar, text="Remove selected", command=self.remove
        )
        self.remove_button.pack(side="left")
        self.clear_button = ttk.Button(bar, text="Clear list", command=self.clear)
        self.clear_button.pack(side="left", padx=8)
        self.count = ttk.Label(bar, text="No files added")
        self.count.pack(side="right")
        ttk.Label(
            content,
            text="Add the .gjf or .com files your preparation tool created. Actions apply to all files in this list.",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))
        frame = ttk.Frame(content)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = (
            "file",
            "cpus",
            "gaussian",
            "slurm",
            "hours",
            "stages",
            "state",
            "job",
        )
        self.table = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended"
        )
        heading_font = font.Font(family="Segoe UI", size=10, weight="bold")
        for column, title, width in zip(
            columns,
            (
                "Input file",
                "CPUs",
                "Gaussian MB",
                "Slurm MiB",
                "Hours",
                "Stages",
                "Status",
                "Job ID",
            ),
            (250, 55, 110, 100, 55, 60, 140, 90),
        ):
            self.table.heading(column, text=title)
            self.table.column(
                column,
                width=max(width, heading_font.measure(title) + 26),
                minwidth=50,
                stretch=column in {"file", "state"},
                anchor="w" if column in {"file", "state"} else "center",
            )
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(
            yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set
        )
        self.table.bind("<<TreeviewSelect>>", lambda event: self.buttons())
        self.overview = ttk.Label(
            content,
            text="Preview reads exported CPU/memory settings and adds the cluster's memory headroom.",
            wraplength=1000,
        )
        self.overview.grid(row=3, column=0, sticky="w", pady=(12, 8))
        actions = ttk.Frame(content)
        actions.grid(row=4, column=0, sticky="ew")
        self.preview_button = ttk.Button(
            actions,
            text="Preview files",
            style="Accent.TButton",
            command=self.preview_files,
        )
        self.preview_button.pack(side="left")
        self.submit_button = ttk.Button(
            actions, text="Submit prepared jobs", command=self.submit
        )
        self.submit_button.pack(side="left", padx=8)
        self.monitor_button = ttk.Button(
            actions,
            text="Check & return results",
            command=lambda: self.start("monitor"),
        )
        self.monitor_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Stop watching", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.script_button = ttk.Button(
            actions, text="View script", command=self.show_script
        )
        self.script_button.pack(side="right")
        activity_bar = ttk.Frame(content)
        activity_bar.grid(row=5, column=0, sticky="ew", pady=(16, 5))
        ttk.Label(activity_bar, text="Activity", font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        self.results_button = ttk.Button(
            activity_bar, text="Open results folder", command=self.open_results
        )
        self.results_button.pack(side="right")
        self.activity = tk.Text(
            content,
            height=6,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
            background="white",
            foreground="#34445a",
            relief="flat",
            padx=10,
            pady=8,
        )
        self.activity.grid(row=6, column=0, sticky="nsew")
        footer = ttk.Frame(self.root, padding=(22, 8, 22, 12))
        footer.grid(row=3, column=0, sticky="ew")
        self.status = ttk.Label(
            footer, text="Ready. Add inputs to begin; preview does not submit jobs."
        )
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        self.buttons()

    def connection_text(self):
        return f"HPC: {self.settings['host'] or 'Set up your connection'}   •   New jobs: up to {self.settings['hours']} hour(s) each"

    def work(self):
        while True:
            action, args = self.commands.get()
            self.controller.execute(action, *args)
            if action == "close":
                return

    def start(self, action, *args):
        if self.busy:
            return
        self.busy = True
        self.controller.cancelled.clear()
        self.status.configure(
            text={
                "preview": "Preparing preview…",
                "submit": "Submitting approved jobs and watching results…",
                "monitor": "Checking recorded jobs…",
            }[action]
        )
        self.progress.start(12)
        self.buttons()
        self.commands.put((action, args))

    def drain(self):
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind == "message":
                self.log(data)
            elif kind == "row":
                self.rows[data["path"]] = data
                if self.table.exists(data["path"]):
                    r = data["resources"]
                    self.table.item(
                        data["path"],
                        values=(
                            data["name"],
                            r["cpus"],
                            r["gaussian_memory_mb"],
                            r["slurm_memory_mib"],
                            r["hours"],
                            data["stages"],
                            data["state"],
                            data["job_id"],
                        ),
                    )
            elif kind == "review":
                self.review = data
                if data:
                    overview = (
                        f"{data['count']} prepared job(s). If all start together: "
                        f"{data['cpus']} CPUs and {data['memory_mib']:,} MiB "
                        "Slurm memory. Slurm decides start times."
                    )
                    if data.get("recovered"):
                        overview += (
                            "\nRecovered handoff(s): "
                            + ", ".join(data["recovered"])
                            + ". Check for any earlier running calculation before submitting a replacement."
                        )
                    self.overview.configure(text=overview)
            elif kind == "recovery_required":
                self.recovery_request = data
            elif kind == "target":
                self.log(
                    f"Using {data['host']} with {data['ink_path']}. Saved handoffs retain their original connection and settings."
                )
            elif kind == "error":
                self.review = None
                self.log(data)
                self.status.configure(
                    text="Stopped. See the message below; recorded jobs are preserved."
                )
                messagebox.showerror("Handoff stopped", data, parent=self.root)
            elif kind == "done":
                self.busy = False
                self.progress.stop()
                if data != "close" and self.review:
                    self.status.configure(
                        text="Preview ready. Review each row before submitting."
                    )
                elif data != "close":
                    self.status.configure(
                        text="Operation finished. See Activity for results or recovery instructions."
                    )
            self.buttons()
        if self.recovery_request and not self.busy and not self.closed:
            request, self.recovery_request = self.recovery_request, None
            files = "\n".join(f"  • {name}" for name in request["files"])
            prompt = (
                "Inkly found saved recovery receipts, but the matching prepared "
                "bundles are missing on the HPC:\n\n"
                + files
                + "\n\nYou can re-prepare these inputs in their original "
                "locations. Inkly will archive each old receipt and create a new "
                "one. Your Gaussian input files will not be moved or changed. "
                "This step only creates a preview; it does not submit a Slurm job."
                + "\n\nIMPORTANT: If you previously submitted any of these "
                "calculations, check your Slurm jobs before submitting a replacement. "
                "A missing bundle does not prove that an earlier job has stopped."
                + "\n\nRe-prepare these inputs now?"
            )
            if messagebox.askyesno(
                "Recover missing HPC handoff",
                prompt,
                parent=self.root,
                default="no",
            ):
                self.start(
                    "preview",
                    request["paths"],
                    request["settings"],
                    request["bundles"],
                )
            else:
                self.log(
                    "Recovery declined. The original inputs and recovery receipts were preserved."
                )
                self.status.configure(
                    text="Recovery declined. Saved inputs and receipts are unchanged."
                )
                self.buttons()
        if not self.closed:
            self.timer = self.root.after(80, self.drain)

    def log(self, text):
        self.activity.configure(state="normal")
        self.activity.insert("end", text + "\n")
        if int(self.activity.index("end-1c").split(".")[0]) > 300:
            self.activity.delete("1.0", "50.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def buttons(self):
        for widget in (
            self.add_button,
            self.folder_button,
            self.remove_button,
            self.clear_button,
            self.settings_button,
        ):
            widget.configure(state="disabled" if self.busy else "normal")
        self.preview_button.configure(
            state="normal" if self.files and not self.busy else "disabled"
        )
        self.submit_button.configure(
            state="normal"
            if self.review and self.review["count"] and not self.busy
            else "disabled"
        )
        self.monitor_button.configure(
            state="normal"
            if self.rows and self.controller.session and not self.busy
            else "disabled"
        )
        self.stop_button.configure(state="normal" if self.busy else "disabled")
        self.script_button.configure(
            state="normal" if self.table.selection() and self.rows else "disabled"
        )
        self.results_button.configure(
            state="normal"
            if any(row.get("output") for row in self.rows.values())
            else "disabled"
        )

    def invalidate(self):
        self.review = None
        self.rows = {}
        for path in self.files:
            self.table.item(
                str(path),
                values=(path.name, "—", "—", "—", "—", "—", "Not previewed", ""),
            )
        self.overview.configure(
            text="Preview again to review the current file list and saved job settings."
        )
        self.count.configure(text=f"{len(self.files)} file(s)")
        self.buttons()

    def add_paths(self, paths):
        try:
            new = picker.selections(paths)
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot add inputs", str(error), parent=self.root)
            return
        for path in new:
            if path not in self.files:
                self.files.append(path)
                self.table.insert(
                    "",
                    "end",
                    iid=str(path),
                    values=(path.name, "—", "—", "—", "—", "—", "Not previewed", ""),
                )
        self.invalidate()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Choose Gaussian input files",
            filetypes=[("Gaussian inputs", "*.gjf *.com")],
        )
        if paths:
            self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Choose a folder containing Gaussian inputs"
        )
        if folder:
            paths = sorted(
                path
                for path in Path(folder).iterdir()
                if path.is_file() and path.suffix in {".gjf", ".com"}
            )
            if not paths:
                messagebox.showinfo(
                    "No Gaussian inputs",
                    "No .gjf or .com files were found directly in this folder.",
                    parent=self.root,
                )
            else:
                self.add_paths(paths)

    def remove(self):
        for path in self.table.selection():
            self.files.remove(Path(path))
            self.table.delete(path)
        self.invalidate()

    def clear(self):
        for path in self.table.get_children():
            self.table.delete(path)
        self.files.clear()
        self.invalidate()

    def configure(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Connection settings")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Use your own HPC account. Passwords and MFA belong in the separate sign-in dialog.",
            wraplength=540,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        values = {
            key: tk.StringVar(value=str(self.settings[key]))
            for key in ("host", "ink_path", "auth", "hours")
        }
        for index, (key, label) in enumerate(
            (
                ("host", "Username@host or SSH alias"),
                ("ink_path", "HPC Inkly launcher"),
                ("auth", "Authentication"),
                ("hours", "Maximum hours for new jobs"),
            ),
            1,
        ):
            ttk.Label(frame, text=label).grid(
                row=index, column=0, sticky="w", padx=(0, 12), pady=7
            )
            if key == "auth":
                widget = ttk.Combobox(
                    frame,
                    textvariable=values[key],
                    values=("keyboard-interactive", "configured"),
                    state="readonly",
                    width=38,
                )
            elif key == "hours":
                widget = ttk.Spinbox(
                    frame, from_=1, to=336, textvariable=values[key], width=10
                )
            else:
                widget = ttk.Entry(frame, textvariable=values[key], width=43)
            widget.grid(row=index, column=1, sticky="w")
        ttk.Label(
            frame,
            text="keyboard-interactive uses password/MFA; configured uses your existing SSH settings.\nExisting handoffs retain the settings in their saved receipts.",
            wraplength=540,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=16)

        def save():
            try:
                settings = {key: value.get().strip() for key, value in values.items()}
                settings["hours"] = int(settings["hours"])
                picker.save_settings(self.path, settings)
            except (OSError, ValueError, KeyError, TypeError) as error:
                messagebox.showerror(
                    "Check connection settings", str(error), parent=dialog
                )
                return
            self.settings = settings
            self.connection.configure(text=self.connection_text())
            self.invalidate()
            dialog.destroy()

        ttk.Button(
            frame, text="Save settings", command=save, style="Accent.TButton"
        ).grid(row=6, column=1, sticky="e")

    def preview_files(self):
        try:
            picker.validate_settings(self.settings)
        except (ValueError, KeyError, TypeError):
            self.configure()
            return
        self.invalidate()
        self.start("preview", list(self.files), dict(self.settings))

    def submit(self):
        if not self.review or self.busy:
            return
        review = self.review
        details = "\n".join(
            f"{job['name']}: {job['cpus']} CPUs, {job['slurm_memory_mib']} MiB, {job['hours']} hour(s)"
            for job in review["jobs"][:8]
        )
        if review["count"] > 8:
            details += f"\n…and {review['count'] - 8} more files shown in the table."
        text = f"Submit {review['count']} independent calculations to the HPC?\n\n{details}\n\nIf all run together: {review['cpus']} CPUs and {review['memory_mib']:,} MiB Slurm memory.\nSlurm decides when jobs start. Closing the app does not cancel them."
        if review.get("recovered"):
            text += (
                "\n\nRECOVERED HANDOFF WARNING: "
                + ", ".join(review["recovered"])
                + ". Before submitting a replacement, verify that any earlier "
                "calculation associated with the archived receipt is no longer "
                "running. A missing bundle does not prove that the old job ended."
                + "\n\nHave you checked and confirmed that it is safe to submit?"
            )
        if messagebox.askyesno(
            "Confirm HPC submission", text, parent=self.root, default="no"
        ):
            self.start("submit", review["id"], True)

    def stop(self):
        self.controller.stop()
        self.review = None
        self.status.configure(
            text="Stopping the connection. Submitted HPC jobs continue."
        )
        self.buttons()

    def show_script(self):
        selected = self.table.selection()
        if not selected:
            return
        row = self.rows.get(selected[0])
        if not row or not row.get("script"):
            messagebox.showinfo(
                "Script unavailable",
                "Preview an unsubmitted input to see its generated SBATCH script.",
                parent=self.root,
            )
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Generated SBATCH — " + row["name"])
        dialog.geometry("900x650")
        text = tk.Text(dialog, wrap="none", font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        text.insert("1.0", row["script"])
        text.configure(state="disabled")

    def open_results(self):
        selected = set(self.table.selection())
        candidates = [
            row for path, row in self.rows.items() if not selected or path in selected
        ]
        output = next(
            (Path(row["output"]).parent for row in candidates if row.get("output")),
            None,
        )
        if output and output.is_dir():
            os.startfile(str(output))
        else:
            messagebox.showinfo(
                "Results",
                "Select a row marked RETURNED, or clear the row selection.",
                parent=self.root,
            )

    def close(self):
        if self.busy and not messagebox.askyesno(
            "Close Inkly Desktop?",
            "Any submitted HPC jobs will continue. Keep the receipts and preview the same files later to resume.",
            parent=self.root,
            default="no",
        ):
            return
        self.closed = True
        if self.busy:
            self.controller.stop()
        self.commands.put(("close", ()))
        self.worker.join(timeout=2)
        self.root.after_cancel(self.timer)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name == "nt":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    # External OpenSSH/PowerShell must use system DLL lookup, not frozen DLLs.
    if os.name == "nt" and getattr(sys, "frozen", False):
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    app = DesktopApp(root)
    if args.smoke_test:
        # Local build verification only: draw the real window and invoke no SSH.
        root.update()
        ssh = shutil.which("ssh")
        checked = (
            subprocess.run(
                [ssh, "-V"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
            )
            if ssh
            else None
        )
        args.smoke_test.write_text(
            json.dumps(
                {
                    "window": root.title(),
                    "width": root.winfo_width(),
                    "height": root.winfo_height(),
                    "ssh": bool(checked and checked.returncode == 0),
                    "askpass": bool(bundled_askpass()),
                    "frozen": bool(getattr(sys, "frozen", False)),
                }
            ),
            encoding="utf-8",
        )
        app.close()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
