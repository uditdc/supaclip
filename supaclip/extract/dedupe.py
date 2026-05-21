from __future__ import annotations


Range = tuple[float, float]


def iou(a: Range, b: Range) -> float:
    a0, a1 = a
    b0, b1 = b
    if a1 <= a0 or b1 <= b0:
        return 0.0
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    if union <= 0:
        return 0.0
    return inter / union


def merge_overlapping(ranges: list[Range], threshold: float) -> list[Range]:
    """Iteratively merge any pair with IoU >= threshold into their union, until stable."""
    current = sorted(ranges, key=lambda r: r[0])
    while True:
        merged_any = False
        result: list[Range] = []
        consumed = [False] * len(current)
        for i, r in enumerate(current):
            if consumed[i]:
                continue
            cur = r
            for j in range(i + 1, len(current)):
                if consumed[j]:
                    continue
                if iou(cur, current[j]) >= threshold:
                    cur = (min(cur[0], current[j][0]), max(cur[1], current[j][1]))
                    consumed[j] = True
                    merged_any = True
            result.append(cur)
        current = sorted(result, key=lambda r: r[0])
        if not merged_any:
            return current
