"""Line-aware overlap chunker.

Produces chunks of roughly `target_chars` characters, with `overlap_chars`
overlap between successive chunks. Never splits mid-line; the boundary
always falls between two newline-terminated lines, so retrieved chunks
are readable as-is.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    text: str
    start_line: int  # 1-based
    end_line: int    # 1-based, inclusive


def chunk_text(text: str, target_chars: int = 1500, overlap_chars: int = 200) -> list[Chunk]:
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be in [0, target_chars)")

    if not text:
        return []

    # Preserve trailing newlines per line so reconstruction round-trips.
    lines = text.splitlines(keepends=True)
    if not lines:
        return []
    # Cumulative character counts: cum[i] = chars in lines[0:i].
    cum = [0]
    for ln in lines:
        cum.append(cum[-1] + len(ln))

    out: list[Chunk] = []
    n = len(lines)
    i = 0  # line cursor (0-based)
    while i < n:
        start_chars = cum[i]
        # Pick the last line index j such that cum[j+1] - start_chars <= target_chars,
        # but always include at least one line so we make forward progress.
        j = i
        while j + 1 < n and cum[j + 2] - start_chars <= target_chars:
            j += 1
        chunk_lines = lines[i : j + 1]
        chunk_text_str = "".join(chunk_lines)
        # Strip a single trailing newline so the chunk text reads cleanly,
        # but only if it exists (preserves chunks that don't end on a newline).
        if chunk_text_str.endswith("\n"):
            chunk_text_str = chunk_text_str[:-1]
        out.append(Chunk(text=chunk_text_str, start_line=i + 1, end_line=j + 1))

        if j + 1 >= n:
            break
        # Slide the cursor forward by approximately (target - overlap) chars
        # but stop at a line boundary >= i + 1 (must make progress).
        target_advance = max(1, target_chars - overlap_chars)
        next_i = i + 1
        advanced = cum[next_i] - cum[i]
        while next_i < j + 1 and advanced < target_advance:
            next_i += 1
            advanced = cum[next_i] - cum[i]
        if next_i <= i:
            next_i = i + 1
        i = next_i

    return out
