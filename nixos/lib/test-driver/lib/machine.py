from contextlib import contextmanager, _GeneratorContextManager
from queue import Queue
from typing import Tuple, Any, Callable, Dict, Optional, List, Iterable
import queue
import io
import _thread
import base64
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import telnetlib
import time

from pprint import pprint
from pathlib import Path

CHAR_TO_KEY = {
    "A": "shift-a",
    "N": "shift-n",
    "-": "0x0C",
    "_": "shift-0x0C",
    "B": "shift-b",
    "O": "shift-o",
    "=": "0x0D",
    "+": "shift-0x0D",
    "C": "shift-c",
    "P": "shift-p",
    "[": "0x1A",
    "{": "shift-0x1A",
    "D": "shift-d",
    "Q": "shift-q",
    "]": "0x1B",
    "}": "shift-0x1B",
    "E": "shift-e",
    "R": "shift-r",
    ";": "0x27",
    ":": "shift-0x27",
    "F": "shift-f",
    "S": "shift-s",
    "'": "0x28",
    '"': "shift-0x28",
    "G": "shift-g",
    "T": "shift-t",
    "`": "0x29",
    "~": "shift-0x29",
    "H": "shift-h",
    "U": "shift-u",
    "\\": "0x2B",
    "|": "shift-0x2B",
    "I": "shift-i",
    "V": "shift-v",
    ",": "0x33",
    "<": "shift-0x33",
    "J": "shift-j",
    "W": "shift-w",
    ".": "0x34",
    ">": "shift-0x34",
    "K": "shift-k",
    "X": "shift-x",
    "/": "0x35",
    "?": "shift-0x35",
    "L": "shift-l",
    "Y": "shift-y",
    " ": "spc",
    "M": "shift-m",
    "Z": "shift-z",
    "\n": "ret",
    "!": "shift-0x02",
    "@": "shift-0x03",
    "#": "shift-0x04",
    "$": "shift-0x05",
    "%": "shift-0x06",
    "^": "shift-0x07",
    "&": "shift-0x08",
    "*": "shift-0x09",
    "(": "shift-0x0A",
    ")": "shift-0x0B",
}


def make_command(args: list) -> str:
    return " ".join(map(shlex.quote, (map(str, args))))


def retry(fn: Callable, timeout: int = 900) -> None:
    """Call the given function repeatedly, with 1 second intervals,
    until it returns True or a timeout is reached.
    """

    for _ in range(timeout):
        if fn(False):
            return
        time.sleep(1)

    if not fn(True):
        raise Exception(f"action timed out after {timeout} seconds")


def _perform_ocr_on_screenshot(
    screenshot_path: str, model_ids: Iterable[int]
) -> List[str]:
    if shutil.which("tesseract") is None:
        raise Exception("OCR requested but enableOCR is false")

    magick_args = (
        "-filter Catrom -density 72 -resample 300 "
        + "-contrast -normalize -despeckle -type grayscale "
        + "-sharpen 1 -posterize 3 -negate -gamma 100 "
        + "-blur 1x65535"
    )

    tess_args = f"-c debug_file=/dev/null --psm 11"

    cmd = f"convert {magick_args} {screenshot_path} tiff:{screenshot_path}.tiff"
    ret = subprocess.run(cmd, shell=True, capture_output=True)
    if ret.returncode != 0:
        raise Exception(f"TIFF conversion failed with exit code {ret.returncode}")

    model_results = []
    for model_id in model_ids:
        cmd = f"tesseract {screenshot_path}.tiff - {tess_args} --oem {model_id}"
        ret = subprocess.run(cmd, shell=True, capture_output=True)
        if ret.returncode != 0:
            raise Exception(f"OCR failed with exit code {ret.returncode}")
        model_results.append(ret.stdout.decode("utf-8"))

    return model_results


