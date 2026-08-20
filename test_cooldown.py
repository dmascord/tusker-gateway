from tusker_gateway.cooldown import _cooldown_seconds_for_429

cases = [
    "try again in 1 week",
    "try again in 15 minutes",
    "retry after 2 hours",
    "wait 30 seconds",
    "weekly limit",
    "rate limit",
]
for body in cases:
    print(f"{body:25} -> {_cooldown_seconds_for_429({'body': body, 'headers': {}})}")
