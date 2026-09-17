import pytest
from django.utils import timezone

from news.application.process import ProcessState, process_raw_article
from news.models import RawArticle, Source, SourceEndpoint


@pytest.mark.django_db
def test_process_stub_leaves_pending(monkeypatch):
    monkeypatch.setattr("news.adapters.targets._resolve", lambda *_: ("8.8.8.8",))
    source = Source.objects.create(slug="p", name="P")
    endpoint = SourceEndpoint.objects.create(
        source=source, kind="RSS", url="https://feed.example/rss"
    )
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind="EXTERNAL_ID",
        external_key="x",
        external_id="x",
        url="",
        payload={},
        payload_hash="a" * 64,
        fetched_at=timezone.now(),
    )
    assert process_raw_article(raw.pk).state == ProcessState.PENDING
    raw.refresh_from_db()
    assert raw.status == "PENDING" and raw.article_id is None
    RawArticle.objects.filter(pk=raw.pk).update(status="REJECTED")
    assert process_raw_article(raw.pk).state == ProcessState.SKIPPED
