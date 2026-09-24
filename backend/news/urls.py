"""Private News product read routes (no trailing slash)."""

from django.urls import path

from news import views

app_name = "news"
urlpatterns = [
    path(
        "stories/<str:story_id>/sources",
        views.StorySourcesView.as_view(),
        name="story-sources",
    ),
]
