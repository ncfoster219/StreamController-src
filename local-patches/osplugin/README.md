# OSPlugin cold-start RunCommand fix

Live plugins live under the Flatpak data dir (shared with native `--data`):

```bash
DATA="$HOME/.var/app/com.core447.StreamController/data/plugins/com_core447_OSPlugin"
cp local-patches/osplugin/RunCommand.py "$DATA/actions/RunCommand/RunCommand.py"
cp local-patches/osplugin/EasyCommand.py "$DATA/actions/EasyCommand/EasyCommand.py"
```

Already applied on this machine. Re-apply after OSPlugin store updates overwrite the files.

Change: replace `multiprocessing.Process(target=subprocess.Popen, ...)` with direct
`subprocess.Popen(..., start_new_session=True)`. Fork-from-multi-threaded SC was
dropping/delaying the first detached launch while ActionCore still logged `invoke`.
