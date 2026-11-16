#!/usr/bin/env python3
"""Inkly Desktop: a windowed companion for Gaussian file handoffs."""

import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
import webbrowser

# Source launches and the packaged app use the same stdlib-only diagnostic code.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tensor_desktop_status as status
import tensor_picker as picker
from tensor_desktop_backend import DesktopController


def bundled_askpass():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1] / "build"))
    path = root / "InklySshAskpass.exe"
    return path if path.is_file() else None


# How often the Tk thread runs Python's cyclic garbage collector (see DesktopApp).
GC_INTERVAL_SECONDS = 10
# Stop watching waits this long for the watch to end before disconnecting.
STOP_GRACE_MS = 20000


class DesktopApp:
    def __init__(self, root, controller_factory=DesktopController, settings_path=None):
        # Tk objects may only be freed on the thread that created them. Python's
        # cyclic garbage collector runs on whichever thread triggers it, and here
        # the worker and diagnostics threads allocate constantly; Tcl aborts the
        # whole process if one of them frees a Tk object. So automatic
        # collection stays off while the window exists, and drain() collects
        # on the Tk thread instead. Reference counting still frees most memory
        # immediately.
        gc.disable()
        self.next_collection = time.monotonic() + GC_INTERVAL_SECONDS
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
        self.action = None
        # PREPARED rows displayed before an interrupted submission may be stale.
        self.unconfirmed = set()
        self.busy = False
        self.closed = False
        # Each started action gets a number, so a delayed disconnect after Stop
        # watching can never hit an action started later.
        self.run_id = 0
        self.stop_deadline = None
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
        # Slightly tighter padding offsets most of the added contact line height.
        banner = tk.Frame(self.root, background="#142b45", padx=24, pady=14)
        banner.grid(row=0, column=0, sticky="ew")
        self.banner = banner
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
        # The full address stays readable even when no email app is configured.
        self.contact = tk.Label(
            banner,
            text=status.SUPPORT_TEXT,
            background="#142b45",
            foreground="#dcefff",
            font=("Segoe UI", 10, "underline"),
            cursor="hand2",
            takefocus=1,
            highlightthickness=1,
            highlightbackground="#142b45",
            highlightcolor="white",
        )
        self.contact.pack(anchor="w", pady=(2, 0))
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.contact.bind(sequence, self.contact_support)
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
        self.jobs_button = ttk.Button(tools, text="My jobs", command=self.my_jobs)
        self.jobs_button.grid(row=0, column=3, rowspan=2, padx=(8, 0))

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
        # These act on the HPC jobs of the selected rows, not on the list itself.
        self.cancel_button = ttk.Button(
            bar, text="Cancel job", command=self.cancel_jobs
        )
        self.cancel_button.pack(side="left", padx=(16, 0))
        self.rerun_button = ttk.Button(bar, text="Run again", command=self.run_again)
        self.rerun_button.pack(side="left", padx=8)
        # Packed last so the file count gives way first in narrow windows.
        self.count = ttk.Label(bar, text="No files added")
        self.count.pack(side="right")
        self.hint = ttk.Label(
            content,
            text=(
                "Add .gjf or .com inputs. Preview, Submit and Check use the whole "
                "list; Cancel job and Run again use selected rows."
            ),
            wraplength=1000,
        )
        self.hint.grid(row=1, column=0, sticky="w", pady=(0, 8))
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

        # Wrap the summary inside narrower or scaled windows instead of clipping it.
        def wrap(event):
            for label in (self.hint, self.overview):
                label.configure(wraplength=max(300, event.width - 44))

        content.bind("<Configure>", wrap, add="+")
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
        self.action = action
        self.run_id += 1
        self.controller.cancelled.clear()
        self.controller.paused.clear()
        self.status.configure(
            text={
                "preview": "Preparing preview…",
                "submit": "Submitting approved jobs and watching results…",
                "monitor": "Checking recorded jobs…",
                "my_jobs": "Reading your Slurm jobs…",
                "cancel_jobs": "Cancelling the selected pending jobs…",
                "rerun": "Preparing the selected files again (nothing is submitted)…",
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
            elif kind == "my_jobs":
                self.show_my_jobs(data)
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
                            data["state"]
                            + (" (input edited)" if data.get("input_changed") else ""),
                            data["job_id"],
                        ),
                    )
                # A fresh HPC status read replaces any interrupted-submit doubt.
                self.unconfirmed.discard(data["path"])
                self.refresh_overview()
            elif kind == "review":
                self.review = data
                self.refresh_overview()
            elif kind == "recovery_required":
                self.recovery_request = data
            elif kind == "target":
                self.log(
                    f"Using {data['host']} with {data['ink_path']}. Saved handoffs retain their original connection and settings."
                )
            elif kind == "request_error":
                # Something asked for during the watch was not done; watching continues.
                self.log(data)
                messagebox.showerror("Not done", data, parent=self.root)
            elif kind == "notice":
                # Expected outcomes (for example a job that already completed),
                # not failures; the connection stays open.
                messagebox.showinfo("Cancel job", data, parent=self.root)
            elif kind == "error":
                self.review = None
                if self.action == "submit":
                    self.mark_unconfirmed()
                self.refresh_overview()
                self.log(data)
                self.status.configure(
                    text="Stopped. See the message below; recorded jobs are preserved."
                )
                messagebox.showerror("Handoff stopped", data, parent=self.root)
            elif kind == "done":
                if self.stop_deadline:
                    self.root.after_cancel(self.stop_deadline)
                    self.stop_deadline = None
                self.busy = False
                self.action = None
                self.progress.stop()
                self.refresh_overview()
                if data != "close" and self.review:
                    self.status.configure(
                        text="Preview ready. Review each row before submitting."
                    )
                elif data != "close":
                    self.status.configure(
                        text="Operation finished. See Activity for results or recovery instructions."
                    )
            self.buttons()
        if (
            self.recovery_request
            and (not self.busy or self.watching())
            and not self.closed
        ):
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
                arguments = (request["paths"], request["settings"], request["bundles"])
                if self.watching():
                    self.controller.request("preview", *arguments)
                else:
                    self.start("preview", *arguments)
            else:
                self.log(
                    "Recovery declined. The original inputs and recovery receipts were preserved."
                )
                self.status.configure(
                    text="Recovery declined. Saved inputs and receipts are unchanged."
                )
                self.buttons()
        if time.monotonic() >= self.next_collection:
            gc.collect()
            self.next_collection = time.monotonic() + GC_INTERVAL_SECONDS
        if not self.closed:
            self.timer = self.root.after(80, self.drain)

    def log(self, text):
        self.activity.configure(state="normal")
        self.activity.insert("end", text + "\n")
        if int(self.activity.index("end-1c").split(".")[0]) > 300:
            self.activity.delete("1.0", "50.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def refresh_overview(self):
        """Summarize the latest displayed rows, or the unconsumed preview approval."""
        rows = [self.rows[str(path)] for path in self.files if str(path) in self.rows]
        text = status.overview_text(
            self.review,
            rows,
            self.unconfirmed,
            submitting=self.busy and self.action == "submit",
        )
        waiting = sum(str(path) not in self.rows for path in self.files)
        if waiting:
            note = f"{waiting} file(s) not previewed yet: choose Preview files to check them."
            text = f"{text}\n{note}" if text else note
        if text:
            self.overview.configure(text=text)

    def mark_unconfirmed(self):
        # An interrupted submit may have reached sbatch, so a PREPARED row
        # shown before it is no longer proof that nothing was submitted.
        self.unconfirmed = {
            path
            for path, row in self.rows.items()
            if status.state_name(row.get("state")) == "PREPARED"
        }

    def contact_support(self, event=None):
        opened = False
        try:
            opened = webbrowser.open(status.SUPPORT_URL)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(status.SUPPORT_EMAIL)
                copied = " The address was copied to the clipboard."
            except tk.TclError:
                copied = ""
            messagebox.showinfo(
                "Contact Inkly support",
                f"No email app opened. Write to {status.SUPPORT_EMAIL} "
                f"from any email service.{copied}",
                parent=self.root,
            )
        return "break"

    def layout_report(self):
        """Geometry evidence for the packaged smoke test and scaled-font tests."""
        self.root.update_idletasks()
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        width, height = self.root.winfo_width(), self.root.winfo_height()

        def box(widget):
            x, y = widget.winfo_rootx() - left, widget.winfo_rooty() - top
            return x, y, x + widget.winfo_width(), y + widget.winfo_height()

        def fits(widget):
            # Children are clipped by every parent frame, not only by the window.
            x1, y1, x2, y2 = box(widget)
            if not widget.winfo_ismapped() or x2 - x1 <= 1 or y2 - y1 <= 1:
                return False
            parent = widget.master
            while parent is not None and parent is not self.root:
                px1, py1, px2, py2 = box(parent)
                if x1 < px1 or y1 < py1 or x2 > px2 or y2 > py2:
                    return False
                parent = parent.master
            return x1 >= 0 and y1 >= 0 and x2 <= width and y2 <= height

        controls = {
            "settings": self.settings_button,
            "my_jobs": self.jobs_button,
            "add": self.add_button,
            "folder": self.folder_button,
            "remove": self.remove_button,
            "clear": self.clear_button,
            "cancel": self.cancel_button,
            "rerun": self.rerun_button,
            "preview": self.preview_button,
            "submit": self.submit_button,
            "monitor": self.monitor_button,
            "stop": self.stop_button,
            "script": self.script_button,
            "results": self.results_button,
            "status": self.status,
        }
        clipped = sorted(name for name, widget in controls.items() if not fits(widget))
        shown = self.contact.winfo_manager() == "pack"
        cx1, cy1, cx2, cy2 = box(self.contact)
        overlaps = shown and any(
            x1 < cx2 and cx1 < x2 and y1 < cy2 and cy1 < y2
            for x1, y1, x2, y2 in map(box, controls.values())
        )
        return {
            "contact": self.contact.cget("text"),
            "contact_visible": shown
            and fits(self.contact)
            and cy2 <= box(self.banner)[3]
            and not overlaps,
            "controls_visible": not clipped,
            "clipped": clipped,
            "table_height": self.table.winfo_height(),
            # Diagnostics for layout reviews at different display scaling.
            "window_size": [width, height],
            "tk_scaling": round(float(self.root.tk.call("tk", "scaling")), 3),
            "screen": [self.root.winfo_screenwidth(), self.root.winfo_screenheight()],
            "contact_height": cy2 - cy1 if shown else 0,
            "bottoms": {name: box(widget)[3] for name, widget in controls.items()},
            "requested_height": self.root.winfo_reqheight(),
        }

    def buttons(self):
        # While watching, new work goes to the running watch on its open
        # connection. Removing rows or changing the connection waits until idle.
        ready = not self.busy or self.watching()
        self.jobs_button.configure(state="normal" if ready else "disabled")
        for widget in (self.add_button, self.folder_button):
            widget.configure(state="normal" if ready else "disabled")
        for widget in (self.remove_button, self.clear_button, self.settings_button):
            widget.configure(state="disabled" if self.busy else "normal")
        self.preview_button.configure(
            state="normal" if self.files and ready else "disabled"
        )
        self.submit_button.configure(
            state="normal"
            if self.review and self.review["count"] and ready
            else "disabled"
        )
        # Watching again reuses the open connection, or signs in if it closed.
        self.monitor_button.configure(
            state="normal" if self.rows and not self.busy else "disabled"
        )
        self.stop_button.configure(state="normal" if self.busy else "disabled")
        self.cancel_button.configure(
            state="normal" if self.cancellable() and ready else "disabled"
        )
        self.rerun_button.configure(
            state="normal" if self.rerunnable() and ready else "disabled"
        )
        self.script_button.configure(
            state="normal" if self.table.selection() and self.rows else "disabled"
        )
        self.results_button.configure(
            state="normal"
            if any(row.get("output") for row in self.rows.values())
            else "disabled"
        )

    def selected_rows(self):
        """Displayed rows for the selection; None where a row is not previewed."""
        return [(path, self.rows.get(path)) for path in self.table.selection()]

    def cancellable(self):
        rows = self.selected_rows()
        return bool(rows) and all(
            row
            and status.state_name(row.get("state")) == "PENDING"
            and row.get("job_id")
            for _, row in rows
        )

    def rerunnable(self):
        # An unchanged PREPARED row can simply be submitted; an edited one
        # needs a fresh bundle.
        rows = self.selected_rows()
        return bool(rows) and all(
            row
            and status.state_name(row.get("state")) in picker.RERUNNABLE
            and (
                status.state_name(row.get("state")) != "PREPARED"
                or row.get("input_changed")
            )
            for _, row in rows
        )

    def my_jobs(self):
        if not self.settings_ready():
            return
        if self.watching():
            self.controller.request("my_jobs", dict(self.settings))
        elif not self.busy:
            self.start("my_jobs", dict(self.settings))

    def show_my_jobs(self, response):
        previous = getattr(self, "jobs_window", None)
        if previous is not None and previous.winfo_exists():
            previous.destroy()
        dialog = self.jobs_window = tk.Toplevel(self.root)
        dialog.title("My jobs — current HPC account")
        dialog.geometry("1000x520")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        summary = f"Current queue and last 7 days of accounting. Observed (UTC): {response['observed_at']}"
        if response.get("unavailable"):
            summary += (
                "\nUnavailable: "
                + ", ".join(response["unavailable"])
                + ". This is an incomplete listing."
            )
        if response.get("truncated"):
            summary += (
                "\nListing limited to 200 jobs; use Slurm directly for older records."
            )
        if not response.get("jobs") and not response.get("unavailable"):
            summary += "\nNo jobs reported in this window."
        ttk.Label(dialog, text=summary, wraplength=940).grid(
            row=0, column=0, sticky="w", padx=12, pady=12
        )
        columns = (
            "job_id",
            "name",
            "state",
            "elapsed",
            "exit_code",
            "start_time",
            "reason",
        )
        table = ttk.Treeview(dialog, columns=columns, show="headings")
        for key, title in zip(
            columns,
            (
                "Job ID",
                "Job name",
                "State",
                "Elapsed",
                "Exit",
                "Start (HPC time)",
                "Reason",
            ),
        ):
            table.heading(key, text=title)
            table.column(key, width=130, minwidth=65)
        for row in response.get("jobs", ()):
            table.insert(
                "", "end", values=[row.get(key) or "Unavailable" for key in columns]
            )
        table.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        vertical = ttk.Scrollbar(dialog, orient="vertical", command=table.yview)
        vertical.grid(row=1, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(dialog, orient="horizontal", command=table.xview)
        horizontal.grid(row=2, column=0, sticky="ew", padx=12)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        ttk.Label(
            dialog,
            text="Read-only account view, including jobs submitted outside Inkly. Refresh to update; use saved handoffs for result return.",
        ).grid(row=3, column=0, sticky="w", padx=12, pady=8)
        ttk.Button(dialog, text="Refresh", command=self.my_jobs).grid(
            row=4, column=0, sticky="e", padx=12, pady=(0, 12)
        )

    def settings_ready(self):
        try:
            picker.validate_settings(self.settings)
        except (ValueError, KeyError, TypeError):
            self.configure()
            return False
        return True

    @staticmethod
    def listing(lines):
        text = "\n".join(lines[:8])
        if len(lines) > 8:
            text += f"\n…and {len(lines) - 8} more selected rows."
        return text

    def watching(self):
        """Submitting or checking jobs: the worker is in (or about to start) its
        monitor loop, which can take cancel requests on its open connection."""
        return self.busy and self.action in {"submit", "monitor"}

    def cancel_jobs(self):
        if self.busy and not self.watching():
            return
        if not self.cancellable() or not self.settings_ready():
            return
        rows = self.selected_rows()
        details = self.listing(
            [f"{row['name']}: job {row['job_id']}" for _, row in rows]
        )
        text = (
            f"Cancel {len(rows)} pending job(s) on the HPC?\n\n{details}\n\n"
            "Only jobs that are still waiting are cancelled. A job that has already "
            "started keeps running. Your input files are not changed and nothing is "
            "resubmitted. To run a file again later, select it and choose Run again."
        )
        if not messagebox.askyesno(
            "Cancel pending jobs", text, parent=self.root, default="no"
        ):
            return
        paths = [path for path, _ in rows]
        if self.watching():
            # Same SSH session as the watch: no new sign-in, and watching continues.
            self.controller.request_cancel(paths, dict(self.settings))
            self.log("Cancelling at the next check, on the open connection.")
        else:
            self.start("cancel_jobs", paths, dict(self.settings))

    def run_again(self):
        if self.busy and not self.watching():
            return
        if not self.rerunnable() or not self.settings_ready():
            return
        rows = self.selected_rows()
        details = self.listing(
            [
                f"{row['name']}: {status.state_name(row.get('state'))}"
                + (" (input edited)" if row.get("input_changed") else "")
                for _, row in rows
            ]
        )
        text = (
            f"Prepare {len(rows)} file(s) again as new job(s)?\n\n{details}\n\n"
            "Inkly keeps a copy of each old receipt beside the input and prepares the "
            "file as it is now, with its original HPC connection and your current time "
            f"limit ({self.settings['hours']} hour(s)). Nothing is submitted: review "
            "the preview, then choose Submit prepared jobs."
        )
        if messagebox.askyesno(
            "Run again as new jobs", text, parent=self.root, default="no"
        ):
            self.review = None
            arguments = (
                [path for path, _ in rows],
                [str(path) for path in self.files],
                dict(self.settings),
            )
            if self.watching():
                self.controller.request("rerun", *arguments)
                self.log("Preparing again on the open connection; watching continues.")
                self.buttons()
            else:
                self.start("rerun", *arguments)

    def invalidate(self):
        self.review = None
        self.rows = {}
        self.unconfirmed = set()
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

    def list_changed(self):
        """The file list changed: keep each existing row as last shown, drop rows
        for removed files, and require a new preview before any submission."""
        self.review = None
        self.rows = {
            key: row for key, row in self.rows.items() if Path(key) in self.files
        }
        self.unconfirmed &= set(self.rows)
        self.count.configure(text=f"{len(self.files)} file(s)")
        self.refresh_overview()
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
        self.list_changed()

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
        self.list_changed()

    def clear(self):
        for path in self.table.get_children():
            self.table.delete(path)
        self.files.clear()
        self.list_changed()

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
        if (self.busy and not self.watching()) or not self.settings_ready():
            return
        if self.watching():
            # Rows stay as shown until the preview updates them.
            self.review = None
            self.controller.request("preview", list(self.files), dict(self.settings))
            self.log("Previewing on the open connection; watching continues.")
            self.buttons()
            return
        self.invalidate()
        self.start("preview", list(self.files), dict(self.settings))

    def submit(self):
        if not self.review or (self.busy and not self.watching()):
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
        if not messagebox.askyesno(
            "Confirm HPC submission", text, parent=self.root, default="no"
        ):
            return
        if self.watching():
            self.review = None  # Approval is used once.
            self.controller.request("submit", review["id"], True)
            self.log("Submitting on the open connection; watching continues.")
            self.refresh_overview()
            self.buttons()
        else:
            self.start("submit", review["id"], True)

    def stop(self):
        if self.watching() and not self.stop_deadline:
            # End the watch after the current check and keep the connection, so
            # Check & return results resumes without a new sign-in. Disconnect
            # only if the watch has not ended after the grace period.
            self.controller.pause()
            self.status.configure(
                text="Stopping the watch. Submitted HPC jobs continue and the "
                "connection stays open."
            )
            run = self.run_id
            self.stop_deadline = self.root.after(
                STOP_GRACE_MS, lambda: self.force_stop(run)
            )
            self.buttons()
            return
        self.force_stop(self.run_id)  # Not watching, or Stop clicked twice.

    def force_stop(self, run):
        """Disconnect to interrupt the action that was running when Stop was clicked."""
        self.stop_deadline = None
        if not self.busy or run != self.run_id:
            return
        self.controller.stop()
        self.review = None
        if self.action == "submit":
            self.mark_unconfirmed()
        self.refresh_overview()
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
        if self.stop_deadline:
            self.root.after_cancel(self.stop_deadline)
        self.root.destroy()
        # Free the closed window here, on the Tk thread. Automatic collection
        # stays off: this window's threads may still be finishing.
        gc.collect()


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
        for _ in range(5):
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
                    **app.layout_report(),
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
