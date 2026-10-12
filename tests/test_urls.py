"""Link handling: YouTube only, canonicalised, with an honest refusal otherwise.

The engine treats anything it does not recognise as a *search query*, so a stray Vimeo
link would quietly download an unrelated video. These tests pin that down.
"""

from __future__ import annotations

from bot.urls import CANONICAL, canonise, extract_url, youtube_link

VIDEO = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"


def test_extracts_a_url_from_a_sentence_and_trims_punctuation():
    assert extract_url(f"look at https://youtu.be/{VIDEO} please") == f"https://youtu.be/{VIDEO}"
    assert extract_url(f"(see {WATCH}).") == WATCH
    assert extract_url("no link here") is None
    assert extract_url("") is None


def test_every_youtube_url_shape_becomes_one_canonical_watch_url():
    for url in (
        WATCH,
        f"https://youtu.be/{VIDEO}",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://youtube.com/live/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        f"https://music.youtube.com/watch?v={VIDEO}&feature=share",
    ):
        assert canonise(url) == CANONICAL.format(id=VIDEO), url


def test_tracking_parameters_and_playlists_do_not_change_the_target():
    messy = f"https://www.youtube.com/watch?v={VIDEO}&list=PLabc&index=4&si=tracking&t=42"
    assert canonise(messy) == CANONICAL.format(id=VIDEO)


def test_a_bare_video_id_is_accepted():
    url, reason = youtube_link(VIDEO)
    assert url == CANONICAL.format(id=VIDEO)
    assert reason == ""


def test_ids_of_the_wrong_length_are_not_mistaken_for_links():
    assert youtube_link("dQw4w9WgXc")[0] is None
    assert youtube_link("dQw4w9WgXcQx")[0] is None


def test_other_sites_are_refused_with_the_host_in_the_reason():
    for text, host in (
        ("https://vimeo.com/12345", "vimeo.com"),
        ("https://www.dailymotion.com/video/x123", "dailymotion.com"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ):
        url, reason = youtube_link(text)
        assert url is None, text
        assert host in reason, reason


def test_a_youtube_link_without_a_video_id_is_refused():
    url, reason = youtube_link("https://www.youtube.com/playlist?list=PLabc")
    assert url is None
    assert reason  # it is a YouTube host, but there is no video in it


def test_plain_text_is_not_reported_as_a_bad_link():
    assert youtube_link("hello there") == (None, "")
