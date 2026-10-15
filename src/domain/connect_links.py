import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

SUPPORTED_APPS: dict[str, str] = {
    "happ": "Happ",
    "v2raytun": "V2RayTun",
    "hiddify": "Hiddify",
}

MAX_PAGE_URL_LENGTH = 1024
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f]")


@dataclass(frozen=True)
class ConnectConfig:
    page_url: str  # https-адрес страницы без фрагмента
    apps: tuple[str, ...]  # порядок = порядок кнопок


def _normalize_page_url(raw: str) -> str | None:
    """https-адрес с доменом (в хосте есть точка), без учётных данных; фрагмент отбрасывается."""
    if len(raw) > MAX_PAGE_URL_LENGTH or _FORBIDDEN_CHARS.search(raw):
        return None
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        parts.port  # noqa: B018 - проверка корректности порта (ValueError)
    except ValueError:
        return None
    if parts.scheme != "https" or not hostname or "." not in hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def parse_connect_config(
    page_url: str | None, apps_csv: str | None
) -> tuple[ConnectConfig | None, list[str]]:
    """Валидирует конфигурацию кнопок подключения.

    Возвращает (конфиг | None, предупреждения). Предупреждения не содержат
    значений из окружения. None означает, что кнопки выключены.
    """
    warnings: list[str] = []

    raw_url = (page_url or "").strip()
    if not raw_url:
        warnings.append("CONNECT_PAGE_URL не задан: кнопки подключения выключены")
        return None, warnings

    normalized = _normalize_page_url(raw_url)
    if normalized is None:
        warnings.append(
            "CONNECT_PAGE_URL невалиден (нужен https-адрес, в хосте должна быть точка, "
            "без учётных данных и пробелов): кнопки подключения выключены"
        )
        return None, warnings

    apps: list[str] = []
    for item in (apps_csv or "").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name not in SUPPORTED_APPS:
            warnings.append(f"CONNECT_APPS: неизвестное приложение '{name}' пропущено")
            continue
        if name not in apps:
            apps.append(name)

    if not apps:
        warnings.append("CONNECT_APPS не содержит поддерживаемых приложений: кнопки подключения выключены")
        return None, warnings

    return ConnectConfig(page_url=normalized, apps=tuple(apps)), warnings


def build_connect_url(config: ConnectConfig, app: str, sub_url: str) -> str:
    """URL страницы-прослойки: данные во фрагменте (он не уходит на сервер)."""
    if app not in SUPPORTED_APPS:
        raise ValueError(f"Unsupported app: {app}")
    return f"{config.page_url}#app={app}&sub={quote(sub_url, safe='')}"
