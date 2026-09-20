"""
Patch StreamDeck beta-resume HID reader so last_key_states always advances,
and add cold-start diagnostics for silent first physical presses.

Upstream `_read_with_resume_from_suspend` only updated last_key_states when a
key_callback was set. During open()/clear() that left sticky True bits, and the
first physical press was a silent no-op (old==new → no callback).

Sep 20 cold sessions still showed zero `hardware key_event` with no sticky
bits — HID IN reports never arrived until a GUI press forced a key-image USB
OUT. This patch also:
  - instruments None-vs-data reads (heartbeat while stalled)
  - drops LibUSBHIDAPI Library.mutex around hid_read/hid_write so image OUT
    floods cannot starve the IN reader (Device.mutex still serializes a handle)
"""

from __future__ import annotations

import time

from loguru import logger as log

_PATCHED = False
_LIBUSB_PATCHED = False


def apply_libusb_hidapi_io_mutex_patch() -> bool:
    """Stop holding the global Library.mutex during per-handle read/write."""
    global _LIBUSB_PATCHED
    if _LIBUSB_PATCHED:
        return True
    try:
        from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI
        from StreamDeck.Transport.Transport import TransportError
        import ctypes
    except Exception as e:
        log.warning(f"[first-press] could not import LibUSBHIDAPI for mutex patch: {e}")
        return False

    def write(self, handle, data):
        if not handle:
            raise TransportError("No HID device.")
        result = self.hidapi.hid_write(handle, bytes(data), len(data))
        if result < 0:
            raise TransportError(f"Failed to write out report ({result})")
        return result

    def read(self, handle, length):
        if not handle:
            raise TransportError("No HID device.")
        data = ctypes.create_string_buffer(length)
        result = self.hidapi.hid_read(handle, data, len(data))
        if result < 0:
            raise TransportError(f"Failed to read in report ({result})")
        if result == 0:
            return None
        return data.raw[:length]

    LibUSBHIDAPI.Library.write = write
    LibUSBHIDAPI.Library.read = read
    _LIBUSB_PATCHED = True
    log.info("[first-press] applied LibUSBHIDAPI read/write mutex patch")
    return True


def apply_streamdeck_resume_reader_patch() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    apply_libusb_hidapi_io_mutex_patch()
    try:
        from StreamDeck.Devices.StreamDeck import (
            ControlType,
            DialEventType,
            StreamDeck,
            TransportError,
        )
    except Exception as e:
        log.warning(f"[first-press] could not import StreamDeck for HID patch: {e}")
        return False

    def _read_with_resume_from_suspend(self) -> None:
        none_streak = 0
        data_reports = 0
        key_reports = 0
        last_heartbeat = time.monotonic()
        logged_first_data = False

        while self.run_read_thread:
            try:
                control_states = self._read_control_states()
                if control_states is None:
                    none_streak += 1
                    now = time.monotonic()
                    # Until the first real key report, emit a heartbeat so cold
                    # stalls are visible (all-None read path vs sticky state).
                    if key_reports == 0 and now - last_heartbeat >= 5.0:
                        log.info(
                            f"[first-press] HID read heartbeat deck={self.id()} "
                            f"none_streak={none_streak} data_reports={data_reports} "
                            f"key_reports={key_reports} "
                            f"last_key_states_true="
                            f"{[i for i, v in enumerate(self.last_key_states) if v]}"
                        )
                        last_heartbeat = now
                    time.sleep(1.0 / self.read_poll_hz)
                    continue

                none_streak = 0
                data_reports += 1
                if not logged_first_data:
                    logged_first_data = True
                    log.info(
                        f"[first-press] HID first non-None report deck={self.id()} "
                        f"controls={list(control_states.keys())}"
                    )

                if ControlType.KEY in control_states:
                    key_reports += 1
                    changes = [
                        (k, old, new)
                        for k, (old, new) in enumerate(
                            zip(self.last_key_states, control_states[ControlType.KEY])
                        )
                        if old != new
                    ]
                    # Concurrent image writes sometimes yield garbage multi-key
                    # flips; treat as state sync only (no app callbacks).
                    if len(changes) > 2:
                        for k, _old, new in changes:
                            self.last_key_states[k] = new
                        log.warning(
                            f"[first-press] ignored noisy HID key report "
                            f"n_changes={len(changes)} indices={[c[0] for c in changes]}"
                        )
                        continue
                    for k, _old, new in changes:
                        self.last_key_states[k] = new
                        if self.key_callback is not None:
                            self.key_callback(self, k, new)

                elif ControlType.DIAL in control_states:
                    if DialEventType.PUSH in control_states[ControlType.DIAL]:
                        for k, (old, new) in enumerate(
                            zip(
                                self.last_dial_states,
                                control_states[ControlType.DIAL][DialEventType.PUSH],
                            )
                        ):
                            if old == new:
                                continue
                            self.last_dial_states[k] = new
                            if self.dial_callback is not None:
                                self.dial_callback(self, k, DialEventType.PUSH, new)

                    if DialEventType.TURN in control_states[ControlType.DIAL]:
                        for k, amount in enumerate(
                            control_states[ControlType.DIAL][DialEventType.TURN]
                        ):
                            if amount == 0:
                                continue
                            if self.dial_callback is not None:
                                self.dial_callback(self, k, DialEventType.TURN, amount)

                elif ControlType.TOUCHSCREEN in control_states:
                    if self.touchscreen_callback is not None:
                        self.touchscreen_callback(
                            self, *control_states[ControlType.TOUCHSCREEN]
                        )

            except TransportError as error:
                self.run_read_thread = False
                self.close()

                if not self.reconnect_after_suspend:
                    return

                log.warning(
                    f"Read thread of deck {self.id()} hit a transport error, "
                    f"trying to reconnect: {error}"
                )
                self._reconnect()
                return

    StreamDeck._read_with_resume_from_suspend = _read_with_resume_from_suspend
    _PATCHED = True
    log.info("[first-press] applied StreamDeck resume-reader HID state patch")
    return True


def kick_hid_input(deck, reason: str = "") -> bool:
    """
    Nudge the USB HID path with a short OUT / feature write.

    Prefer DeckController._kick_hid_input which also force-repaints a key
    (same traffic as GUI simulate_press). This helper is the low-level half.
    """
    try:
        raw = getattr(deck, "deck", deck)
        if hasattr(raw, "_reset_key_stream"):
            raw._reset_key_stream()
        if hasattr(raw, "set_brightness"):
            pct = 75
            try:
                pct = int(getattr(raw, "_kick_brightness", pct))
            except Exception:
                pct = 75
            raw.set_brightness(pct)
        if hasattr(raw, "get_serial_number"):
            _ = raw.get_serial_number()
        log.info(f"[first-press] HID wake kick reason={reason}")
        return True
    except Exception as e:
        log.warning(f"[first-press] HID wake kick failed reason={reason}: {e}")
        return False
