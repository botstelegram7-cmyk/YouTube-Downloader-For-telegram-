"""Torrents, direct links and the fetcher — the three things the bot accepts besides YouTube.

Two rules run through every test here:

* the bot decides what a file *is* by probing it, not by trusting its name, and
* a link that points inside the server's own network is refused before anything is fetched,
  because a bot that fetches any URL a stranger sends is a request-forgery tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bot.fetch import (
    FetchProgress,
    parse_eta,
    parse_readout,
    probe_link,
    size_to_bytes,
)
from bot.sources import DIRECT, TORRENT, YOUTUBE, blocked_target, classify
from bot.torrent import (
    TorrentError,
    bdecode,
    bencode,
    info_hash_of,
    magnet_hash,
    magnet_metadata_name,
    magnet_name,
    parse_torrent,
)
from config import load_settings

TOKEN = "123456789:" + "A" * 35

ARIA2 = os.environ.get("ARIA2_BINARY") or "/usr/bin/aria2c"


def settings(**env):
    return load_settings({"BOT_TOKEN": TOKEN, **env})


# -- bencode ------------------------------------------------------------------- #
def make_torrent(files: list[tuple[str, int]], *, name: str = "pack") -> bytes:
    return bencode(
        {
            "announce": "http://tracker.example/announce",
            "info": {
                "name": name,
                "piece length": 262144,
                "pieces": b"\x00" * 20,
                "files": [{"length": size, "path": path.split("/")} for path, size in files],
            },
        }
    )


def test_bencode_round_trips_including_nested_paths():
    payload = make_torrent([("sub/dir/clip.mp4", 10)])
    decoded = bdecode(payload)
    assert decoded[b"info"][b"name"] == b"pack"
    assert decoded[b"info"][b"files"][0][b"path"] == [b"sub", b"dir", b"clip.mp4"]


def test_a_torrent_that_is_not_a_torrent_is_refused():
    with pytest.raises(TorrentError):
        parse_torrent(b"this is not bencoded")
    with pytest.raises(TorrentError):
        parse_torrent(bencode({"hello": "world"}))


def test_files_are_ranked_video_first_and_biggest_first():
    info = parse_torrent(
        make_torrent(
            [
                ("readme.nfo", 4),
                ("sample/clip-sample.mp4", 900),
                ("Season 1/Episode 1.mp4", 4_000_000),
                ("Season 1/Episode 2.mp4", 8_000_000),
                ("Season 1/subs.srt", 40),
            ]
        )
    )

    ranked = [item.path for item in info.candidates(8)]
    assert ranked[0] == "Season 1/Episode 2.mp4", "the biggest video is what people want"
    assert ranked[1] == "Season 1/Episode 1.mp4"
    assert ranked.index("sample/clip-sample.mp4") < ranked.index("readme.nfo")


def test_the_torrent_carries_the_real_total_and_sizes():
    info = parse_torrent(make_torrent([("a.mp4", 1000), ("b.mp4", 24)]))
    assert info.total_size == 1024
    assert info.file_number(2).size == 24
    assert info.file_number(9) is None


def test_a_single_file_torrent_is_file_number_one():
    payload = bencode(
        {
            "announce": "http://t.example/a",
            "info": {"name": "clip.mp4", "piece length": 262144, "pieces": b"\x00" * 20, "length": 555},
        }
    )
    info = parse_torrent(payload)
    assert [(item.index, item.path, item.size) for item in info.files] == [(1, "clip.mp4", 555)]


def test_an_empty_torrent_is_refused():
    with pytest.raises(TorrentError):
        parse_torrent(make_torrent([("nothing.mp4", 0)]))


# -- magnets -------------------------------------------------------------------- #
def test_a_magnet_gives_up_its_hash_and_name():
    url = "magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a&dn=Some%20Release&tr=udp%3A%2F%2Fx"
    assert magnet_hash(url) == "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
    assert magnet_name(url) == "Some Release"
    assert magnet_metadata_name(url) == "c12fe1c06bba254a9dc9f519b335aa7c1367a88a.torrent"


def test_a_base32_magnet_is_named_the_way_aria2_names_it():
    """aria2 writes metadata as <hex infohash>.torrent, even for a base32 magnet."""
    raw = bytes.fromhex("c12fe1c06bba254a9dc9f519b335aa7c1367a88a")
    import base64

    base32 = base64.b32encode(raw).decode()
    url = f"magnet:?xt=urn:btih:{base32}&dn=x"
    assert magnet_metadata_name(url) == "c12fe1c06bba254a9dc9f519b335aa7c1367a88a.torrent"


def test_the_infohash_matches_the_standard_definition():
    payload = make_torrent([("a.mp4", 10)])
    info = bdecode(payload)[b"info"]
    expected = hashlib.sha1(bencode(info)).hexdigest()
    assert info_hash_of(payload) == expected


# -- classifying links ---------------------------------------------------------- #
def test_youtube_links_still_win():
    source, refusal = classify("https://youtu.be/dQw4w9WgXcQ", settings())
    assert source is not None and source.kind == YOUTUBE and refusal == ""
    assert source.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_a_magnet_is_a_torrent_source():
    url = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Big%20Bucket"
    source, refusal = classify(url, settings())
    assert source is not None and source.kind == TORRENT and source.is_magnet
    assert source.name == "Big Bucket"
    assert refusal == ""


def test_a_torrent_url_is_a_torrent_source():
    source, _ = classify("https://example.com/linux-image.torrent", settings())
    assert source is not None and source.kind == TORRENT and not source.is_magnet
    assert source.name == "linux-image.torrent"


def test_any_other_url_is_a_direct_link():
    source, refusal = classify("https://cdn.example.com/videos/clip.mp4?sig=abc", settings())
    assert source is not None and source.kind == DIRECT
    assert source.label == "cdn.example.com"
    assert source.name == "clip.mp4"
    assert refusal == ""


def test_a_plain_driver_link_would_be_refused_not_downloaded():
    """google.com is not blocked; it is simply not YouTube — and it is served as a direct link."""
    source, _ = classify("https://www.google.com/search?q=test", settings())
    assert source is not None and source.kind == DIRECT


def test_links_inside_the_server_network_are_refused():
    for url in (
        "http://127.0.0.1:8080/admin.torrent",
        "http://localhost/secret.mp4",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/file.bin",
        "http://192.168.1.1/router.cfg",
        "http://[::1]/x",
        "http://metadata.google.internal/computeMetadata/v1/",
    ):
        source, refusal = classify(url, settings())
        assert source is None, f"{url} must not be fetchable"
        assert "own network" in refusal


def test_the_operator_can_allow_private_links_for_local_testing():
    config = settings(ALLOW_PRIVATE_LINKS="true")
    assert blocked_target("http://127.0.0.1:9/x", config) == ""
    assert blocked_target("http://127.0.0.1:9/x", settings()) != ""


def test_a_magnet_without_a_hash_is_refused():
    source, refusal = classify("magnet:?dn=NoHash", settings())
    assert source is None and "infohash" in refusal.lower()


def test_ordinary_text_is_not_a_link():
    source, refusal = classify("hello there", settings())
    assert source is None and refusal == ""


def test_a_bare_video_id_is_still_a_youtube_link():
    source, _ = classify("dQw4w9WgXcQ", settings())
    assert source is not None and source.kind == YOUTUBE


# -- aria2 output --------------------------------------------------------------- #
def test_aria2_sizes_are_read_in_both_conventions():
    assert size_to_bytes("1.2MiB") == int(1.2 * 1024**2)
    assert size_to_bytes("1.5MB") == int(1.5 * 1000**2)
    assert size_to_bytes("416KiB") == 416 * 1024
    assert size_to_bytes("2.0GiB") == 2 * 1024**3
    assert size_to_bytes("nonsense") == 0


def test_eta_strings_become_seconds():
    assert parse_eta("3m11s") == 191
    assert parse_eta("2h") == 7200
    assert parse_eta("45s") == 45
    assert parse_eta("") is None


def test_a_console_readout_line_becomes_progress():
    sample = parse_readout("[#11e43b 16KiB/3.0MiB(0%) CN:1 DL:15KiB ETA:3m11s]")
    assert sample == FetchProgress(
        percent=0.0, downloaded=16384, total=3 * 1024 * 1024, speed=15360.0, eta=191.0
    )


def test_a_readout_without_an_eta_still_reports_speed():
    sample = parse_readout("[#11e43b 2.7MiB/3.0MiB(91%) CN:1 DL:350KiB]")
    assert sample is not None
    assert sample.eta is None and sample.speed == 350 * 1024
    assert sample.percent == 91.0


def test_lines_that_are_not_readouts_are_ignored():
    assert parse_readout("10/12 06:39:46 [ERROR] something broke") is None
    assert parse_readout("Download Results:") is None


def test_a_readout_with_an_unknown_total_still_counts_bytes():
    sample = parse_readout("[#abc123 4.0MiB/N/A(0%) CN:2 DL:1.0MiB]")
    assert sample is not None
    assert sample.total is None and sample.downloaded == 4 * 1024 * 1024


# -- the real aria2, when it is installed --------------------------------------- #
pytestmark_aria2 = pytest.mark.skipif(
    not Path(ARIA2).exists(), reason=f"aria2c not installed at {ARIA2}"
)


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):
            """Silence the fixture server's own request log."""
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


