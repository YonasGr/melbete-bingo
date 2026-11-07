"""Winner share for a room: the room's own ratio when its switch is on, else the shared payout_ratio."""

MIN_RATIO, MAX_RATIO = 0.5, 1.0


def effective_payout_ratio(settings, track=None) -> float:
    if track:
        custom = getattr(settings, f"payout_custom_{track}", False)
        own = getattr(settings, f"payout_ratio_{track}", None)
        if custom and own is not None and MIN_RATIO <= float(own) <= MAX_RATIO:
            return float(own)
    return settings.payout_ratio
