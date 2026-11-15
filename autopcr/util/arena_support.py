"""Pure helpers for counterattack queries; no simulator or account state."""

import heapq
from itertools import product
from threading import Event
import time


class DefenseTeams(list):
    def __init__(self, teams=(), slots=()):
        super().__init__(teams)
        self.slots = tuple(slots)


def valid_team(team, available):
    return len(team) == 5 and len(set(team)) == 5 and all(
        type(uid) is int and uid > 0 and uid in available for uid in team
    )


def top_combinations(groups, key, limit=9, stop=None, transform=None):
    """Stable bounded selection. key must include any legacy secondary sort."""
    stop = stop or Event()

    def candidates():
        for index, group in enumerate(product(*groups)):
            if index % 4096 == 0:
                time.sleep(.001)  # A timed GIL handoff also wakes Windows IOCP loops.
            if stop.is_set():
                return
            ids = [uid for team in group for uid in team]
            if len(set(ids)) != len(ids):
                continue
            yield index, group

    # Generic callers use their own record objects instead of raw unit lists.
    def ranked():
        for index, group in enumerate(product(*groups)):
            if index % 4096 == 0:
                time.sleep(.001)
            if stop.is_set():
                return
            value = transform(group) if transform else group
            if value is not None:
                yield key(value), index, value

    values = ranked() if transform else ((key(g), i, g) for i, g in candidates())
    if limit is None:
        return [list(value) for _, _, value in sorted(values, key=lambda x: x[:2])]
    return [list(value) for _, _, value in heapq.nsmallest(
        max(0, limit), values, key=lambda x: x[:2]
    )]