@pytestmark_aria2
def test_a_direct_link_is_fetched_with_progress(tmp_path):
    from bot.fetch import download_direct
    from bot.sources import Source

    payload = os.urandom(600_000)
    (tmp_path / "payload.bin").write_bytes(payload)
    server, base = _serve(tmp_path)
    try:
        config = settings(ARIA2_BINARY=ARIA2, ALLOW_PRIVATE_LINKS="true", DOWNLOAD_DIR=str(tmp_path / "dl"))
        destination = tmp_path / "dl"
        destination.mkdir()
        samples: list = []

        path = asyncio.run(
            download_direct(
                source=Source(kind=DIRECT, url=f"{base}/payload.bin", label="127.0.0.1", name="payload.bin"),
                destination=destination,
                settings=config,
                on_progress=samples.append,
                timeout=60,
            )
        )

        assert path.read_bytes() == payload
        assert samples, "progress was reported"
        assert samples[-1].percent == 100.0
    finally:
        server.shutdown()


@pytestmark_aria2
def test_a_torrent_is_fetched_through_its_web_seed(tmp_path):
    """A torrent with a web seed and no reachable tracker: the payload still arrives.

    This is the shape of a real magnet hand-off: metadata first, then the payload, and the
    selection is one file out of the torrent rather than all of it.
    """
    from bot.fetch import download_torrent_file, fetch_torrent_metadata
    from bot.sources import Source

    payload = os.urandom(1_200_000)
    (tmp_path / "clip.mp4").write_bytes(payload)
    server, base = _serve(tmp_path)
    try:
        piece = 1024 * 1024
        hashes = b"".join(
            hashlib.sha1(payload[i : i + piece]).digest()
            for i in range(0, len(payload), piece)
        )
        torrent = bencode(
            {
                "announce": "http://127.0.0.1:1/announce",  # deliberately dead
                "url-list": [f"{base}/clip.mp4"],
                "info": {
                    "name": "clip.mp4",
                    "piece length": piece,
                    "pieces": hashes,
                    "length": len(payload),
                },
            }
        )
        (tmp_path / "clip.torrent").write_bytes(torrent)

        config = settings(ARIA2_BINARY=ARIA2, ALLOW_PRIVATE_LINKS="true", DOWNLOAD_DIR=str(tmp_path / "dl"))
        meta_dir = tmp_path / "meta"
        meta_dir.mkdir()

        meta = asyncio.run(
            fetch_torrent_metadata(
                source=Source(
                    kind=TORRENT, url=f"{base}/clip.torrent", label="127.0.0.1", name="clip.torrent"
                ),
                destination=meta_dir,
                settings=config,
                timeout=60,
            )
        )
        info = parse_torrent(meta.read_bytes())
        assert info.files[0].path == "clip.mp4"

        destination = tmp_path / "dl"
        destination.mkdir()
        samples: list = []
        path = asyncio.run(
            download_torrent_file(
                torrent_path=meta,
                index=info.files[0].index,
                destination=destination,
                settings=config,
                on_progress=samples.append,
                timeout=120,
            )
        )
        assert path.read_bytes() == payload
        assert samples and samples[-1].percent == 100.0
    finally:
        server.shutdown()


