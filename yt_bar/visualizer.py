from .constants import BRAILLE_BASE, DOT_BITS, GRID_H, GRID_W


def grid_to_braille(grid):
    chars = []
    for char_col in range(0, GRID_W, 2):
        code = BRAILLE_BASE
        for local_col in range(2):
            x = char_col + local_col
            if x >= GRID_W:
                continue
            for y in range(GRID_H):
                if grid[x, y] > 0.18:
                    code |= DOT_BITS[local_col][y]
        chars.append(chr(code))
    return "".join(chars)
