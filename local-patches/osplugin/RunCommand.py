import functools
import glob
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from configparser import ConfigParser

from loguru import logger as log

# Import gtk modules
import gi
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

LABEL_POSITIONS = ["top", "center", "bottom"]


class RunCommand(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.has_configuration = True

        self.auto_run_timer: threading.Timer = None

        self.registered_down: bool = False # Temporary workaround for an issue in the app causing the action to get the short_up event if it's on the same key, but different page as a "change page" action that triggers the change #TODO

    def on_ready(self):
        self.set_media(media_path=os.path.join(self.plugin_base.PATH, "assets", "terminal.png"))
        self.start_timer()
        # Reset registered_down flag to prevent action from being blocked after page changes
        self.registered_down = False

    def stop_timer(self):
        if self.auto_run_timer is not None:
            self.auto_run_timer.cancel()

    def start_timer(self):
        self.stop_timer()
        settings = self.get_settings()
        if settings.get("auto_run", 0) <= 0:
            return
        self.auto_run_timer = threading.Timer(settings.get("auto_run", 0), self.execute, args=(True,))
        self.auto_run_timer.setDaemon(True)
        self.auto_run_timer.setName("AutoRunTimer")
        self.auto_run_timer.start()

    def event_callback(self, event, data):
        # Workaround for issue described in line 24
        if event == Input.Key.Events.DOWN:
            if self.registered_down:
                return
            else:
                self.registered_down = True
        elif event == Input.Key.Events.UP:
            self.registered_down = False
        # End workaround

        if self.get_settings().get("auto_run", 0) <= 0:
            if event == Input.Key.Events.DOWN:
                self.execute()
        else:
            if event == Input.Key.Events.HOLD_START:
                if self.auto_run_timer is not None:
                    self.stop_timer()
            elif event == Input.Key.Events.HOLD_STOP:
                # The timer got stopped on HOLD_START, resume it after the key has been released
                self.start_timer()
            elif event == Input.Key.Events.SHORT_UP:
                self.execute(restart_timer=self.get_settings().get("auto_run", 0) > 0)

    def execute(self, restart_timer: bool = False):
        self.stop_timer()
        settings = self.get_settings()

        if settings.get("display_output", False) and settings.get("clear_output", False):
            self.set_output_label("")

        cmd = settings.get("command", None)
        env_preview = resolve_gui_env()
        log.info(
            f"[first-press] RunCommand.execute cmd={cmd!r} "
            f"detached={settings.get('detached', True)} "
            f"display_output={settings.get('display_output', False)} "
            f"thread={threading.current_thread().name} "
            f"DISPLAY={env_preview.get('DISPLAY')!r} "
            f"WAYLAND_DISPLAY={env_preview.get('WAYLAND_DISPLAY')!r} "
            f"XAUTHORITY={env_preview.get('XAUTHORITY')!r} "
            f"DBUS_SESSION_BUS_ADDRESS={env_preview.get('DBUS_SESSION_BUS_ADDRESS')!r} "
            f"XDG_RUNTIME_DIR={env_preview.get('XDG_RUNTIME_DIR')!r} "
            f"cwd_will_be={os.path.expanduser('~')!r}"
        )
        result = self.run_command(cmd)
        if settings.get("display_output", False):
            self.set_output_label(result)

        if restart_timer:
            if self.get_is_present() or settings.get("keep_auto_run_in_background", False):
                self.start_timer()

    def set_output_label(self, text: str):
        self.set_label(text, position=self.get_label_position())

    def get_label_position(self) -> str:
        position = self.get_settings().get("label_position", "center")
        if position not in LABEL_POSITIONS:
            position = "center"
        return position

    def get_config_rows(self):
        entry_row = Adw.EntryRow(title=self.plugin_base.lm.get("run.entry.title"))
        self.display_output_switch = Adw.SwitchRow(
            title=self.plugin_base.lm.get("run.display-output.title"),
            subtitle=self.plugin_base.lm.get("run.display-output.subtitle"))
        self.clear_output_switch = Adw.SwitchRow(
            title=self.plugin_base.lm.get("run.clear-output.title"),
            subtitle=self.plugin_base.lm.get("run.clear-output.subtitle"))
        self.detached_switch = Adw.SwitchRow(title=self.plugin_base.lm.get("run.detached.title"),
                                             subtitle=self.plugin_base.lm.get(
                                                 "run.detached.subtitle"))
        
        self.label_position_row = Adw.ComboRow(
            title=self.plugin_base.lm.get("run.label-position.title"),
            subtitle=self.plugin_base.lm.get("run.label-position.subtitle"),
            model=Gtk.StringList.new([self.plugin_base.lm.get(f"run.label-position.{position}") for position in LABEL_POSITIONS]))

        self.interactive_shell_switch = Adw.SwitchRow(
            title=self.plugin_base.lm.get("run.interactive-shell.title"),
            subtitle=self.plugin_base.lm.get("run.interactive-shell.subtitle"))

        self.auto_run_row = Adw.SpinRow.new_with_range(0, 60, 0.1)
        self.auto_run_row.set_title("Auto run every (s)")
        self.auto_run_row.set_subtitle("Auto run command automatically (0 to disable)")

        self.keep_auto_run_in_background = Adw.SwitchRow(title="Keep auto running in background",
                                                         subtitle="Keep auto running when other page is active")

        # Load from config
        settings = self.get_settings()
        command = settings.setdefault("command", None)
        self.set_settings(settings)

        if command is None:
            command = ""
        entry_row.set_text(command)

        self.display_output_switch.set_active(settings.get("display_output", False))
        self.clear_output_switch.set_active(settings.get("clear_output", False))
        self.clear_output_switch.set_sensitive(settings.get("display_output", False))
        self.label_position_row.set_selected(LABEL_POSITIONS.index(self.get_label_position()))
        self.label_position_row.set_sensitive(settings.get("display_output", False))
        self.detached_switch.set_active(settings.get("detached", True))
        self.interactive_shell_switch.set_active(settings.get("interactive_shell", False))
        self.auto_run_row.set_value(settings.get("auto_run", 0))

        self.keep_auto_run_in_background.set_active(settings.get("keep_auto_run_in_background", False))

        # Connect entry
        entry_row.connect("notify::text", self.on_change_command)
        self.display_output_switch.connect("notify::active", self.on_display_output_changed)
        self.clear_output_switch.connect("notify::active", self.on_clear_output_changed)
        self.label_position_row.connect("notify::selected", self.on_label_position_changed)
        self.detached_switch.connect("notify::active", self.on_detached_changed)
        self.interactive_shell_switch.connect("notify::active", self.on_interactive_shell_changed)
        self.auto_run_row.connect("changed", self.on_auto_run_changed)
        self.keep_auto_run_in_background.connect("notify::active", self.on_keep_auto_run_in_background_changed)

        return [entry_row, self.display_output_switch, self.clear_output_switch, self.label_position_row, self.detached_switch, self.interactive_shell_switch, self.auto_run_row, self.keep_auto_run_in_background]
    
    def on_auto_run_changed(self, spin):
        settings = self.get_settings()
        settings["auto_run"] = round(spin.get_value(), 1)
        self.set_settings(settings)
        self.start_timer()

    def on_change_command(self, entry, _):
        settings = self.get_settings()
        settings["command"] = entry.get_text()
        self.set_settings(settings)

    def on_display_output_changed(self, switch, _):
        settings = self.get_settings()
        settings["display_output"] = switch.get_active()
        if switch.get_active():
            # can't display output AND run detached
            self.detached_switch.set_active(False)
        else:
            # remove possibly present label
            self.set_output_label("")
        self.clear_output_switch.set_sensitive(switch.get_active())
        self.label_position_row.set_sensitive(switch.get_active())
        self.set_settings(settings)

    def on_clear_output_changed(self, switch, _):
        settings = self.get_settings()
        settings["clear_output"] = switch.get_active()
        self.set_settings(settings)

    def on_label_position_changed(self, combo, _):
        position = LABEL_POSITIONS[combo.get_selected()]
        old_position = self.get_label_position()
        if position == old_position:
            return

        # remove the label at the old position
        self.set_label("", position=old_position)

        settings = self.get_settings()
        settings["label_position"] = position
        self.set_settings(settings)

    def on_detached_changed(self, switch, _):
        settings = self.get_settings()
        settings["detached"] = switch.get_active()
        if switch.get_active():
            # can't run detached AND display output
            self.display_output_switch.set_active(False)
        self.set_settings(settings)

    def on_interactive_shell_changed(self, switch, _):
        settings = self.get_settings()
        settings["interactive_shell"] = switch.get_active()
        self.set_settings(settings)

    def on_keep_auto_run_in_background_changed(self, switch, _):
        settings = self.get_settings()
        settings["keep_auto_run_in_background"] = switch.get_active()
        self.set_settings(settings)

    def run_command(self, command):
        if command is None:
            log.info("[first-press] RunCommand skipped: command is None")
            return

        original = command
        if self.get_settings().get("interactive_shell", False):
            # Run in an interactive shell so that aliases and functions from the user's rc files are available
            command = f"{shlex.quote(get_user_shell())} -ic {shlex.quote(command)}"

        # Prefer desktop/DBus activation for single-token GUI apps (e.g. cpupower-gui).
        # Raw binary start of a Gtk.Application can register on the bus without raising
        # a window; a later press/GUI click then "works" via present(). Desktop Exec for
        # cpupower-gui is `gapplication launch org.rnd2.cpupower_gui`.
        launch_method = "raw"
        if not self.get_settings().get("interactive_shell", False):
            rewritten, launch_method = rewrite_command_for_desktop_launch(command)
            if rewritten != command:
                log.info(
                    f"[first-press] RunCommand rewrite {original!r} -> {rewritten!r} "
                    f"via {launch_method}"
                )
                command = rewritten

        if is_in_flatpak():
            command = "flatpak-spawn --host " + command

        cwd = os.path.expanduser("~")
        detached = self.get_settings().get("detached", True)
        env = resolve_gui_env()
        if not env.get("XAUTHORITY") and env.get("DISPLAY"):
            log.warning(
                "[first-press] RunCommand: XAUTHORITY still unset after resolve; "
                "X11 GUI children may start without a visible window"
            )

        # Do NOT wrap Popen in multiprocessing.Process (fork under GTK/HID threads).
        activator = launch_method.split(":", 1)[0] in (
            "gapplication", "gtk-launch", "gio", "systemd-run",
        )
        try:
            if detached:
                err_file = tempfile.NamedTemporaryFile(
                    prefix="sc-runcommand-", suffix=".err", delete=False, mode="w+b"
                )
                err_path = err_file.name
                err_file.close()
                with open(err_path, "wb") as err_f:
                    proc = subprocess.Popen(
                        command,
                        shell=True,
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=err_f,
                        cwd=cwd,
                        env=env,
                    )
                log.info(
                    f"[first-press] RunCommand started pid={proc.pid} "
                    f"detached=True method={launch_method} cmd={command!r} "
                    f"XAUTHORITY={env.get('XAUTHORITY')!r} stderr_file={err_path}"
                )
                exited = False
                for label, delay in (("50ms", 0.05), ("200ms", 0.15)):
                    time.sleep(delay)
                    rc = proc.poll()
                    err_tail = _read_file_tail(err_path, 2000)
                    if rc is None:
                        log.info(
                            f"[first-press] RunCommand still running "
                            f"pid={proc.pid} at={label} method={launch_method}"
                        )
                    else:
                        # Activators often exit 0 quickly after DBus handoff — success.
                        level = log.info if (activator and rc == 0) else log.error
                        level(
                            f"[first-press] RunCommand exited pid={proc.pid} "
                            f"rc={rc} at={label} method={launch_method} "
                            f"cmd={command!r} stderr={err_tail!r}"
                        )
                        exited = True
                        break
                if not exited:
                    err_tail = _read_file_tail(err_path, 2000)
                    if err_tail:
                        log.warning(
                            f"[first-press] RunCommand stderr pid={proc.pid} "
                            f"cmd={command!r} stderr={err_tail!r}"
                        )
                return ""

            result = subprocess.Popen(
                command,
                shell=True,
                start_new_session=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            log.info(
                f"[first-press] RunCommand started pid={result.pid} "
                f"detached=False method={launch_method} cmd={command!r}"
            )
            out, err = result.communicate()
            out = (out or "").rstrip()
            err = (err or "").rstrip()
            if err:
                log.warning(
                    f"[first-press] RunCommand stderr pid={result.pid} "
                    f"rc={result.returncode} cmd={command!r} stderr={err!r}"
                )
            log.info(
                f"[first-press] RunCommand finished pid={result.pid} "
                f"rc={result.returncode} cmd={command!r}"
            )
            return out
        except Exception as e:
            log.exception(
                f"[first-press] RunCommand failed cmd={command!r} error={e}"
            )
            return ""


def _read_file_tail(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return ""
        return data[-max_bytes:].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _systemd_user_environment() -> dict:
    """Best-effort read of `systemctl --user show-environment` (session DISPLAY/XAUTH)."""
    out = {}
    try:
        cmd = ["systemctl", "--user", "show-environment"]
        if is_in_flatpak():
            cmd = ["flatpak-spawn", "--host", "--"] + cmd
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2, check=False
        )
        if proc.returncode != 0:
            return out
        for line in (proc.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key] = val
    except Exception:
        pass
    return out


def _find_runtime_xauthority() -> str | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    pattern = os.path.join(runtime, "xauth_*")
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    home = os.path.expanduser("~/.Xauthority")
    if os.path.isfile(home) and os.access(home, os.R_OK):
        return home
    return None


def resolve_gui_env() -> dict:
    """
    Copy process env and fill gaps GUI apps need on X11 cold start.

    On this host ~/.Xauthority does not exist; the session cookie lives at
    /run/user/$UID/xauth_*. Missing XAUTHORITY lets cpupower-gui stay alive
    (pid returned) without mapping a window — matching cold deck presses.
    """
    env = os.environ.copy()
    # Flatpak host spawn should talk to the real session bus when possible.
    if is_in_flatpak() and env.get("DBUS_SESSION_BUS_ADDRESS", "").startswith(
        "unix:path=/run/flatpak/bus"
    ):
        host_bus = f"unix:path=/run/user/{os.getuid()}/bus"
        if os.path.exists(f"/run/user/{os.getuid()}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = host_bus

    need_keys = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
    if any(not env.get(k) for k in need_keys):
        sysd = _systemd_user_environment()
        for k in need_keys:
            if not env.get(k) and sysd.get(k):
                env[k] = sysd[k]
                log.info(f"[first-press] RunCommand filled {k} from systemd --user env")

    if not env.get("XAUTHORITY"):
        xauth = _find_runtime_xauthority()
        if xauth:
            env["XAUTHORITY"] = xauth
            log.info(f"[first-press] RunCommand filled XAUTHORITY from runtime path {xauth!r}")

    if not env.get("XDG_RUNTIME_DIR"):
        runtime = f"/run/user/{os.getuid()}"
        if os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = runtime

    return env


def _xdg_application_dirs() -> list:
    dirs = []
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs.append(os.path.join(data_home, "applications"))
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for d in data_dirs.split(":"):
        if d:
            dirs.append(os.path.join(d, "applications"))
    return dirs


def find_desktop_entry_for_command(command: str):
    """
    Return (desktop_path, application_id, dbus_activatable) for a simple command.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None, None, False
    if len(parts) != 1:
        return None, None, False
    token = parts[0]
    base = os.path.basename(token)

    for app_dir in _xdg_application_dirs():
        if not os.path.isdir(app_dir):
            continue
        for path in glob.glob(os.path.join(app_dir, "*.desktop")):
            try:
                cp = ConfigParser(interpolation=None)
                cp.read(path, encoding="utf-8")
                if "Desktop Entry" not in cp:
                    continue
                section = cp["Desktop Entry"]
                if section.get("NoDisplay", "false").lower() == "true":
                    continue
                if section.get("Hidden", "false").lower() == "true":
                    continue
                exec_line = section.get("Exec", "")
                try:
                    exec_bin = shlex.split(exec_line.replace("%%", "%"))[0] if exec_line else ""
                except ValueError:
                    exec_bin = ""
                exec_base = os.path.basename(exec_bin) if exec_bin else ""
                name = os.path.splitext(os.path.basename(path))[0]
                icon = section.get("Icon", "")
                matches = (
                    exec_base == base
                    or base == name
                    or base == icon
                    or (f"/{base}" in exec_line)
                    or exec_line.strip().endswith(base)
                    or base in name
                )
                if not matches and section.get("Name", "").lower() == base.lower():
                    matches = True
                if not matches:
                    continue
                dbus_act = section.get("DBusActivatable", "false").lower() == "true"
                return path, name, dbus_act
            except Exception:
                continue
    return None, None, False


def rewrite_command_for_desktop_launch(command: str):
    """
    Rewrite a simple GUI command to a session-aware launcher when possible.
    Returns (command, method_tag).
    """
    path, app_id, dbus_act = find_desktop_entry_for_command(command)
    if not path:
        return command, "raw"
    if dbus_act and app_id and shutil.which("gapplication"):
        return f"gapplication launch {shlex.quote(app_id)}", f"gapplication:{app_id}"
    desk_id = os.path.splitext(os.path.basename(path))[0]
    if shutil.which("gtk-launch"):
        return f"gtk-launch {shlex.quote(desk_id)}", f"gtk-launch:{desk_id}"
    if shutil.which("gio"):
        return f"gio launch {shlex.quote(path)}", f"gio:{path}"
    return command, "raw"


def is_in_flatpak() -> bool:
    return os.path.isfile('/.flatpak-info')


@functools.cache
def get_user_shell() -> str:
    """
    The login shell of the user - in the flatpak this has to be asked from the host,
    the sandbox always reports /bin/sh.
    """
    if is_in_flatpak():
        try:
            passwd_entry = subprocess.run(["flatpak-spawn", "--host", "getent", "passwd", str(os.getuid())],
                                          capture_output=True, text=True, timeout=5).stdout.strip()
            shell = passwd_entry.split(":")[-1]
            if shell:
                return shell
        except Exception as e:
            log.error(e)
    else:
        shell = os.environ.get("SHELL")
        if shell:
            return shell

        try:
            return pwd.getpwuid(os.getuid()).pw_shell
        except Exception as e:
            log.error(e)

    return "/bin/bash"
