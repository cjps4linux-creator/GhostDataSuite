from urllib.parse import urljoin
import re

MEDIA_EXT_MAP = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"},
    "video": {".mp4", ".webm", ".ogv", ".mov", ".mkv", ".avi"},
    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".csv"},
}
MEDIA_EXT_GUESS = {ext for exts in MEDIA_EXT_MAP.values() for ext in exts}


def _extract_media_urls(html: str, base_url: str) -> list[str]:
    url_re = re.compile(r'https?://[^\s"<>]+|/[^\s"<>]+')
    seen: set[str] = set()
    out: list[str] = []
    for raw in url_re.findall(html):
        candidate = urljoin(base_url, raw)
        parsed_path = candidate.split("?", 1)[0].split("#", 1)[0].lower()
        last = parsed_path.split("/")[-1]
        if not last or "." not in last:
            continue
        ext = "." + last.split(".")[-1]
        if ext not in MEDIA_EXT_GUESS:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


html = """
<a href="/tag/misattributed-eleanor-roosevelt/page/1/">x</a>
<a href="https://httpbin.org/image/png">img</a>
<img src="https://picsum.photos/200/300.jpg" />
<a href="https://example.com/manual.pdf">pdf</a>
"""
urls = _extract_media_urls(html, "https://quotes.toscrape.com")
print("count", len(urls))
for u in urls:
    print(" ", u)
