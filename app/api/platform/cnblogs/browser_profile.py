from __future__ import annotations


BROWSER_MAJOR_VERSION = "152"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{BROWSER_MAJOR_VERSION}.0.0.0 Safari/537.36"
)
CLIENT_HINT_HEADERS = {
    "Sec-CH-UA": (
        f'"Chromium";v="{BROWSER_MAJOR_VERSION}", '
        '"Not?A_Brand";v="24", '
        f'"Google Chrome";v="{BROWSER_MAJOR_VERSION}"'
    ),
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}
