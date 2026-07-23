"""Decode live-data PID payloads into named, unit-bearing sensor readings.

Parallel to :mod:`trecu.protocol.dtc`: where ``dtc.py`` turns DTC byte triples
into SAE J2012 codes + descriptions, this turns a PID's raw data bytes into a
physical value using the model-value table in ``trecu/data/triumph_pids.json``
(roadmap F2). It is the "sensor-decode layer" Phase 3 slots in below the service.

Each PID descriptor carries a **formula** — an expression over the data bytes
``A, B, C, D`` (A = first data byte, big-endian) exactly as SAE J1979 and the
common OBD-II PID references write it. Formulas are evaluated by a tiny
arithmetic interpreter restricted to ``+ - * /``, unary sign, parentheses,
numeric constants, and those four names — never Python ``eval`` of arbitrary
code, even though the table is bundled and trusted. A malformed or disallowed
formula raises :class:`FormulaError` at *load* time, so a bad table entry fails
loudly on startup rather than mid-poll.
"""

from __future__ import annotations

import ast
import json
import operator
from dataclasses import dataclass, field
from importlib import resources
from typing import Callable, Dict, Iterable, List, Optional

# Data bytes are exposed to a formula as A, B, C, D (big-endian order).
_BYTE_VARS = ("A", "B", "C", "D")

# The only arithmetic the formula language allows — nothing else evaluates.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

Env = Dict[str, int]


class FormulaError(ValueError):
    """A PID formula is malformed or uses a disallowed construct."""


def _ev(node: ast.AST, env: Env) -> float:
    if isinstance(node, ast.Expression):
        return _ev(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError(f"non-numeric constant: {node.value!r}")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise FormulaError(f"unknown name {node.id!r} (allowed: {', '.join(_BYTE_VARS)})")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_ev(node.left, env), _ev(node.right, env))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_ev(node.operand, env))
    raise FormulaError(f"disallowed expression: {type(node).__name__}")


def compile_formula(expr: str) -> Callable[[Env], float]:
    """Compile a byte-formula string into an ``env -> value`` callable.

    ``env`` maps byte names ``A``..``D`` to integers. Validation happens now (a
    trial evaluation over a zero environment), so a disallowed construct or
    unknown name raises :class:`FormulaError` here rather than during polling.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"cannot parse formula {expr!r}: {exc}") from exc
    _ev(tree, {v: 0 for v in _BYTE_VARS})  # validate node whitelist + names
    return lambda env: _ev(tree, env)


@dataclass(frozen=True)
class PidDef:
    """Descriptor for one live-data PID: identity, display, and decode formula."""

    pid: int
    name: str
    group: str
    unit: str
    num_bytes: int
    formula: str
    min: float
    max: float
    _fn: Callable[[Env], float] = field(compare=False, repr=False)
    # Byte position inside a packed multi-channel frame (kwp_local); unused by
    # the per-PID obd_mode01 path, where each PID's data arrives alone.
    frame_offset: int = 0

    @classmethod
    def from_entry(cls, pid: int, entry: dict) -> "PidDef":
        formula = entry["formula"]
        return cls(
            pid=pid,
            name=entry.get("name", f"PID 0x{pid:02X}"),
            group=entry.get("group", ""),
            unit=entry.get("unit", ""),
            num_bytes=int(entry.get("bytes", 1)),
            formula=formula,
            min=float(entry.get("min", 0.0)),
            max=float(entry.get("max", 0.0)),
            _fn=compile_formula(formula),
            frame_offset=int(entry.get("frame_offset", 0)),
        )

    def decode(self, data: bytes) -> float:
        """Evaluate the formula against ``data``'s first ``num_bytes`` bytes."""
        if len(data) < self.num_bytes:
            raise FormulaError(
                f"PID 0x{self.pid:02X} needs {self.num_bytes} data byte(s), "
                f"got {len(data)}"
            )
        env = {v: (data[i] if i < len(data) else 0) for i, v in enumerate(_BYTE_VARS)}
        return self._fn(env)


@dataclass(frozen=True)
class SensorReading:
    """One decoded live-data value, ready for display."""

    pid: int
    name: str
    value: float
    unit: str = ""
    group: str = ""
    min: float = 0.0
    max: float = 0.0
    raw: bytes = b""

    def formatted(self) -> str:
        """Compact numeric string: integers stay whole, else up to 2 decimals."""
        v = round(self.value, 2)
        return str(int(v)) if v == int(v) else f"{v:g}"


