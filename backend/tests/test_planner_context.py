import uuid

from backend.models import Video
from backend.services.planner_context import VideoContext, build_video_contexts


def _video(*, name: str | None, original_filename: str) -> Video:
    return Video(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        storage_uri="local:///videos/x.mp4",
        original_filename=original_filename,
        name=name,
    )


def test_build_video_contexts_assigns_stable_ordered_handles() -> None:
    videos = [
        _video(name="Intro", original_filename="a.mp4"),
        _video(name=None, original_filename="b.mp4"),
    ]

    contexts = build_video_contexts(videos)

    assert contexts == [
        VideoContext(
            handle="video_1",
            video_id=str(videos[0].id),
            display_name="Intro",
            uri="local:///videos/x.mp4",
        ),
        VideoContext(
            handle="video_2",
            video_id=str(videos[1].id),
            display_name="b.mp4",
            uri="local:///videos/x.mp4",
        ),
    ]


def test_build_video_contexts_falls_back_to_original_filename_when_unnamed() -> None:
    videos = [_video(name=None, original_filename="raw_upload.mov")]

    contexts = build_video_contexts(videos)

    assert contexts[0].display_name == "raw_upload.mov"


def test_build_video_contexts_empty_list() -> None:
    assert build_video_contexts([]) == []
