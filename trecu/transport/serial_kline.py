"""FT232RL / KKL cable K-line transport built on pyserial.

A KKL cable (FT232RL + a K-line driver transistor) enumerates through the host's
FTDI serial (VCP) driver as an ordinary serial port (``/dev/ttyUSB0``,
``/dev/cu.usbserial-XXXX``, ``COMx``).  We drive it at the ISO 9141-2 line rate
(10400 baud, 8N1) and bit-bang the 5-baud init by toggling the UART break
condition, which pulls the K-line.
"""

from __future__ import annotations

import time
from typing import List

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - dependency guard
    raise ImportError(
        "pyserial is required for the serial K-line transport. "
        "Install it with `pip install pyserial`."
    ) from exc

from .base import Transport, TransportError


def list_serial_ports() -> List[dict]:
    """Return metadata for the serial ports currently present on the system.

    FTDI-based KKL cables usually show a ``vid:pid`` of ``0403:6001`` and a
    device path like ``/dev/ttyUSB0``, ``/dev/cu.usbserial-...``, or ``COMx``.
    """
    ports = []
    for p in list_ports.comports():
        ports.append(
            {
                "device": p.device,
                "description": p.description or "",
                "manufacturer": p.manufacturer or "",
                "vid": p.vid,
                "pid": p.pid,
                "serial_number": p.serial_number or "",
                "likely_kkl": (p.vid == 0x0403),  # FTDI vendor id
            }
        )
    return ports


class KLineSerialTransport(Transport):
    """Talk to the ECU over a serial-attached KKL cable."""

    echoes = True  # single-wire K-line reflects our TX into our RX
    supports_slow_init = True

    def __init__(self, port: str, baudrate: int = 10400, read_timeout: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self._ser: "serial.Serial | None" = None

    def open(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.read_timeout,
                write_timeout=2.0,
            )
        except serial.SerialException as exc:
            raise TransportError(f"could not open {self.port}: {exc}") from exc
        # Some FTDI/K-line drivers leave the break asserted; make sure it is off.
        try:
            self._ser.break_condition = False
        except (OSError, ValueError):
            pass
        self.reset_input()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def _dev(self) -> "serial.Serial":
        if self._ser is None:
            raise TransportError("transport is not open")
        return self._ser

    def reset_input(self) -> None:
        try:
            self._dev.reset_input_buffer()
        except (OSError, serial.SerialException):
            pass

    def write(self, data: bytes) -> None:
        try:
            self._dev.write(data)
            self._dev.flush()
        except serial.SerialException as exc:
            raise TransportError(f"write failed: {exc}") from exc

    def read(self, count: int, timeout: float) -> bytes:
        dev = self._dev
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Honour the caller's overall timeout regardless of the port default.
            dev.timeout = min(self.read_timeout, remaining)
            chunk = dev.read(count - len(buf))
            if chunk:
                buf.extend(chunk)
            elif time.monotonic() >= deadline:
                break
        return bytes(buf)

    def five_baud_init(self, address: int) -> None:
        """ISO 9141-2 5-baud slow init.

        Bit-bangs the address byte at 5 baud using the break condition, with
        the waveform other K-line tools drive on an FTDI cable: a 100 ms
        pre-init settle, then **one full bit period of guaranteed idle-high**
        before the start bit (so the ECU sees a clean falling edge even if the
        line was just driven), then start bit + 8 data bits LSB-first + stop
        bit — 11 bit-periods, ~2.2 s total. Each bit edge is scheduled against
        an **absolute** deadline derived from a single start time, so the
        per-bit ``break_condition`` ioctl latency and ``sleep`` overshoot
        cannot *accumulate* across the frame — a naive ``sleep(bit_time)`` per
        bit drifts enough over the frame that the ECU samples the wrong
        address and the handshake fails intermittently. The ioctl is also
        only touched when the line level actually *changes* (matching other
        K-line implementations), keeping its latency out of the mid-frame
        timing on runs of equal bits.
        """
        dev = self._dev
        bit_time = 1.0 / 5.0  # 200 ms per bit
        self.reset_input()
        time.sleep(0.1)  # pre-init settle (100 ms before the init, as other K-line tools do)
        # Frame: 1 idle-high period, 1 start bit (0), 8 data bits LSB first,
        # 1 stop bit (1) — the (address*4)+1025 bit pattern.
        bits = [1, 0] + [(address >> i) & 1 for i in range(8)] + [1]
        try:
            start = time.monotonic()
            level: int | None = None
            for i, bit in enumerate(bits):
                if bit != level:
                    # break asserted == line low == logical 0
                    dev.break_condition = (bit == 0)
                    level = bit
                # Hold this bit until its slot ends on the absolute schedule.
                remaining = (start + (i + 1) * bit_time) - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
            dev.break_condition = False
        except (OSError, ValueError) as exc:
            raise TransportError(f"5-baud init failed: {exc}") from exc
        self.reset_input()
