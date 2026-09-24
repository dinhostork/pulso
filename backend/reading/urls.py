"""Private Reading endpoint routes."""

from django.urls import path

from reading import views

app_name = "reading"
urlpatterns = [
    path("feed", views.FeedView.as_view(), name="feed"),
    path("stories/<str:story_id>", views.StoryDetailView.as_view(), name="story-detail"),
    path("bookmarks", views.BookmarkListView.as_view(), name="bookmark-list"),
    path(
        "bookmarks/<int:story_id>",
        views.BookmarkMutationView.as_view(),
        name="bookmark-mutation",
    ),
    path(
        "feed-impressions",
        views.FeedImpressionBatchView.as_view(),
        name="feed-impressions",
    ),
]
