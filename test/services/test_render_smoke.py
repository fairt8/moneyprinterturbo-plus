"""Prove MoviePy can draw a subtitle on a short clip with the bundled font."""

import os

from moviepy import ColorClip, CompositeVideoClip, TextClip


def test_textclip_renders_a_short_clip(tmp_path):
    font = os.path.join(
        os.path.dirname(__file__), "..", "..", "resource", "fonts", "STHeitiMedium.ttc"
    )
    background = ColorClip(size=(320, 180), color=(20, 18, 16), duration=0.4)
    caption = TextClip(
        text="Hello",
        font=os.path.abspath(font),
        font_size=28,
        color="white",
        duration=0.4,
    )
    video = CompositeVideoClip(
        [background, caption.with_position(("center", "center"))],
        size=(320, 180),
    )
    output = tmp_path / "caption.mp4"
    video.write_videofile(
        str(output),
        fps=8,
        codec="libx264",
        audio=False,
        logger=None,
    )
    assert output.stat().st_size > 500
