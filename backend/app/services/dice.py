"""Dice expression parser and roller.

Supports: "d20", "2d6+3", "4d6-1", "3d8+2d4+5", and "adv"/"dis"
(shorthand for advantage/disadvantage on a d20).
"""

import random
import re

_TERM = re.compile(r"([+-]?)\s*(?:(\d*)d(\d+)|(\d+))", re.IGNORECASE)

MAX_DICE = 100
MAX_SIDES = 1000


class DiceError(ValueError):
    pass


def roll(expression: str) -> dict:
    expr = expression.strip().lower()
    if expr in ("adv", "advantage"):
        a, b = random.randint(1, 20), random.randint(1, 20)
        return {
            "expression": "adv (2d20 keep highest)",
            "rolls": [a, b],
            "total": max(a, b),
        }
    if expr in ("dis", "disadvantage"):
        a, b = random.randint(1, 20), random.randint(1, 20)
        return {
            "expression": "dis (2d20 keep lowest)",
            "rolls": [a, b],
            "total": min(a, b),
        }

    pos = 0
    rolls: list[int] = []
    total = 0
    matched_any = False
    for match in _TERM.finditer(expr):
        if expr[pos : match.start()].strip():
            raise DiceError(f"Can't parse dice expression: {expression!r}")
        if matched_any and not match.group(1):
            raise DiceError(f"Missing +/- between terms in {expression!r}")
        pos = match.end()
        matched_any = True
        sign = -1 if match.group(1) == "-" else 1
        if match.group(3):  # NdM term
            count = int(match.group(2) or 1)
            sides = int(match.group(3))
            if not (1 <= count <= MAX_DICE) or not (2 <= sides <= MAX_SIDES):
                raise DiceError(f"Dice out of range in {expression!r}")
            for _ in range(count):
                value = random.randint(1, sides)
                rolls.append(sign * value)
                total += sign * value
        else:  # flat modifier
            total += sign * int(match.group(4))
    if not matched_any or expr[pos:].strip():
        raise DiceError(f"Can't parse dice expression: {expression!r}")
    return {"expression": expression.strip(), "rolls": rolls, "total": total}
