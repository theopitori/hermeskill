# A deliberately-broken stub. The demo "agent" pretends to fix the TODO.
def get_user(token: str | None):
    # TODO: handle None — currently raises AttributeError
    return token.split(".")[0]
