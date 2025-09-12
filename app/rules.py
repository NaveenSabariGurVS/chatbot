def apply_rules(message: str) -> str | None:
    if not message:
        return None
    message = message.strip().lower()
    rules = {
        "hi": "how may I help you ?"
    }
    return rules.get(message)
