"""Pure-ASCII waveform renderer for protocol transactions (no RTL dependency)."""

from __future__ import annotations

from typing import Iterable, Sequence

LOW = "_"
HIGH = "\u203e"


def render(records, *, t0: int | None = None,
           t1: int | None = None, cols: int = 72, label: str = "",
           rows: Sequence[tuple[str, Sequence[tuple[int, int]]]] = (),
           names: Sequence[str] | None = None) -> str:
    """Render one waveform row per signal from [(cycle, level)] transition lists.

    A transition column shows ``/`` for a rising edge and ``\\`` for a falling
    edge, so the exact edge position is preserved in the ASCII art.
    """
    lines: list[str] = []
    if label:
        lines.append(label)
    single = bool(records) and isinstance(records[0], (tuple, list)) and \
        len(records[0]) == 2 and isinstance(records[0][0], int)
    signals = [records] if single else list(records)
    all_cycles = [c for tr in signals for c, _ in tr]
    for _, tr in rows:
        all_cycles += [c for c, _ in tr]
    if not all_cycles:
        lines.append("  (no transitions)")
        return "\n".join(lines)
    if t0 is None:
        t0 = min(all_cycles)
    if t1 is None:
        t1 = max(all_cycles)
    if t1 <= t0:
        t1 = t0 + 1
    span = t1 - t0
    cols = max(8, int(cols))

    def col(c: int) -> int:
        return min(max(int(round((c - t0) * (cols - 1) / span)), 0), cols - 1)

    def row(trans: Sequence[tuple[int, int]], name: str) -> str:
        trans = list(trans)
        chars = [LOW] * cols
        if trans:
            cur = None
            for c in range(cols):
                val = trans[0][1]
                for cyc, v in trans:
                    if col(cyc) <= c:
                        val = v
                    else:
                        break
                chars[c] = HIGH if val else LOW
            prev = trans[0][1]
            for cyc, v in trans:
                cc = col(cyc)
                if cc > 0 and v != prev:
                    chars[cc] = "/" if v else "\\"
                prev = v
        return f"{name:<10} {''.join(chars)}"

    if names is None:
        names = ["line"]
    for i, tr in enumerate(signals):
        name = names[i] if i < len(names) else f"sig{i}"
        lines.append(row(tr, name))
    for name, tr in rows:
        lines.append(row(tr, name))
    axis = [" "] * cols
    for k in range(5):
        cc = int(round(k * (cols - 1) / 4))
        txt = str(t0 + int(round(span * cc / max(cols - 1, 1))))
        for j, ch in enumerate(txt):
            if cc + j < cols:
                axis[cc + j] = ch
    lines.append(f"{'cycle':<10} {''.join(axis)}")
    return "\n".join(lines)


def transitions(levels: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Normalise a (cycle, level) list to one entry per actual change."""
    out: list[tuple[int, int]] = []
    for cyc, v in levels:
        v = 1 if v else 0
        if not out or out[-1][1] != v:
            out.append((int(cyc), v))
    return out
