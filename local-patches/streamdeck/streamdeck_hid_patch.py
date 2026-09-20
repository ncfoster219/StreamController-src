"""
Focused StreamDeck patches for cold first-press.

1. Resume reader: always advance last_key_states (match `_read()`), even when
   key_callback is still None during open().
2. LibUSBHIDAPI: do not hold Library.mutex around hid_read/hid_write — Device
   mutex already serializes a handle. Global lock during image OUT floods can
   starve the IN reader; multi-prime without this made cold press *worse*
   (~0% vs ~50% with a single prime).
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
        while self.run_read_thread:
            try:
                control_states = self._read_control_states()
                if control_states is None:
                    time.sleep(1.0 / self.read_poll_hz)
                    continue

                if ControlType.KEY in control_states:
                    for k, (old, new) in enumerate(
                        zip(self.last_key_states, control_states[ControlType.KEY])
                    ):
                        if old == new:
                            continue
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
    log.info("[first-press] applied StreamDeck resume-reader state patch")
    return True
