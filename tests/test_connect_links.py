from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

from src.domain.connect_links import (
    SUPPORTED_APPS,
    ConnectConfig,
    build_connect_url,
    parse_connect_config,
)

PAGE = "https://connect.example.com/"
APPS = "happ,v2raytun,hiddify"


def _config(page: str = PAGE, apps: str = APPS) -> ConnectConfig:
    config, _ = parse_connect_config(page, apps)
    assert config is not None
    return config


# ---------- построение URL страницы ----------

def test_url_puts_data_into_fragment_not_query():
    url = build_connect_url(_config(), "happ", "https://sub.example.com/sub/TOKEN")

    parts = urlsplit(url)
    assert parts.query == ""  # токен не должен попасть в query (и в логи хостинга)
    assert parts.scheme == "https" and parts.netloc == "connect.example.com"
    fragment = parse_qs(parts.fragment)
    assert fragment["app"] == ["happ"]
    assert fragment["sub"] == ["https://sub.example.com/sub/TOKEN"]


@pytest.mark.parametrize(
    "sub_url",
    [
        "https://sub.example.com:8443/sub/TOKEN?a=1&b=2",
        "https://sub.example.com/a b/ж",
        "https://sub.example.com/x#frag",
        "https://sub.example.com/a+b%20c",
        "https://sub.example.com/<script>\"'",
    ],
)
def test_subscription_url_is_fully_percent_encoded(sub_url):
    url = build_connect_url(_config(), "hiddify", sub_url)

    sub_part = url.split("&sub=", 1)[1]
    assert sub_part == quote(sub_url, safe="")
    assert not any(ch in sub_part for ch in "/?#&+ <>\"'")  # ничего, что ломает фрагмент
    assert unquote(sub_part) == sub_url  # раскодируется ровно в исходное значение
    assert url.count("#") == 1


def test_apps_follow_configured_order_without_duplicates():
    config = _config(apps="hiddify, Happ ,hiddify,v2raytun")

    assert config.apps == ("hiddify", "happ", "v2raytun")


def test_unknown_apps_are_skipped_with_warning():
    config, warnings = parse_connect_config(PAGE, "happ,evil,hiddify")

    assert config is not None and config.apps == ("happ", "hiddify")
    assert any("evil" in w for w in warnings)


def test_no_supported_apps_disables_buttons():
    config, warnings = parse_connect_config(PAGE, "evil, ,")

    assert config is None
    assert warnings


def test_build_url_rejects_unsupported_app():
    with pytest.raises(ValueError):
        build_connect_url(_config(), "evil", "https://sub.example.com/x")


def test_all_supported_apps_can_be_built():
    config = _config()
    for app in SUPPORTED_APPS:
        assert f"app={app}&sub=" in build_connect_url(config, app, "https://sub.example.com/x")


# ---------- валидация адреса страницы ----------

@pytest.mark.parametrize(
    "page_url",
    [
        "http://connect.example.com/",  # не https
        "ftp://connect.example.com/",
        "connect.example.com",  # без схемы
        "https://localhost/",  # хост без точки
        "https://connect/",
        "https://",  # нет хоста
        "https://user:pass@connect.example.com/",  # учётные данные
        "https://conn ect.example.com/",  # пробел
        "https://connect.example.com/a b",
        "https://connect.example.com/a\nb",
        "https://connect.example.com:notaport/",
        "https://connect.example.com/" + "a" * 1100,  # слишком длинный
        "",
        "   ",
        None,
    ],
)
def test_invalid_page_url_disables_buttons_with_warning(page_url):
    config, warnings = parse_connect_config(page_url, APPS)

    assert config is None
    assert warnings  # причина для лога
    assert all(str(page_url).strip() not in w for w in warnings if page_url and str(page_url).strip())


@pytest.mark.parametrize(
    "page_url,expected",
    [
        ("https://connect.example.com", "https://connect.example.com"),
        ("https://connect.example.com/", "https://connect.example.com/"),
        ("  https://connect.example.com/connect/  ", "https://connect.example.com/connect/"),
        ("https://connect.example.com:8443/c/", "https://connect.example.com:8443/c/"),
        ("https://connect.example.com/c/#old=1", "https://connect.example.com/c/"),  # фрагмент отбрасывается
        ("HTTPS://Connect.Example.com/c/", "https://Connect.Example.com/c/"),
    ],
)
def test_valid_page_url_is_normalized(page_url, expected):
    config, warnings = parse_connect_config(page_url, APPS)

    assert config is not None
    assert config.page_url == expected
    assert warnings == []


def test_page_url_with_existing_query_keeps_fragment_separate():
    config = _config(page="https://connect.example.com/c/?v=2")

    url = build_connect_url(config, "happ", "https://sub.example.com/x")

    assert url.startswith("https://connect.example.com/c/?v=2#app=happ&sub=")
    assert url.count("#") == 1


def test_default_apps_string_is_all_apps():
    config, _ = parse_connect_config(PAGE, "happ,v2raytun,hiddify")

    assert config.apps == ("happ", "v2raytun", "hiddify")
