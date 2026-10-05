#!/usr/bin/env python3
"""
vmtui.py - ESXi / vCenter VM power operation TUI

Features:
  - List VMs and monitor power state / VMware Tools state / heartbeat / IP
  - Power on the selected VM
  - Graceful guest shutdown of the selected VM

Notes:
  - Guest shutdown requires VMware Tools to be installed and running in the VM.
  - This tool intentionally does not implement hard PowerOffVM_Task by default.
"""

from __future__ import annotations

import argparse
import atexit
import curses
import getpass
import os
import queue
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl


@dataclass
class VmRow:
    index: int
    moid: str
    name: str
    power: str
    tools: str
    heartbeat: str
    host: str
    ip: str
    connection_state: str
    vm: vim.VirtualMachine


class VsphereClient:
    def __init__(self, host: str, user: str, password: str, port: int = 443, insecure: bool = False):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.insecure = insecure
        self.si = None
        self.content = None

    def connect(self) -> None:
        ssl_context = None
        if self.insecure:
            ssl_context = ssl._create_unverified_context()

        self.si = SmartConnect(
            host=self.host,
            user=self.user,
            pwd=self.password,
            port=self.port,
            sslContext=ssl_context,
        )
        atexit.register(Disconnect, self.si)
        self.content = self.si.RetrieveContent()

    def disconnect(self) -> None:
        if self.si is not None:
            Disconnect(self.si)
            self.si = None
            self.content = None

    def list_vms(self, name_filter: str = "") -> list[VmRow]:
        if self.content is None:
            raise RuntimeError("Not connected")

        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            rows: list[VmRow] = []
            needle = name_filter.lower().strip()
            for vm in sorted(view.view, key=lambda x: x.name.lower()):
                name = safe_str(vm.name)
                if needle and needle not in name.lower():
                    continue

                runtime = vm.runtime
                guest = vm.guest
                config = vm.config

                host_name = "-"
                try:
                    if runtime.host:
                        host_name = safe_str(runtime.host.name)
                except Exception:
                    host_name = "-"

                rows.append(
                    VmRow(
                        index=len(rows) + 1,
                        moid=safe_str(vm._moId),
                        name=name,
                        power=enum_value(runtime.powerState),
                        tools=safe_str(getattr(guest, "toolsRunningStatus", None)) or "-",
                        heartbeat=enum_value(getattr(vm, "guestHeartbeatStatus", None)) or "-",
                        host=host_name,
                        ip=safe_str(getattr(guest, "ipAddress", None)) or "-",
                        connection_state=enum_value(getattr(runtime, "connectionState", None)) or "-",
                        vm=vm,
                    )
                )
            return rows
        finally:
            view.Destroy()

    def power_on(self, vm: vim.VirtualMachine, timeout: int = 300) -> str:
        if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
            return f"{vm.name} is already powered on"
        task = vm.PowerOnVM_Task()
        wait_for_task(task, timeout=timeout)
        return f"Power on completed: {vm.name}"

    def shutdown_guest(self, vm: vim.VirtualMachine) -> str:
        if vm.runtime.powerState != vim.VirtualMachine.PowerState.poweredOn:
            return f"{vm.name} is not powered on"
        vm.ShutdownGuest()
        return f"Guest shutdown command sent: {vm.name}"


def enum_value(value) -> str:
    if value is None:
        return ""
    return getattr(value, "value", str(value))


def safe_str(value) -> str:
    if value is None:
        return ""
    return str(value)


def fault_text(exc: BaseException) -> str:
    if isinstance(exc, vmodl.MethodFault):
        return exc.msg or exc.__class__.__name__
    return str(exc) or exc.__class__.__name__


def wait_for_task(task, timeout: int = 300, interval: float = 0.5) -> None:
    start = time.time()
    while True:
        state = task.info.state
        if state == vim.TaskInfo.State.success:
            return
        if state == vim.TaskInfo.State.error:
            err = task.info.error
            raise RuntimeError(err.msg if err and err.msg else "Task failed")
        if time.time() - start > timeout:
            raise TimeoutError(f"Task did not finish within {timeout} seconds")
        time.sleep(interval)