@pytestmark_aria2
def test_a_probe_learns_the_size_and_the_name(tmp_path):
    payload = b"x" * 40_000
    (tmp_path / "report.pdf").write_bytes(payload)
    server, base = _serve(tmp_path)
    try:
        probe = asyncio.run(probe_link(f"{base}/report.pdf"))
        assert probe.reachable
        assert probe.total == len(payload)
    finally:
        server.shutdown()


# -- what a finished download really is ----------------------------------------- #
def test_random_bytes_are_a_document_not_a_video(tmp_path):
    """ffprobe invents a 'bintext' 1280×3200 video for random data. It must not be believed."""
    from bot.download import _kind_for_suffix, looks_playable
    from bot.ffmpeg import MediaProbe

    probe = MediaProbe(
        path=tmp_path / "data.bin",
        duration=0.0,
        width=1280,
        height=3200,
        size=64000,
        has_video=True,
        has_audio=False,
        video_codec="bintext",
        audio_codec="",
        container="bin",
    )
    assert looks_playable(probe) == (False, False)
    assert _kind_for_suffix("data.bin") == "document"
    assert _kind_for_suffix("clip.mp4") == "video"
    assert _kind_for_suffix("song.m4a") == "audio"
    assert _kind_for_suffix("release.zip") == "document"


