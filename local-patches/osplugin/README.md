# OSPlugin local patches

Mirrored copies of cold-start fixes for the live plugin under:

`~/.var/app/com.core447.StreamController/data/plugins/com_core447_OSPlugin/`

## Apply

```bash
DATA="$HOME/.var/app/com.core447.StreamController/data/plugins/com_core447_OSPlugin"
cp local-patches/osplugin/RunCommand.py "$DATA/actions/RunCommand/RunCommand.py"
cp local-patches/osplugin/EasyCommand.py "$DATA/actions/EasyCommand/EasyCommand.py"
rm -f "$DATA/actions/RunCommand/__pycache__/"*.pyc
# then fully quit + relaunch StreamController
```

## RunCommand (v2c)

- No `multiprocessing.Process` (fork under GTK/HID was racy)
- `resolve_gui_env()` fills `XAUTHORITY` from `systemctl --user show-environment` or `/run/user/$UID/xauth_*` (this host has no `~/.Xauthority`)
- Single-token GUI commands rewritten to `gapplication launch` / `gtk-launch` / `gio launch` when a matching `.desktop` exists (`cpupower-gui` → `org.rnd2.cpupower_gui`)
- Detached launches: stderr tempfile + `poll()` at ~50ms / ~200ms with `[first-press]` logs

See store doc `docs/streamcontroller-first-press.md`.
