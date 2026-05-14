#!/usr/bin/env python3

import curses
import os
import queue
import re
import signal
import socket
import subprocess
import textwrap
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
APP_VERSION = os.environ.get("DEV_TERMINAL_VERSION", "dev")


@dataclass
class ManagedProcess:
    name: str
    proc: Optional[subprocess.Popen] = None
    status: str = "stopped"
    pid: Optional[int] = None
    code: Optional[int] = None


@dataclass
class TaskState:
    label: str
    status: str = "idle"
    code: Optional[int] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None


class DevTerminal:
    def __init__(self, stdscr):
        self.stdscr = stdscr

        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = str(os.environ.get("PORT", "8000"))
        self.vite_host = os.environ.get("VITE_HOST", "127.0.0.1")

        self.root = self.detect_project_root()
        os.chdir(str(self.root))

        self.log_dir = self.root / "storage" / "logs" / "dev-terminal"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.logs = deque(maxlen=8000)

        self.shutdown = False
        self.follow = True
        self.scroll_offset = 0
        self.log_filter = ""
        self.log_mode = "all"
        self.flash = ""
        self.flash_until = 0.0
        self.busy_label = ""

        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_started = False

        self.serve = ManagedProcess("serve")
        self.vite = ManagedProcess("vite")

        self.health = "unknown"
        self.health_last = 0.0

        self.tasks = {
            "optimize": TaskState("php artisan optimize"),
            "clear": TaskState("php artisan optimize:clear"),
            "build": TaskState("npm run build"),
            "version": TaskState("scripts/update-version.sh"),
            "test": TaskState("php artisan test"),
            "migrate": TaskState("php artisan migrate"),
            "pint": TaskState("vendor/bin/pint"),
            "routes": TaskState("php artisan route:list"),
            "autoload": TaskState("composer dump-autoload"),
        }

        self.commands = {
            "optimize": ["php", "artisan", "optimize"],
            "clear": ["php", "artisan", "optimize:clear"],
            "build": ["npm", "run", "build"],
            "version": ["bash", "scripts/update-version.sh"],
            "test": ["php", "artisan", "test"],
            "migrate": ["php", "artisan", "migrate"],
            "pint": ["vendor/bin/pint"],
            "routes": ["php", "artisan", "route:list"],
            "autoload": ["composer", "dump-autoload"],
        }

        self.palette = {}
        self.setup_curses()

    def detect_project_root(self) -> Path:
        cwd = Path.cwd().resolve()

        if (cwd / "artisan").exists():
            return cwd

        for parent in cwd.parents:
            if (parent / "artisan").exists():
                return parent

        return cwd

    def setup_curses(self):
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.timeout(120)
        self.stdscr.keypad(True)

        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()

            colors = [
                ("ok", curses.COLOR_GREEN),
                ("error", curses.COLOR_RED),
                ("warn", curses.COLOR_YELLOW),
                ("serve", curses.COLOR_CYAN),
                ("vite", curses.COLOR_BLUE),
                ("task", curses.COLOR_MAGENTA),
                ("muted", curses.COLOR_BLUE),
                ("header", curses.COLOR_WHITE),
            ]

            for index, item in enumerate(colors, start=1):
                name, color = item
                curses.init_pair(index, color, -1)
                self.palette[name] = curses.color_pair(index)

        for name in ["ok", "error", "warn", "serve", "vite", "task", "muted", "header"]:
            self.palette.setdefault(name, curses.A_NORMAL)

    def start(self):
        self.worker.start()
        self.worker_started = True
        self.enqueue("pipeline")
        self.loop()

    def loop(self):
        while not self.shutdown:
            self.drain_events()
            self.refresh_health()
            self.draw()

            key = self.stdscr.getch()

            if key != -1:
                self.handle_key(key)

        self.stop_process(self.serve)
        self.stop_process(self.vite)

    def worker_loop(self):
        while not self.shutdown:
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self.events.put(("busy", job))

                if job == "pipeline":
                    self.run_pipeline()
                elif job == "serve:start":
                    self.start_serve(interactive=False)
                elif job == "serve:stop":
                    self.stop_process(self.serve)
                elif job == "serve:restart":
                    self.stop_process(self.serve)
                    self.start_serve(interactive=False)
                elif job == "vite:start":
                    self.start_vite()
                elif job == "vite:stop":
                    self.stop_process(self.vite)
                elif job == "vite:restart":
                    self.stop_process(self.vite)
                    self.start_vite()
                elif job.startswith("task:"):
                    self.run_command(job.split(":", 1)[1])
                elif job.startswith("custom:"):
                    self.run_custom(job.split(":", 1)[1])

            except Exception as exc:
                self.emit("error", "Job fout: {}".format(exc), "error")
            finally:
                self.events.put(("busy", ""))

    def enqueue(self, job: str):
        self.jobs.put(job)
        self.flash_message("Taak toegevoegd: {}".format(job))

    def now(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def clean(self, value: str) -> str:
        value = ANSI_RE.sub("", value)
        return value.replace("\r", "\n").rstrip("\n")

    def emit(self, channel: str, message: str, level: str = "normal"):
        for line in self.clean(str(message)).splitlines() or [""]:
            self.events.put(("log", self.now(), channel, line, level))

            log_file = self.log_dir / "{}.log".format(channel if channel else "app")
            try:
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("[{}] [{}] {}\n".format(
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        channel,
                        line,
                    ))
            except Exception:
                pass

    def flash_message(self, message: str, seconds: float = 2.0):
        self.events.put(("flash", message, time.time() + seconds))

    def drain_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            kind = event[0]

            if kind == "log":
                _, ts, channel, message, level = event
                self.logs.append((ts, channel, message, level))

                if self.follow:
                    self.scroll_offset = 0

            elif kind == "flash":
                _, self.flash, self.flash_until = event

            elif kind == "busy":
                _, self.busy_label = event

            elif kind == "task":
                _, key, status, code = event
                task = self.tasks[key]
                task.status = status
                task.code = code

                if status == "running":
                    task.started_at = time.time()
                    task.ended_at = None
                elif status in ["ok", "failed"]:
                    task.ended_at = time.time()

            elif kind == "process":
                _, name, status, pid, code = event
                proc = self.serve if name == "serve" else self.vite
                proc.status = status
                proc.pid = pid
                proc.code = code

    def run_pipeline(self):
        self.emit("task", "Pipeline gestart: optimize -> build -> version -> serve", "task")
        self.run_command("optimize")
        self.run_command("build")
        self.run_command("version")
        self.start_serve(interactive=False)
        self.emit("task", "Pipeline klaar", "ok")

    def run_command(self, key: str) -> int:
        if key not in self.commands:
            self.emit("error", "Onbekende taak: {}".format(key), "error")
            return 1

        label = self.tasks[key].label
        command = self.commands[key]

        self.events.put(("task", key, "running", None))
        self.emit("task", "▶ {}".format(label), "task")
        self.emit("task", "$ {}".format(" ".join(command)), "muted")

        code = 1

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None

            for raw in iter(proc.stdout.readline, ""):
                if raw == "" and proc.poll() is not None:
                    break
                self.emit("app", raw.rstrip("\n"))

            code = proc.wait()

        except FileNotFoundError as exc:
            self.emit("error", "Command niet gevonden: {}".format(exc.filename), "error")
            code = 127
        except Exception as exc:
            self.emit("error", "Command faalde: {}".format(exc), "error")
            code = 1

        if code == 0:
            self.events.put(("task", key, "ok", code))
            self.emit("task", "✓ {} afgerond".format(label), "ok")
        else:
            self.events.put(("task", key, "failed", code))
            self.emit("error", "✗ {} faalde met exit code {}".format(label, code), "error")
            self.emit("warn", "Terminal blijft actief.", "warn")

        return code

    def run_custom(self, command: str):
        self.emit("task", "▶ custom command", "task")
        self.emit("task", "$ {}".format(command), "muted")

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None

            for raw in iter(proc.stdout.readline, ""):
                if raw == "" and proc.poll() is not None:
                    break
                self.emit("app", raw.rstrip("\n"))

            code = proc.wait()

            if code == 0:
                self.emit("task", "✓ custom command afgerond", "ok")
            else:
                self.emit("error", "✗ custom command faalde met exit code {}".format(code), "error")

        except Exception as exc:
            self.emit("error", "Custom command fout: {}".format(exc), "error")

    def start_serve(self, interactive: bool):
        busy = self.find_port_processes(self.port)

        if busy:
            self.emit("error", "Laravel serve niet gestart: poort {} is al bezet.".format(self.port), "error")

            for item in busy:
                self.emit("warn", "PID {} | {}".format(item["pid"], item["command"]), "warn")

            self.emit("warn", "Gebruik j om andere Laravel serve PID's te killen of start met PORT=8080.", "warn")
            self.events.put(("process", "serve", "failed", None, 1))
            return

        command = [
            "php",
            "artisan",
            "serve",
            "--host",
            self.host,
            "--port",
            self.port,
        ]

        self.emit("serve", "▶ Laravel serve starten", "serve")
        self.emit("serve", "$ {}".format(" ".join(command)), "muted")

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=(os.name == "posix"),
            )

            self.serve.proc = proc
            self.events.put(("process", "serve", "running", proc.pid, None))
            self.emit("serve", "Laravel serve draait, PID {}".format(proc.pid), "ok")

            threading.Thread(target=self.watch_process, args=(self.serve, "serve"), daemon=True).start()

        except Exception as exc:
            self.events.put(("process", "serve", "failed", None, 1))
            self.emit("error", "Laravel serve starten faalde: {}".format(exc), "error")

    def start_vite(self):
        command = ["npm", "run", "dev", "--", "--host", self.vite_host]

        self.emit("vite", "▶ Vite starten", "vite")
        self.emit("vite", "$ {}".format(" ".join(command)), "muted")

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=(os.name == "posix"),
            )

            self.vite.proc = proc
            self.events.put(("process", "vite", "running", proc.pid, None))
            self.emit("vite", "Vite draait, PID {}".format(proc.pid), "ok")

            threading.Thread(target=self.watch_process, args=(self.vite, "vite"), daemon=True).start()

        except Exception as exc:
            self.events.put(("process", "vite", "failed", None, 1))
            self.emit("error", "Vite starten faalde: {}".format(exc), "error")

    def watch_process(self, managed: ManagedProcess, channel: str):
        proc = managed.proc

        if proc is None:
            return

        try:
            assert proc.stdout is not None

            for raw in iter(proc.stdout.readline, ""):
                if raw == "" and proc.poll() is not None:
                    break
                self.emit(channel, raw.rstrip("\n"), channel)

            code = proc.wait()

            if managed.proc is proc:
                status = "stopped" if code == 0 else "failed"
                self.events.put(("process", managed.name, status, None, code))

                if code == 0:
                    self.emit(channel, "{} gestopt.".format(managed.name), "warn")
                else:
                    self.emit("error", "{} stopte met exit code {}".format(managed.name, code), "error")

        except Exception as exc:
            self.emit("error", "{} watcher fout: {}".format(channel, exc), "error")

    def stop_process(self, managed: ManagedProcess):
        proc = managed.proc

        if proc is None or proc.poll() is not None:
            self.events.put(("process", managed.name, "stopped", None, managed.code))
            self.emit(managed.name, "{} draait niet.".format(managed.name), "warn")
            return

        self.emit(managed.name, "{} stoppen, PID {}".format(managed.name, proc.pid), "warn")

        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()

            proc.wait(timeout=4)

        except subprocess.TimeoutExpired:
            self.emit(managed.name, "{} force kill.".format(managed.name), "warn")

            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()

                proc.wait(timeout=2)
            except Exception as exc:
                self.emit("error", "{} kill faalde: {}".format(managed.name, exc), "error")

        except Exception as exc:
            self.emit("error", "{} stoppen faalde: {}".format(managed.name, exc), "error")

        self.events.put(("process", managed.name, "stopped", None, proc.returncode))
        self.emit(managed.name, "{} gestopt.".format(managed.name), "ok")

    def find_port_processes(self, port: str) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["lsof", "-nP", "-iTCP:{}".format(port), "-sTCP:LISTEN"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
            )
        except Exception:
            return []

        lines = result.stdout.splitlines()

        if len(lines) <= 1:
            return []

        items = []

        for line in lines[1:]:
            parts = line.split(None, 8)

            if len(parts) < 2:
                continue

            pid = parts[1]
            command = self.command_for_pid(pid)

            items.append({
                "pid": pid,
                "command": command or line,
                "is_laravel": self.is_laravel_serve(command or line),
            })

        return items

    def command_for_pid(self, pid: str) -> str:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def is_laravel_serve(self, command: str) -> bool:
        value = command.lower()

        return (
            "artisan serve" in value
            or "resources/server.php" in value
            or "laravel/framework" in value and "server.php" in value
            or "php -s" in value and "server.php" in value
        )

    def kill_other_laravel_serve(self):
        processes = self.find_port_processes(self.port)
        targets = [item for item in processes if item.get("is_laravel")]

        if not targets:
            self.emit("warn", "Geen andere Laravel serve PID gevonden.", "warn")
            return

        pids = ", ".join([str(item["pid"]) for item in targets])

        answer = self.ask("Andere Laravel serve PID(s) {} killen? ja/nee: ".format(pids))

        if answer.strip().lower() not in ["ja", "j", "yes", "y"]:
            self.emit("warn", "Kill geannuleerd.", "warn")
            return

        for item in targets:
            pid = str(item["pid"])

            try:
                os.kill(int(pid), signal.SIGTERM)
                self.emit("warn", "PID {} gestopt.".format(pid), "warn")
            except Exception as exc:
                self.emit("error", "PID {} stoppen faalde: {}".format(pid, exc), "error")

    def inspect_port(self):
        processes = self.find_port_processes(self.port)

        if not processes:
            self.emit("warn", "Poort {} is vrij.".format(self.port), "ok")
            return

        self.emit("warn", "Poort {} is bezet.".format(self.port), "warn")

        for item in processes:
            self.emit("warn", "PID {} | {}".format(item["pid"], item["command"]), "warn")

    def show_laravel_log(self):
        path = self.root / "storage" / "logs" / "laravel.log"

        if not path.exists():
            self.emit("warn", "storage/logs/laravel.log bestaat niet.", "warn")
            return

        try:
            lines = path.read_text(errors="replace").splitlines()[-80:]

            for line in lines:
                self.emit("laravel", line)
        except Exception as exc:
            self.emit("error", "Laravel-log lezen faalde: {}".format(exc), "error")

    def open_browser(self):
        url = "http://{}:{}".format(self.host, self.port)

        try:
            if os.name == "posix":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["python", "-m", "webbrowser", url])
            self.emit("task", "Browser openen: {}".format(url), "ok")
        except Exception as exc:
            self.emit("error", "Browser openen faalde: {}".format(exc), "error")

    def refresh_health(self):
        if time.time() - self.health_last < 2:
            return

        self.health_last = time.time()

        if self.serve.status != "running":
            self.health = "stopped"
            return

        url = "http://{}:{}".format(self.host, self.port)

        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                self.health = "http {}".format(response.status)
        except Exception:
            self.health = "unreachable"

    def ask(self, prompt: str) -> str:
        curses.echo()
        curses.curs_set(1)
        self.stdscr.nodelay(False)

        height, width = self.stdscr.getmaxyx()

        self.stdscr.move(height - 1, 0)
        self.stdscr.clrtoeol()
        self.add(height - 1, 2, prompt, self.palette["warn"])

        try:
            value = self.stdscr.getstr(height - 1, min(width - 2, 2 + len(prompt)), 120)
            answer = value.decode("utf-8", errors="replace")
        except Exception:
            answer = ""
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.stdscr.nodelay(True)

        return answer

    def handle_key(self, key: int):
        if key in [ord("q"), ord("Q")]:
            self.shutdown = True

        elif key in [ord("r"), ord("R")]:
            self.stop_process(self.serve)
            self.start_serve(interactive=True)

        elif key in [ord("s"), ord("S")]:
            self.start_serve(interactive=True)

        elif key in [ord("x"), ord("X")]:
            self.enqueue("serve:stop")

        elif key in [ord("u"), ord("U")]:
            self.enqueue("vite:restart")

        elif key in [ord("y"), ord("Y")]:
            self.enqueue("vite:stop")

        elif key in [ord("d"), ord("D")]:
            self.enqueue("task:build")

        elif key in [ord("v"), ord("V")]:
            self.enqueue("task:version")

        elif key in [ord("o"), ord("O")]:
            self.enqueue("task:optimize")

        elif key in [ord("k"), ord("K")]:
            self.enqueue("task:clear")

        elif key in [ord("t"), ord("T")]:
            self.enqueue("task:test")

        elif key in [ord("m"), ord("M")]:
            self.enqueue("task:migrate")

        elif key in [ord("p"), ord("P")]:
            self.enqueue("task:pint")

        elif key in [ord("z"), ord("Z")]:
            self.enqueue("task:routes")

        elif key in [ord("b"), ord("B")]:
            self.enqueue("task:autoload")

        elif key in [ord("a"), ord("A")]:
            self.enqueue("pipeline")

        elif key in [ord("i"), ord("I")]:
            self.inspect_port()

        elif key in [ord("j"), ord("J")]:
            self.kill_other_laravel_serve()

        elif key in [ord("g"), ord("G")]:
            self.show_laravel_log()

        elif key in [ord("e"), ord("E")]:
            self.open_browser()

        elif key in [ord("f"), ord("F")]:
            self.follow = not self.follow

            if self.follow:
                self.scroll_offset = 0

            self.flash_message("Follow aan" if self.follow else "Follow uit")

        elif key in [ord("c"), ord("C")]:
            self.logs.clear()
            self.flash_message("Schermlog gewist")

        elif key == ord("/"):
            self.log_filter = self.ask("Filter: ")
            self.flash_message("Filter: {}".format(self.log_filter or "geen"))

        elif key == ord(":"):
            command = self.ask("Command: ")

            if command.strip():
                self.enqueue("custom:{}".format(command.strip()))

        elif key in [ord("l"), ord("L")]:
            modes = ["all", "app", "serve", "vite", "laravel", "error", "task", "warn"]
            index = modes.index(self.log_mode) if self.log_mode in modes else 0
            self.log_mode = modes[(index + 1) % len(modes)]
            self.flash_message("Logmodus: {}".format(self.log_mode))

        elif key == curses.KEY_UP:
            self.follow = False
            self.scroll_offset += 1

        elif key == curses.KEY_DOWN:
            self.scroll_offset = max(0, self.scroll_offset - 1)

            if self.scroll_offset == 0:
                self.follow = True

        elif key == curses.KEY_PPAGE:
            height, _ = self.stdscr.getmaxyx()
            self.follow = False
            self.scroll_offset += max(5, height - 8)

        elif key == curses.KEY_NPAGE:
            height, _ = self.stdscr.getmaxyx()
            self.scroll_offset = max(0, self.scroll_offset - max(5, height - 8))

            if self.scroll_offset == 0:
                self.follow = True

        elif key == curses.KEY_HOME:
            self.follow = False
            self.scroll_offset = len(self.logs) + 1000

        elif key == curses.KEY_END:
            self.follow = True
            self.scroll_offset = 0

    def filtered_logs(self):
        result = []

        for row in self.logs:
            ts, channel, message, level = row

            if self.log_mode != "all" and channel != self.log_mode and level != self.log_mode:
                continue

            if self.log_filter and self.log_filter.lower() not in message.lower():
                continue

            result.append(row)

        return result

    def build_rows(self, width: int):
        rows = []

        for ts, channel, message, level in self.filtered_logs():
            prefix = "{} {:<7} | ".format(ts, channel)
            wrap_width = max(10, width - len(prefix))
            chunks = textwrap.wrap(message, wrap_width, replace_whitespace=False, drop_whitespace=False) or [""]

            for index, chunk in enumerate(chunks):
                if index == 0:
                    rows.append((prefix + chunk, channel, level))
                else:
                    rows.append((" " * len(prefix) + chunk, channel, level))

        return rows

    def add(self, y: int, x: int, text: str, attr: int = 0):
        height, width = self.stdscr.getmaxyx()

        if y < 0 or y >= height or x < 0 or x >= width:
            return

        try:
            self.stdscr.addnstr(y, x, str(text), max(0, width - x - 1), attr)
        except curses.error:
            pass

    def box(self, y: int, x: int, h: int, w: int, title: str):
        if h < 3 or w < 6:
            return

        try:
            self.stdscr.attron(self.palette["muted"])
            self.stdscr.hline(y, x + 1, curses.ACS_HLINE, w - 2)
            self.stdscr.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
            self.stdscr.vline(y + 1, x, curses.ACS_VLINE, h - 2)
            self.stdscr.vline(y + 1, x + w - 1, curses.ACS_VLINE, h - 2)
            self.stdscr.addch(y, x, curses.ACS_ULCORNER)
            self.stdscr.addch(y, x + w - 1, curses.ACS_URCORNER)
            self.stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER)
            self.stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
            self.stdscr.attroff(self.palette["muted"])
        except curses.error:
            pass

        self.add(y, x + 2, " {} ".format(title), self.palette["header"])

    def draw(self):
        self.stdscr.erase()

        height, width = self.stdscr.getmaxyx()

        if height < 18 or width < 80:
            self.add(0, 0, "Terminal te klein. Minimaal 80x18.", self.palette["error"])
            self.stdscr.refresh()
            return

        spinner = SPINNER[int(time.time() * 10) % len(SPINNER)]
        busy = "{} {}".format(spinner, self.busy_label) if self.busy_label else "idle"
        queued = self.jobs.qsize()

        header = " Laravel dev terminal | http://{}:{} | {} | queue {} ".format(
            self.host,
            self.port,
            busy,
            queued,
        )

        self.add(0, 2, header[:width - 4], self.palette["header"] | curses.A_BOLD)
        self.add(1, 2, "Project: {}".format(self.root), self.palette["muted"])

        sidebar_w = min(40, max(32, width // 4))
        main_x = sidebar_w + 1
        main_w = width - main_x - 1
        panel_h = height - 4

        self.box(3, 0, panel_h, sidebar_w, "dashboard")
        self.draw_sidebar(4, 2, panel_h - 2, sidebar_w - 4)

        mode = "{} | {}".format("follow" if self.follow else "scroll", self.log_mode)

        if self.log_filter:
            mode += " | filter: {}".format(self.log_filter)

        self.box(3, main_x, panel_h, main_w, "output [{}]".format(mode))
        self.draw_output(4, main_x + 2, panel_h - 2, main_w - 4)

        footer = (
            self.flash
            if time.time() < self.flash_until
            else "r serve restart | i port info | j kill other serve | u vite restart | d build | t test | m migrate | p pint | g laravel.log | : command | / filter | q quit"
        )

        version = "dev-terminal {}".format(APP_VERSION)
        version_x = max(2, width - len(version) - 2)
        footer_width = max(0, version_x - 4)
        footer_attr = self.palette["warn"] if time.time() < self.flash_until else self.palette["muted"]

        self.add(height - 1, 2, footer[:footer_width], footer_attr)
        self.add(height - 1, version_x, version, self.palette["header"])
        self.stdscr.refresh()

    def draw_sidebar(self, y: int, x: int, h: int, w: int):
        line = y

        self.add(line, x, "PROCESSES", self.palette["header"])
        line += 1

        for proc in [self.serve, self.vite]:
            attr = self.palette["ok"] if proc.status == "running" else self.palette["error"] if proc.status == "failed" else self.palette["warn"]
            self.add(line, x, "{:<6} {:<8} pid {}".format(proc.name, proc.status, proc.pid or "-")[:w], attr)
            line += 1

        line += 1
        self.add(line, x, "HEALTH", self.palette["header"])
        line += 1

        health_attr = self.palette["ok"] if self.health.startswith("http") else self.palette["warn"]
        self.add(line, x, self.health[:w], health_attr)
        line += 2

        self.add(line, x, "TASKS", self.palette["header"])
        line += 1

        for key in ["optimize", "build", "version", "test", "migrate", "pint"]:
            task = self.tasks[key]
            status = task.status

            if status == "running" and task.started_at:
                status = "running {}s".format(int(time.time() - task.started_at))

            attr = self.palette["ok"] if task.status == "ok" else self.palette["error"] if task.status == "failed" else self.palette["task"] if task.status == "running" else self.palette["muted"]
            self.add(line, x, "{:<9} {}".format(key, status)[:w], attr)
            line += 1

            if line >= y + h - 10:
                break

        line += 1
        self.add(line, x, "KEYS", self.palette["header"])
        line += 1

        keys = [
            ("r", "serve restart"),
            ("s/x", "serve start/stop"),
            ("i", "port info"),
            ("j", "kill other serve"),
            ("u/y", "vite restart/stop"),
            ("d", "build"),
            ("v", "version"),
            ("o/k", "optimize/clear"),
            ("t", "test"),
            ("m", "migrate"),
            ("p", "pint"),
            ("g", "laravel.log"),
            (":", "custom command"),
            ("/", "filter"),
            ("l", "log mode"),
            ("q", "quit"),
        ]

        for key, description in keys:
            if line >= y + h - 3:
                break
            self.add(line, x, "{:<4} {}".format(key, description)[:w], self.palette["muted"])
            line += 1

        if line < y + h - 1:
            line += 1
            self.add(line, x, "LOGS", self.palette["header"])
            line += 1
            self.add(line, x, "storage/logs/dev-terminal"[:w], self.palette["muted"])

    def draw_output(self, y: int, x: int, h: int, w: int):
        rows = self.build_rows(w)
        total = len(rows)
        visible = max(1, h - 1)

        max_offset = max(0, total - visible)
        self.scroll_offset = min(self.scroll_offset, max_offset)

        if self.follow:
            start = max(0, total - visible)
        else:
            start = max(0, total - visible - self.scroll_offset)

        selected = rows[start:start + visible]

        if not selected:
            self.add(y, x, "Nog geen output.", self.palette["muted"])
            return

        for index, row in enumerate(selected):
            text, channel, level = row

            attr = curses.A_NORMAL

            if level == "ok":
                attr = self.palette["ok"]
            elif level == "error":
                attr = self.palette["error"]
            elif level == "warn":
                attr = self.palette["warn"]
            elif channel == "serve":
                attr = self.palette["serve"]
            elif channel == "vite":
                attr = self.palette["vite"]
            elif channel == "task":
                attr = self.palette["task"]
            elif channel == "warn":
                attr = self.palette["warn"]

            self.add(y + index, x, text[:w], attr)


def main(stdscr):
    app = DevTerminal(stdscr)
    app.start()


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