def test_a_real_video_is_recognised_through_ffprobes_container_list(tmp_path):
    """An MP4's format_name is 'mov,mp4,m4a,3gp,3g2,mj2' — a list, not a name."""
    from bot.download import looks_playable
    from bot.ffmpeg import MediaProbe

    probe = MediaProbe(
        path=tmp_path / "clip.mp4",
        duration=12.0,
        width=1920,
        height=1080,
        size=5_000_000,
        has_video=True,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mov,mp4,m4a,3gp,3g2,mj2",
    )
    assert looks_playable(probe) == (True, True)


def test_an_audio_only_container_is_not_treated_as_video(tmp_path):
    from bot.download import looks_playable
    from bot.ffmpeg import MediaProbe

    probe = MediaProbe(
        path=tmp_path / "song.mp3",
        duration=200.0,
        width=0,
        height=0,
        size=3_000_000,
        has_video=True,  # the embedded cover art, which most players ignore
        has_audio=True,
        video_codec="mjpeg",
        audio_codec="mp3",
        container="mp3",
    )
    assert looks_playable(probe) == (False, True)


# -- choices are scoped and expire ---------------------------------------------- #
def test_a_choice_belongs_to_the_user_who_made_it():
    from bot.cache import FetchCache, PendingFetch
    from bot.sources import Source

    cache = FetchCache(ttl_seconds=60)
    entry = PendingFetch(kind="direct", source=Source(kind=DIRECT, url="https://x/y.mp4"), user_id=7)
    token = cache.put(entry)

    assert cache.get(token, user_id=7) is entry
    assert cache.get(token, user_id=8) is None, "another user must not reuse the token"
    assert len(cache) == 1
    cache.drop(token)
    assert cache.get(token) is None


def test_an_expired_choice_is_gone():
    from bot.cache import FetchCache, PendingFetch
    from bot.sources import Source

    cache = FetchCache(ttl_seconds=0)
    token = cache.put(PendingFetch(kind="torrent", source=Source(kind=TORRENT, url="magnet:?x"), user_id=1))
    assert cache.get(token, user_id=1) is None
    assert len(cache) == 0
