"""Deterministic HTML-to-text conversion for publication material.

Suppressed elements (script, style, template, iframe, noscript and svg) lose
their complete subtrees. Common block elements and ``br`` create newline
boundaries. Comments are ignored, entities are decoded once by ``HTMLParser``,
and remaining whitespace is collapsed per logical line. Broken markup is
handled on a best-effort basis without executing or fetching anything.
"""

from html.parser import HTMLParser

_SUPPRESSED = frozenset({"script", "style", "template", "iframe", "noscript", "svg"})
_BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "dt",
        "dd",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_tags: list[str] = []

    def _boundary(self) -> None:
        if not self.suppressed_tags:
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if self.suppressed_tags or tag in _SUPPRESSED:
            self.suppressed_tags.append(tag)
        elif tag == "br" or tag in _BLOCKS:
            self._boundary()

    def handle_startendtag(self, tag: str, attrs) -> None:
        del attrs
        if not self.suppressed_tags and (tag == "br" or tag in _BLOCKS):
            self._boundary()

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed_tags:
            if tag in self.suppressed_tags:
                index = len(self.suppressed_tags) - 1 - self.suppressed_tags[::-1].index(tag)
                del self.suppressed_tags[index:]
        elif tag in _BLOCKS:
            self._boundary()

    def handle_data(self, data: str) -> None:
        if not self.suppressed_tags:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Return readable, normalized plain text from an HTML string."""

    if not isinstance(html, str) or not html:
        return ""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)
