"""Group EasyOCR word boxes into text lines in reading order.

EasyOCR returns one box per word group, often splitting a line ("39", "Hàng", "Cót") and in no
reliable order. Boxes whose vertical spans overlap enough and that sit close horizontally are
joined left to right; lines are then ordered top to bottom.
"""

from dataclasses import dataclass
from typing import Any

MIN_V_OVERLAP = 0.5  # of the shorter box's height
MAX_H_GAP = 1.5  # in line heights; a wider gap starts another line (e.g. a separate caption)


@dataclass
class _Box:
    text: str
    confidence: float
    top: float
    bottom: float
    left: float
    right: float

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


def _box(points: Any, text: str, confidence: float) -> _Box:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return _Box(text, confidence, min(ys), max(ys), min(xs), max(xs))


def _same_row(a: _Box, b: _Box) -> bool:
    overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
    return overlap >= MIN_V_OVERLAP * min(a.height, b.height)


def group_lines(detections: list[tuple[Any, str, float]]) -> list[dict[str, Any]]:
    """`detections` as EasyOCR's readtext returns them: (points, text, confidence)."""
    boxes = sorted(
        (_box(p, t.strip(), float(c)) for p, t, c in detections if t.strip()),
        key=lambda b: (b.top + b.bottom) / 2,
    )
    rows: list[list[_Box]] = []
    for box in boxes:
        for row in rows:
            # compare with the row's first box only: chaining through any member would let
            # slanted or stacked text glue several lines together
            if _same_row(box, row[0]):
                row.append(box)
                break
        else:
            rows.append([box])

    lines: list[list[_Box]] = []
    for row in rows:
        row.sort(key=lambda b: b.left)
        current = [row[0]]
        for box in row[1:]:
            prev = current[-1]
            if box.left - prev.right > MAX_H_GAP * max(prev.height, box.height):
                lines.append(current)
                current = [box]
            else:
                current.append(box)
        lines.append(current)

    lines.sort(key=lambda line: (min(b.top for b in line), min(b.left for b in line)))
    return [
        {
            "text": " ".join(b.text for b in line),
            # weighted by text length so a long confident word outweighs a stray mark
            "confidence": sum(b.confidence * len(b.text) for b in line)
            / sum(len(b.text) for b in line),
        }
        for line in lines
    ]