class KwpLocalTable:
    """Channel table for the packed Keihin live-data frame (``kwp_local``).

    Every Triumph Keihin sensor is read with a single
    ReadDataByLocalIdentifier request (``21 80``); the response is one frame
    with each channel at a fixed byte offset. This table maps channel index ->
    :class:`PidDef` (whose ``frame_offset``/``num_bytes`` locate it in the
    frame) and splits such a frame into :class:`SensorReading`\\ s. The bundled
    layout, divisors, and offsets are a **draft** pending a hardware capture of
    a real ``21 80`` response (roadmap F4) — fixing them is a data-only edit to
    ``triumph_pids.json``.
    """

    def __init__(self, lid: int, defs: Dict[int, PidDef]):
        self.lid = lid
        self._defs = defs

    @classmethod
    def from_dict(cls, section: dict) -> "KwpLocalTable":
        # Channel keys are *decimal* Keihin channel indices (0..98, gappy) —
        # unlike obd_mode01's hex PID keys.
        defs = {
            int(key): PidDef.from_entry(int(key), entry)
            for key, entry in section.get("channels", {}).items()
        }
        return cls(int(section.get("lid", "80"), 16), defs)

    def __len__(self) -> int:
        return len(self._defs)

    def __contains__(self, idx: int) -> bool:
        return idx in self._defs

    def channels(self) -> List[int]:
        return sorted(self._defs)

    def decode_frame(
        self, data: bytes, channels: Optional[Iterable[int]] = None
    ) -> List[SensorReading]:
        """Split one packed frame into readings for ``channels`` (None = all).

        A channel whose slot lies beyond the end of ``data`` — a real ECU may
        send a shorter frame than the draft layout — is dropped, mirroring
        ``read_live``'s "the ECU didn't answer it" convention.
        """
        out: List[SensorReading] = []
        for idx in self.channels() if channels is None else channels:
            d = self._defs.get(idx)
            if d is None:
                continue
            chunk = data[d.frame_offset : d.frame_offset + d.num_bytes]
            if len(chunk) < d.num_bytes:
                continue
            out.append(
                SensorReading(
                    pid=idx,
                    name=d.name,
                    value=d.decode(chunk),
                    unit=d.unit,
                    group=d.group,
                    min=d.min,
                    max=d.max,
                    raw=chunk,
                )
            )
        return out


class PidDatabase:
    """Lookup table mapping PID numbers to :class:`PidDef` decoders.

    Backed by ``trecu/data/triumph_pids.json``. The ``obd_mode01`` section holds
    the standardized SAE J1979 PIDs (the confirmed ISO 9141-2 / OBD path); the
    ``kwp_local`` section — exposed as the :attr:`kwp_local`
    :class:`KwpLocalTable` (``None`` when absent) — holds the Triumph Keihin
    packed-frame channels for the KWP path.
    """

    def __init__(self, defs: Optional[Dict[int, PidDef]] = None):
        self._defs: Dict[int, PidDef] = defs or {}
        self.kwp_local: Optional[KwpLocalTable] = None

    @classmethod
    def load_default(cls) -> "PidDatabase":
        with resources.files("trecu.data").joinpath("triumph_pids.json").open(
            "r", encoding="utf-8"
        ) as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def load_file(cls, path: str) -> "PidDatabase":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def from_dict(cls, data: dict) -> "PidDatabase":
        section = data.get("obd_mode01", data)
        defs: Dict[int, PidDef] = {}
        for key, entry in section.items():
            pid = int(key, 16)
            defs[pid] = PidDef.from_entry(pid, entry)
        db = cls(defs)
        kwp = data.get("kwp_local")
        if isinstance(kwp, dict) and kwp.get("channels"):
            db.kwp_local = KwpLocalTable.from_dict(kwp)
        return db

    def __len__(self) -> int:
        return len(self._defs)

    def __contains__(self, pid: int) -> bool:
        return pid in self._defs

    def get(self, pid: int) -> Optional[PidDef]:
        return self._defs.get(pid)

    def pids(self) -> List[int]:
        return sorted(self._defs)

    def decode(self, pid: int, data: bytes) -> SensorReading:
        d = self._defs.get(pid)
        if d is None:
            raise KeyError(f"unknown PID 0x{pid:02X}")
        value = d.decode(data)
        return SensorReading(
            pid=pid,
            name=d.name,
            value=value,
            unit=d.unit,
            group=d.group,
            min=d.min,
            max=d.max,
            raw=bytes(data[: d.num_bytes]),
        )

    def decode_all(self, raw: Iterable[tuple]) -> List[SensorReading]:
        """Decode ``(pid, data_bytes)`` pairs, skipping PIDs not in the table."""
        out: List[SensorReading] = []
        for pid, data in raw:
            if pid in self._defs:
                out.append(self.decode(pid, data))
        return out
