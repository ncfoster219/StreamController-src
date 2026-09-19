"""
Patch StreamDeck beta-resume HID reader so last_key_states always advances.

Upstream `_read_with_resume_from_suspend` only updated last_key_states when a
key_callback was set. During open()/clear() that left sticky True bits, and the
first physical press was a silent no-op (old==new → no callback). Matches cold
sc-native sessions with zero `hardware key_event` lines while EmulateInput worked.
"""

from __future__ import annotations

import time

from loguru import logger as log

_PATCHED = False


def apply_streamdeck_resume_reader_patch() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
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

    # Idempotent: skip if already aligned with _read() (callback gated only on fire)
    # Always replace with known-good implementation matching _read()'s state policy.

    def _read_with_resume_from_suspend(self) -> None:
        while self.run_read_thread:
            try:
                control_states = self._read_control_states()
                if control_states is None:
                    time.sleep(1.0 / self.read_poll_hz)
                    continue

                if ControlType.KEY in control_states:
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