class EsxiTui:
    def __init__(self, client: VsphereClient, interval: int = 5, name_filter: str = ""):
        self.client = client
        self.interval = max(1, interval)
        self.filter = name_filter
        self.rows: list[VmRow] = []
        self.selected = 0
        self.top = 0
        self.last_refresh = 0.0
        self.status = "Starting..."
        self.action_q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(False)
        stdscr.timeout(500)
        self._init_colors()
        self.refresh_vms(force=True)

        while not self.stop_event.is_set():
            self._drain_action_queue()
            now = time.time()
            if now - self.last_refresh >= self.interval:
                self.refresh_vms(force=False)

            self.draw(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue

            if key in (ord("q"), 27):
                self.stop_event.set()
            elif key in (curses.KEY_DOWN, ord("j")):
                self.move_selection(1)
            elif key in (curses.KEY_UP, ord("k")):
                self.move_selection(-1)
            elif key == curses.KEY_NPAGE:
                self.move_selection(10)
            elif key == curses.KEY_PPAGE:
                self.move_selection(-10)
            elif key in (ord("r"), curses.KEY_F5):
                self.refresh_vms(force=True)
            elif key == ord("p"):
                self.confirm_and_run(stdscr, "power on", self.power_on_selected)
            elif key == ord("s"):
                self.confirm_and_run(stdscr, "guest shutdown", self.shutdown_selected)
            elif key == ord("/"):
                self.set_filter(stdscr)
            elif key == ord("c"):
                self.filter = ""
                self.selected = 0
                self.top = 0
                self.refresh_vms(force=True)

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selected row
            curses.init_pair(2, curses.COLOR_GREEN, -1)                  # poweredOn
            curses.init_pair(3, curses.COLOR_YELLOW, -1)                 # suspended
            curses.init_pair(4, curses.COLOR_RED, -1)                    # poweredOff/error
            curses.init_pair(5, curses.COLOR_CYAN, -1)                   # header
        except curses.error:
            pass

    def refresh_vms(self, force: bool = False) -> None:
        try:
            self.rows = self.client.list_vms(self.filter)
            self.selected = min(self.selected, max(0, len(self.rows) - 1))
            self.last_refresh = time.time()
            self.status = f"Refreshed {len(self.rows)} VM(s) at {time.strftime('%H:%M:%S')}"
        except Exception as exc:
            self.status = f"Refresh failed: {fault_text(exc)}"
            if force:
                self.last_refresh = time.time()

    def draw(self, stdscr) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = f" ESXi/vCenter VM TUI  host={self.client.host}  refresh={self.interval}s  filter={self.filter or '-'} "
        self.addn(stdscr, 0, 0, title, w - 1, curses.color_pair(5) | curses.A_BOLD)

        header = self.format_row("No", "Power", "VM Name", "Tools", "Heartbeat", "Host", "IP", w)
        self.addn(stdscr, 2, 0, header, w - 1, curses.A_BOLD)

        body_height = max(1, h - 6)
        self.adjust_scroll(body_height)
        visible = self.rows[self.top : self.top + body_height]

        for offset, row in enumerate(visible):
            y = 3 + offset
            text = self.format_row(
                str(row.index), row.power, row.name, row.tools, row.heartbeat, row.host, row.ip, w
            )
            attr = curses.A_NORMAL
            if row.power == "poweredOn":
                attr |= curses.color_pair(2)
            elif row.power == "suspended":
                attr |= curses.color_pair(3)
            elif row.power == "poweredOff":
                attr |= curses.color_pair(4)

            if self.top + offset == self.selected:
                attr = curses.color_pair(1) | curses.A_BOLD
            self.addn(stdscr, y, 0, text, w - 1, attr)

        footer_y = h - 3
        self.addn(stdscr, footer_y, 0, "─" * max(1, w - 1), w - 1, curses.A_DIM)
        self.addn(
            stdscr,
            h - 2,
            0,
            "Keys: ↑/↓ or j/k move | p power on | s guest shutdown | r refresh | / filter | c clear filter | q quit",
            w - 1,
            curses.A_DIM,
        )
        self.addn(stdscr, h - 1, 0, self.status, w - 1, curses.A_REVERSE)
        stdscr.refresh()

    def format_row(self, no: str, power: str, name: str, tools: str, heartbeat: str, host: str, ip: str, width: int) -> str:
        # Fixed-width columns keep the display readable on narrow terminals.
        parts = [
            fit(no, 4),
            fit(power, 11),
            fit(name, max(20, min(45, width - 70))),
            fit(tools, 18),
            fit(heartbeat, 10),
            fit(host, 18),
            fit(ip, 15),
        ]
        return " ".join(parts)

    def addn(self, stdscr, y: int, x: int, text: str, n: int, attr: int = curses.A_NORMAL) -> None:
        try:
            stdscr.addnstr(y, x, text, n, attr)
        except curses.error:
            pass

    def adjust_scroll(self, body_height: int) -> None:
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + body_height:
            self.top = self.selected - body_height + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - body_height)))

    def move_selection(self, delta: int) -> None:
        if not self.rows:
            return
        self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))

    def selected_row(self) -> Optional[VmRow]:
        if not self.rows:
            self.status = "No VM selected"
            return None
        return self.rows[self.selected]

    def confirm_and_run(self, stdscr, action_name: str, func: Callable[[], None]) -> None:
        row = self.selected_row()
        if row is None:
            return
        if self.worker and self.worker.is_alive():
            self.status = "Another operation is still running"
            return

        answer = self.prompt(stdscr, f"Confirm {action_name} for '{row.name}'? Type y to continue: ")
        if answer.lower() == "y":
            func()
        else:
            self.status = f"Cancelled: {action_name} {row.name}"

    def power_on_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        self.start_worker(f"Power on {row.name}", lambda: self.client.power_on(row.vm))

    def shutdown_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        self.start_worker(f"Guest shutdown {row.name}", lambda: self.client.shutdown_guest(row.vm))

    def start_worker(self, label: str, operation: Callable[[], str]) -> None:
        self.status = f"Running: {label}"

        def target() -> None:
            try:
                msg = operation()
                self.action_q.put(("ok", msg))
            except Exception as exc:
                self.action_q.put(("err", f"{label} failed: {fault_text(exc)}"))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _drain_action_queue(self) -> None:
        changed = False
        while True:
            try:
                kind, msg = self.action_q.get_nowait()
            except queue.Empty:
                break
            changed = True
            self.status = msg if kind == "ok" else f"ERROR: {msg}"
        if changed:
            self.refresh_vms(force=True)

    def set_filter(self, stdscr) -> None:
        text = self.prompt(stdscr, "Filter VM name, empty for all: ")
        self.filter = text.strip()
        self.selected = 0
        self.top = 0
        self.refresh_vms(force=True)

    def prompt(self, stdscr, message: str) -> str:
        h, w = stdscr.getmaxyx()

        # Some Python/curses builds do not expose window.getnstr().
        # Use the widely available window.getstr(y, x, n) instead.
        # Also keep enough room for input even when the terminal is narrow.
        if w < 8:
            return ""

        input_min = 4
        prompt_width = max(1, w - input_min - 2)
        shown_message = message[:prompt_width]
        input_x = min(len(shown_message), w - 2)
        input_len = max(1, w - input_x - 2)

        curses.echo()
        try:
            try:
                curses.curs_set(1)
            except curses.error:
                pass

            self.addn(stdscr, h - 1, 0, " " * max(1, w - 1), w - 1, curses.A_REVERSE)
            self.addn(stdscr, h - 1, 0, shown_message, w - 1, curses.A_REVERSE)
            stdscr.refresh()

            raw = stdscr.getstr(h - 1, input_x, input_len)
            return raw.decode(errors="replace")
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass


def fit(text: str, width: int) -> str:
    text = str(text)
    if width <= 1:
        return text[:width]
    if len(text) > width:
        return text[: max(1, width - 1)] + "…"
    return text.ljust(width)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TUI for ESXi/vCenter VM status and power operations")
    parser.add_argument("--host", required=True, help="ESXi or vCenter hostname / IP")
    parser.add_argument("--user", required=True, help="User name, e.g. root or administrator@vsphere.local")
    parser.add_argument("--port", type=int, default=443, help="vSphere API port, default: 443")
    parser.add_argument("--password", help="Password. If omitted, VSPHERE_PASSWORD or prompt is used")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification for lab/self-signed certs")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval seconds, default: 5")
    parser.add_argument("--filter", default="", help="Initial VM name filter")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    password = args.password or os.environ.get("VSPHERE_PASSWORD")
    if not password:
        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")

    client = VsphereClient(
        host=args.host,
        user=args.user,
        password=password,
        port=args.port,
        insecure=args.insecure,
    )

    try:
        client.connect()
    except Exception as exc:
        print(f"Connection failed: {fault_text(exc)}", file=sys.stderr)
        return 2

    try:
        app = EsxiTui(client, interval=args.interval, name_filter=args.filter)
        app.run()
    finally:
        client.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