class BaseStartCommand:
    def __init__(self):
        pass

    def cmd(
        self,
        monitor_socket_path: Path,
        shell_socket_path: Path,
        allow_reboot: bool = False,  # TODO: unused, legacy?
        tty_path: Optional[Path] = None,  # TODO: currently unused
    ):
        tty_opts = ""
        if tty_path is not None:
            tty_opts += (
                f" -chardev socket,server,nowait,id=console,path={tty_path}"
                " -device virtconsole,chardev=console"
            )
        display_opts = ""
        display_available = any(
            x in os.environ for x in ["DISPLAY", "WAYLAND_DISPLAY"])
        if display_available:
            display_opts += " -nographic"

        # qemu options
        qemu_opts = ""
        qemu_opts += (
            "" if allow_reboot else " -no-reboot"
            " -device virtio-serial"
            " -device virtconsole,chardev=shell"
            " -serial stdio"
        )
        # TODO: qemu script already catpures this env variable, legacy?
        qemu_opts += " " + os.environ.get("QEMU_OPTS", "")

        return (
            f"{self._cmd}"
            f" -monitor unix:{monitor_socket_path}"
            f" -chardev socket,id=shell,path={shell_socket_path}"
            f"{tty_opts}{display_opts}"
        )

    @staticmethod
    def build_environment(
        state_dir: Path,
        shared_dir: Path,
    ):
        return dict(os.environ).update(
            {
                "TMPDIR": state_dir,
                "SHARED_DIR": shared_dir,
                "USE_TMPDIR": "1",
            }
        )

    def run(
        self,
        state_dir: Path,
        shared_dir: Path,
        monitor_socket_path: Path,
        shell_socket_path: Path,
    ):
        return subprocess.Popen(
            self.cmd(monitor_socket_path, shell_socket_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            cwd=state_dir,
            env=self.build_environment(state_dir, shared_dir),
        )


class NixStartScript(BaseStartCommand):
    """A start script from nixos/modules/virtualiation/qemu-vm.nix

    Note, that any dynamically appended flags in self.cmd are are passed
    to the qemu bin by the script's final "${@}"
    """

    def __init__(self, script: str):
        self._cmd = script

    @property
    def machine_name(self):
        match = re.search("run-(.+)-vm$", self._cmd)
        name = "machine"
        if match:
            name = match.group(1)
        return name


class Machine:
    """A handle to the machine with this name
    """
    def __init__(
        self,
        log_serial: Callable,
        log_machinestate: Callable,
        tmp_dir: Path,
        start_command: BaseStartCommand,
        name: str = "machine",
        keep_vm_state: bool = False,
        allow_reboot: bool = False,
    ) -> None:
        self.log_serial = lambda msg: log_serial(
            f"[{name} LOG] {msg}", {"machine": name})
        self.log_machinestate = lambda msg: log_machinestate(
            f"[{name} MCS] {msg}", {"machine": name})
        self.tmp_dir = tmp_dir
        self.keep_vm_state = keep_vm_state
        self.allow_reboot = allow_reboot
        self.name = name
        self.start_command = start_command

        # in order to both enable plain log functions, but also nested
        # machine state logging, check if the `log_machinestate` object
        # has a method `nested` and provide this functionality.
        # Ideally, just remove this logic long term and make it as  easy as
        # possible for the user of `Machine` to provide plain standard loggers
        @contextmanager
        def dummy_nest(message: str) -> _GeneratorContextManager:
            self.log_machinestate(message)
            yield

        nest_op = getattr(self.log_machinestate, "nested", None)
        if callable(nest_op):
            self.nested = nest_op
        else:
            self.nested = dummy_nest

        # set up directories
        self.shared_dir = (self.tmp_dir / "shared-xchg")
        self.shared_dir.mkdir(mode=0o700, exist_ok=True)

        self.state_dir = self.tmp_dir / f"vm-state-{self.name}"
        self.monitor_path = self.state_dir / "monitor"
        self.shell_path = self.state_dir / "shell"
        if (not self.keep_vm_state) and self.state_dir.exists():
            shutil.rmtree(self.state_dir)
            log_machinestate(  # trick: shouldn't be a machine specific log
               f"    -> delete state @ {self.state_dir}"
            )
        self.state_dir.mkdir(mode=0o700, exist_ok=True)

        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.monitor: Optional[socket.socket] = None
        self.shell: Optional[socket.socket] = None

        self.booted = False
        self.connected = False
        # Store last serial console lines for use
        # of wait_for_console_text
        self.last_lines: Queue = Queue()

    def _wait_for_monitor_prompt(self) -> str:
        assert self.monitor is not None
        answer = ""
        while True:
            undecoded_answer = self.monitor.recv(1024)
            if not undecoded_answer:
                break
            answer += undecoded_answer.decode()
            if answer.endswith("(qemu) "):
                break
        return answer

    def _wait_for_shutdown(self) -> None:
        if not self.booted:
            return

        with self.nested("wait for the VM to power off"):
            sys.stdout.flush()
            self.process.wait()

            self.pid = None
            self.booted = False
            self.connected = False

    def start(self) -> None:
        """Start this machine
        """
        if self.booted:
            return

        self.log_machinestate("start")

        def clear(path: Path) -> Path:
            if path.exists():
                path.unlink()
            return path

        def create_socket(path: Path) -> socket.socket:
            s = socket.socket(family=socket.AF_UNIX, type=socket.SOCK_STREAM)
            s.bind(str(path))
            s.listen(1)
            return s

        monitor_socket = create_socket(clear(self.monitor_path))
        shell_socket = create_socket(clear(self.shell_path))
        self.process = self.start_command.run(
            self.state_dir, self.shared_dir,
            self.monitor_path, self.shell_path,
        )
        self.monitor, _ = monitor_socket.accept()
        self.shell, _ = shell_socket.accept()

        def process_serial_output() -> None:
            assert self.process.stdout is not None
            for _line in self.process.stdout:
                # Ignore undecodable bytes that may occur in boot menus
                line = _line.decode(errors="ignore").replace("\r", "").rstrip()
                self.last_lines.put(line)
                self.log_serial(line)

        _thread.start_new_thread(process_serial_output, ())

        self._wait_for_monitor_prompt()

        self.pid = self.process.pid
        self.booted = True

        self.log_machinestate(f"QEMU running (pid {self.pid})")

    def release(self) -> bool:
        """Kill this machine
        """
        if self.pid is None:
            return
        self.log_machinestate(f"kill me (pid {self.pid})")
        self.process.kill()

    def connect(self) -> None:
        """Connect to this machine's root shell
        """
        if self.connected:
            return

        with self.nested("wait for the VM to finish booting"):
            self.start()

            self.log_machinestate("connect to guest root shell")
            tic = time.time()
            self.shell.recv(1024)
            # TODO: Timeout
            toc = time.time()
            self.log_machinestate(f"(took {(toc - tic):.2f} seconds)")
            self.connected = True

    def shutdown(self) -> None:
        """Shut down this machine
        """
        if not self.booted:
            return

        self.log_machinestate("regular shutdown")
        self.shell.send("poweroff\n".encode())
        self._wait_for_shutdown()

    def crash(self) -> None:
        """Simulate to crash this machine
        """
        if not self.booted:
            return

        self.log_machinestate("simulate forced crash")
        self.send_monitor_command("quit")
        self._wait_for_shutdown()

    def block(self) -> None:
        """Make this machine unreachable by shutting down eth1 (the multicast
        interface used to talk to the other VMs).  We keep eth0 up so that
        the test driver can continue to talk to the machine.
        """
        self.send_monitor_command("set_link virtio-net-pci.1 off")

    def unblock(self) -> None:
        """Make this machine reachable.
        """
        self.send_monitor_command("set_link virtio-net-pci.1 on")

    def is_up(self) -> bool:
        """Wether this machine is booted and it's root shell is connected
        """
        return self.booted and self.connected

    def send_monitor_command(self, command: str) -> str:
        """Send a low level monitor command to this machine
        """
        message = (f"{command}\n").encode()
        self.log_machinestate("send monitor command: {command}")
        assert self.monitor is not None
        self.monitor.send(message)
        return self._wait_for_monitor_prompt()

    def wait_for_unit(self, unit: str, user: Optional[str] = None) -> None:
        """Wait for a systemd unit to get into "active" state.
        Throws exceptions on "failed" and "inactive" states as well as
        after timing out.
        """

        def check_active(_: Any) -> bool:
            info = self.get_unit_info(unit, user)
            state = info["ActiveState"]
            if state == "failed":
                raise Exception(f'unit "{unit}" reached state "{state}"')

            if state == "inactive":
                status, jobs = self.systemctl("list-jobs --full 2>&1", user)
                if "No jobs" in jobs:
                    info = self.get_unit_info(unit, user)
                    if info["ActiveState"] == state:
                        raise Exception(
                            f'unit "{unit}" is inactive and there '
                            "are no pending jobs"
                        )

            return state == "active"

        retry(check_active)

    def get_unit_info(self, unit: str, user: Optional[str] = None) -> Dict[str, str]:
        """Get information for a unit on this machine.
        Optionally provide a user to query a unit running under that user.
        """
        status, lines = self.systemctl(f'--no-pager show "{unit}"', user)
        if status != 0:
            user_str = "" if user is None else f'under user "{user}"'
            raise Exception(
                f'retrieving systemctl info for unit "{unit}" {user_str}'
                f"failed with exit code {status}"
            )

        line_pattern = re.compile(r"^([^=]+)=(.*)$")

        def tuple_from_line(line: str) -> Tuple[str, str]:
            match = line_pattern.match(line)
            assert match is not None
            return match[1], match[2]

        return dict(
            tuple_from_line(line)
            for line in lines.split("\n")
            if line_pattern.match(line)
        )

    def systemctl(self, q: str, user: Optional[str] = None) -> Tuple[int, str]:
        """Execute a low level systemctl query on this machine.
        Optionally provide a user to query within the scope of that user.
        """
        if user is not None:
            q = q.replace("'", "\\'")
            return self.execute(
                (
                    f"su -l {user} --shell /bin/sh -c "
                    "$'XDG_RUNTIME_DIR=/run/user/`id -u` "
                    f"systemctl --user {q}'"
                )
            )
        return self.execute(f"systemctl {q}")

    def require_unit_state(self, unit: str, require_state: str = "active") -> None:
        """Wether a unit has reached a specified state ("active" by default)
        """
        with self.nested(
            f"check if unit ‘{unit}’ has reached state '{require_state}'"
        ):
            info = self.get_unit_info(unit)
            state = info["ActiveState"]
            if state != require_state:
                raise Exception(
                    f"Expected unit ‘{unit}’ to to be in state "
                    f"'{require_state}' but it is in state ‘{state}’"
                )

    def execute(self, command: str) -> Tuple[int, str]:
        """Execute a shell command on this machine.
        """
        self.connect()

        out_command = f"( {command} ); echo '|!=EOF' $?\n"
        self.shell.send(out_command.encode())

        output = ""
        status_code_pattern = re.compile(r"(.*)\|\!=EOF\s+(\d+)")

        while True:
            chunk = self.shell.recv(4096).decode(errors="ignore")
            match = status_code_pattern.match(chunk)
            if match:
                output += match[1]
                status_code = int(match[2])
                return (status_code, pprint(output))
            output += chunk

    def shell_interact(self) -> None:
        """Allows you to interact with the guest shell
        Should only be used during testing, not in the production test."""
        self.connect()
        assert self.shell
        telnet = telnetlib.Telnet()
        telnet.sock = self.shell
        telnet.interact()

    def succeed(self, *commands: str) -> str:
        """Execute each command and check that it succeeds."""
        output = ""
        for command in commands:
            with self.nested(f"must succeed: {command}"):
                status, out = self.execute(command)
                if status != 0:
                    self.log_machinestate(f"output: {out}")
                    raise Exception(
                        f"command `{command}` failed (exit code {status})"
                    )
                output += out
        return output

    def fail(self, *commands: str) -> str:
        """Execute each command and check that it fails."""
        output = ""
        for command in commands:
            with self.nested(f"must fail: {command}"):
                (status, out) = self.execute(command)
                if status == 0:
                    raise Exception(
                        f"command `{command}` unexpectedly succeeded"
                    )
                output += out
        return output

    def wait_until_succeeds(self, command: str) -> str:
        """Wait until a command returns success and return its output.
        Throws an exception on timeout.
        """
        output = ""

        def check_success(_: Any) -> bool:
            nonlocal output
            status, output = self.execute(command)
            return status == 0

        with self.nested(f"wait for success: {command}"):
            retry(check_success)
        return output

    def wait_until_fails(self, command: str) -> str:
        """Wait until a command returns failure.
        Throws an exception on timeout.
        """
        output = ""

        def check_failure(_: Any) -> bool:
            nonlocal output
            status, output = self.execute(command)
            return status != 0

        with self.nested(f"wait for failure: {command}"):
            retry(check_failure)
        return output

    def get_tty_text(self, tty: str) -> str:
        """Obtain text from a specified tty of this machine
        """
        status, output = self.execute(
            f"fold -w$(stty -F /dev/tty{tty} size | "
            f"awk '{{print $2}}') /dev/vcs{tty}"
        )
        return output

    def wait_until_tty_matches(self, tty: str, regexp: str) -> None:
        """Wait until the visible output on the chosen TTY matches regular
        expression. Throws an exception on timeout.
        """
        matcher = re.compile(regexp)

        def tty_matches(last: bool) -> bool:
            text = self.get_tty_text(tty)
            res = len(matcher.findall(text)) > 0
            if res:
                return res
            if last:
                self.log_machinestate(
                    f"Last attempt failed to match /{regexp}/ on TTY{tty}:"
                    f"Current text was: \n\n{text}"
                )
            return False

        with self.nested(f"wait for {regexp} to appear on tty {tty}"):
            retry(tty_matches)

    def send_chars(self, chars: List[str]) -> None:
        """Send characters to this machine
        """
        with self.nested(f"send keys ‘{chars}‘"):
            for char in chars:
                self.send_key(char)

    def wait_for_file(self, filename: str) -> None:
        """Waits until the specified file exists in machine's file system."""

        def check_file(_: Any) -> bool:
            status, _ = self.execute(f"test -e {filename}")
            return status == 0

        with self.nested(f"wait for file ‘{filename}‘"):
            retry(check_file)

    def wait_for_open_port(self, port: int) -> None:
        """Waits until the specified port is opened on this machine."""
        def port_is_open(_: Any) -> bool:
            status, _ = self.execute(f"nc -z localhost {port}")
            return status == 0

        with self.nested(f"wait for TCP port {port}"):
            retry(port_is_open)

    def wait_for_closed_port(self, port: int) -> None:
        """Waits until the specified port is closed on this machine."""
        def port_is_closed(_: Any) -> bool:
            status, _ = self.execute(f"nc -z localhost {port}")
            return status != 0

        retry(port_is_closed)

    def start_job(self, jobname: str, user: Optional[str] = None) -> Tuple[int, str]:
        """Starts a systemctl job on this machine
        """
        return self.systemctl(f"start {jobname}", user)

    def stop_job(self, jobname: str, user: Optional[str] = None) -> Tuple[int, str]:
        """Stops a systemctl job on this machine
        """
        return self.systemctl(f"stop {jobname}", user)

    def wait_for_job(self, jobname: str) -> None:
        """Alias as wait for units
        """
        self.wait_for_unit(jobname)

    def screenshot(self, filename: str) -> None:
        """Take a screenshot from this machine and place it in
        the current directory under the specified filename
        (or into $out when called from within a derivation)
        """
        out_dir = Path(os.environ.get("out", Path.cwd()))
        word_pattern = re.compile(r"^\w+$")
        if word_pattern.match(filename):
            filename = out_dir / f"{filename}.png"
        tmp = Path(f"{filename}.ppm")

        with self.nested(
            f"make screenshot {filename}",
            {"image": filename.name},
        ):
            self.send_monitor_command(f"screendump {tmp}")
            ret = subprocess.run(f"pnmtopng {tmp} > {filename}", shell=True)
            tmp.unlink()
            if ret.returncode != 0:
                raise Exception("Cannot convert screenshot")

    def copy_from_host_via_shell(self, source: str, target: str) -> None:
        """Copy a file from the host into the machine by piping it over the
        shell into the destination file. Works without host-guest shared folder.
        Prefer copy_from_host whenever possible.
        """
        with open(source, "rb") as fh:
            content_b64 = base64.b64encode(fh.read()).decode()
            self.succeed(
                f"mkdir -p $(dirname {target})",
                f"echo -n {content_b64} | base64 -d > {target}",
            )

    def copy_from_host(self, source: str, target: str) -> None:
        """Copy a file from the host into the machine via the `shared_dir` shared
        among all the VMs (using a temporary directory).
        """
        host_src = Path(source)
        vm_target = Path(target)
        with tempfile.TemporaryDirectory(dir=self.shared_dir) as shared_td:
            shared_temp = Path(shared_td)
            host_intermediate = shared_temp / host_src.name
            vm_shared_temp = Path("/tmp/shared") / shared_temp.name
            vm_intermediate = vm_shared_temp / host_src.name

            self.succeed(make_command(["mkdir", "-p", vm_shared_temp]))
            if host_src.is_dir():
                shutil.copytree(host_src, host_intermediate)
            else:
                shutil.copy(host_src, host_intermediate)
            self.succeed(make_command(["mkdir", "-p", vm_target.parent]))
            self.succeed(make_command(["cp", "-r", vm_intermediate, vm_target]))

    def copy_from_vm(self, source: str, target_dir: str = "") -> None:
        """Copy a file from the machine into the host via the `shared_dir`
        shared among all the VMs (using a temporary directory). The target
        file is specified relative to the current directory
        (or into $out when called from within a derivation)
        """
        # Compute the source, target, and intermediate shared file names
        out_dir = Path(os.environ.get("out", Path.cwd()))
        vm_src = Path(source)
        with tempfile.TemporaryDirectory(dir=self.shared_dir) as shared_td:
            shared_temp = Path(shared_td)
            vm_shared_temp = Path("/tmp/shared") / shared_temp.name
            vm_intermediate = vm_shared_temp / vm_src.name
            intermediate = shared_temp / vm_src.name
            # Copy the file to the shared directory inside VM
            self.succeed(make_command(["mkdir", "-p", vm_shared_temp]))
            self.succeed(make_command(["cp", "-r", vm_src, vm_intermediate]))
            abs_target = out_dir / target_dir / vm_src.name
            abs_target.parent.mkdir(exist_ok=True, parents=True)
            # Copy the file from the shared directory outside VM
            if intermediate.is_dir():
                shutil.copytree(intermediate, abs_target)
            else:
                shutil.copy(intermediate, abs_target)

    def dump_tty_contents(self, tty: str) -> None:
        """Debugging: Dump the contents of the TTY<n>
        """
        self.execute(f"fold -w 80 /dev/vcs{tty} | systemd-cat")

    def _get_screen_text_variants(self, model_ids: Iterable[int]) -> List[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            screenshot_path = tmpdir / "ppm"
            self.send_monitor_command(f"screendump {screenshot_path}")
            return _perform_ocr_on_screenshot(screenshot_path, model_ids)

    def get_screen_text_variants(self) -> List[str]:
        return self._get_screen_text_variants([0, 1, 2])

    def get_screen_text(self) -> str:
        return self._get_screen_text_variants([2])[0]

    def wait_for_text(self, regex: str) -> None:
        def screen_matches(last: bool) -> bool:
            variants = self.get_screen_text_variants()
            for text in variants:
                if re.search(regex, text) is not None:
                    return True

            if last:
                self.log_machinestate(f"Last OCR attempt failed. Text was: {variants}")

            return False

        with self.nested(f"wait for {regex} to appear on screen"):
            retry(screen_matches)

    def wait_for_console_text(self, regex: str) -> None:
        """Waits until regex matches this machine's console output.
        Can match multiple lines.
        """
        self.log_machinestate(f"wait for {regex} to appear on console")
        # Buffer the console output, this is needed
        # to match multiline regexes.
        console = io.StringIO()
        while True:
            try:
                console.write(self.last_lines.get())
            except queue.Empty:
                self.sleep(1)
                continue
            console.seek(0)
            matches = re.search(regex, console.read())
            if matches is not None:
                return

    def send_key(self, key: str) -> None:
        """Send a key to the machine (low level).
        Keys are mapped over a compatibility map.
        """
        key = CHAR_TO_KEY.get(key, key)
        self.send_monitor_command(f"sendkey {key}")

    def wait_for_x(self) -> None:
        """Wait until it is possible to connect to the X server.  Note that
        testing the existence of /tmp/.X11-unix/X0 is insufficient.
        """

        def check_x(_: Any) -> bool:
            cmd = (
                "journalctl -b SYSLOG_IDENTIFIER=systemd | "
                + 'grep "Reached target Current graphical"'
            )
            status, _ = self.execute(cmd)
            if status != 0:
                return False
            status, _ = self.execute("[ -e /tmp/.X11-unix/X0 ]")
            return status == 0

        with self.nested("wait for the X11 server"):
            retry(check_x)

    def get_window_names(self) -> List[str]:
        """Retrieve the names of the open windows of this machine via
        'xwininfo'

        CAVE: does not work on wayland hosts.
        """
        return self.succeed(
            r"xwininfo -root -tree | sed 's/.*0x[0-9a-f]* \"\([^\"]*\)\".*/\1/; t; d'"
        ).splitlines()

    def wait_for_window(self, regexp: str) -> None:
        """Wait until a window apprers in the machine that matches
        the given regex. Windows optained via 'xwininfo'.

        CAVE: does not work on wayland hosts.
        """
        pattern = re.compile(regexp)

        def window_is_visible(last: bool) -> bool:
            names = self.get_window_names()
            res = any(pattern.search(name) for name in names)
            if res:
                return res
            if last:
                self.log_machinestate(
                    f"Last attempt failed to match {regexp} on the window list,"
                    " which currently contains: "
                    ", ".join(names)
                )
            return False

        with self.nested("Wait for a window to appear"):
            retry(window_is_visible)

    def sleep(self, secs: int) -> None:
        """Sleep the machine for x nr of seconds
        """
        # We want to sleep in *guest* time, not *host* time.
        self.succeed(f"sleep {secs}")

    def forward_port(self, host_port: int = 8080, guest_port: int = 80) -> None:
        """Forward a TCP port on the host to a TCP port on the guest.
        Useful during interactive testing.
        """
        self.send_monitor_command(
            f"hostfwd_add tcp::{host_port}-:{guest_port}"
        )
