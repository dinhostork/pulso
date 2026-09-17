import pytest

from news.domain.html_text import html_to_text


def test_blocks_inline_whitespace_entities_and_comments():
    html = """
    <article><h1> A&nbsp;title </h1><p>First   &amp; <strong>second</strong>.</p>
    <!-- secret --><div>Next<br>line</div><ul><li>One</li><li>Two</li></ul></article>
    """
    assert html_to_text(html) == "A title\nFirst & second.\nNext\nline\nOne\nTwo"


@pytest.mark.parametrize("tag", ["script", "style", "template", "iframe", "noscript", "svg"])
def test_suppressed_subtrees_drop_descendant_and_fallback_text(tag):
    html = f"<p>Before</p><{tag}><div>secret <b>nested</b></div></{tag}><p>After</p>"
    assert html_to_text(html) == "Before\nAfter"


def test_malformed_html_is_best_effort_and_deterministic():
    assert html_to_text("<div>Broken <b>markup<p>Next &amp;") == "Broken markup\nNext &"
    assert html_to_text("<iframe><p>fallback</iframe><p>Visible</p>") == "Visible"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", ""), ("  plain\t text  ", "plain text"), ("&#39;x&#39;", "'x'")],
)
def test_empty_plain_text_and_character_references(value, expected):
    assert html_to_text(value) == expected
