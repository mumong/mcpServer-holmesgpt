"""
internet: fetch_webpage - fetch URL and convert HTML to Markdown.
Requires: requests, beautifulsoup4, markdownify.
"""
import os
from typing import Optional

from mcp.types import Tool

try:
    import requests
except ImportError:
    requests = None


def _run_fetch_webpage(arguments: dict) -> str:
    if requests is None:
        return "Error: install requests to use fetch_webpage."
    url = arguments.get("url")
    if not url:
        return "Error: parameter url is required."
    timeout = int(os.environ.get("INTERNET_TIMEOUT_SECONDS", "30"))
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; holmes-mcp) Gecko/20100101 Firefox/128.0"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        content = r.text
        ct = r.headers.get("content-type", "")
    except Exception as e:
        return "Error fetching {}: {}".format(url, e)
    if "text/html" in ct or (not ct and content.strip().lower().startswith("<!doctype")) or "<html" in content[:500].lower():
        try:
            from bs4 import BeautifulSoup
            from markdownify import markdownify
            soup = BeautifulSoup(content, "html.parser")
            for tag in ("script", "style", "nav", "header", "footer", "iframe"):
                for el in soup.find_all(tag):
                    el.decompose()
            content = markdownify(str(soup))
        except ImportError:
            pass
        except Exception as e:
            content = content + "\n\n(HTML-to-Markdown failed: {})".format(e)
    return content


TOOLS = [
    Tool(
        name="fetch_webpage",
        description="Fetch a webpage. Use to fetch runbooks or docs. Returns Markdown when possible.",
        inputSchema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The URL to fetch."}},
            "required": ["url"],
        },
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    if name != "fetch_webpage":
        return None
    return _run_fetch_webpage(arguments)
