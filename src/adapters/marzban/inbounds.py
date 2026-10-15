from typing import Any


def build_inbounds_payload(inbounds_response: dict[str, Any]) -> dict[str, list[str]]:
    """Собирает словарь `inbounds` для создания пользователя из ответа GET /api/inbounds."""
    inbounds: dict[str, list[str]] = {}
    for protocol, items in inbounds_response.items():
        if isinstance(items, list):
            tags = [item["tag"] for item in items if isinstance(item, dict) and "tag" in item]
            if tags:
                inbounds[protocol] = tags
    return inbounds
