LEVELS = ("L0", "L1", "L2", "L3")


def rank(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError:
        raise ValueError(f"unknown level {level!r}, expected one of {LEVELS}") from None


def max_level(*levels: str) -> str:
    return max(levels, key=rank) if levels else LEVELS[0]


def allows(clearance: str, level: str) -> bool:
    return rank(clearance) >= rank(level)
