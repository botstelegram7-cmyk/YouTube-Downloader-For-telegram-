"""Link extraction, cleaning and the private-network guard."""

from __future__ import annotations

from bot.urls import check_url_safety, clean_url, extract_url, ip_is_public, looks_like_url


def test_extracts_a_url_from_a_sentence():
    assert extract_url("look at https://youtu.be/dQw4w9WgXcQ please") == "https://youtu.be/dQw4w9WgXcQ"
    assert extract_url("no link here") is None
    assert extract_url("") is None


def test_strips_trailing_punctuation():
    assert extract_url("(see https://example.com/video).") == "https://example.com/video"


def test_removes_tracking_parameters_only():
    url = "https://www.youtube.com/watch?v=abc123&si=tracking&feature=share&t=42"
    cleaned = clean_url(url)
    assert "si=" not in cleaned
    assert "feature=" not in cleaned
    assert "v=abc123" in cleaned
    assert "t=42" in cleaned


def test_looks_like_url():
    assert looks_like_url("https://example.com") is True
    assert looks_like_url("hello") is False


def test_ip_is_public_knows_the_private_ranges():
    assert ip_is_public("8.8.8.8") is True
    assert ip_is_public("1.1.1.1") is True
    assert ip_is_public("127.0.0.1") is False
    assert ip_is_public("10.0.0.5") is False
    assert ip_is_public("192.168.1.10") is False
    assert ip_is_public("172.16.5.5") is False
    assert ip_is_public("169.254.169.254") is False  # cloud metadata
    assert ip_is_public("::1") is False
    assert ip_is_public("not-an-ip") is False


def test_public_hosts_are_allowed():
    allowed, reason = check_url_safety("https://example.com/video", resolver=lambda host: ["93.184.216.34"])
    assert allowed is True and reason == ""


def test_loopback_and_private_hostnames_are_blocked():
    for url in ("http://127.0.0.1:8000/x", "http://169.254.169.254/latest/meta-data/", "http://localhost/x"):
        allowed, reason = check_url_safety(url, resolver=lambda host: ["10.0.0.1"])
        assert allowed is False, url
        assert reason


def test_a_hostname_resolving_into_a_private_network_is_blocked():
    allowed, reason = check_url_safety("https://internal.example/secret", resolver=lambda host: ["192.168.0.9"])
    assert allowed is False
    assert "private network" in reason


def test_non_http_schemes_are_refused():
    for url in ("ftp://example.com/file", "file:///etc/passwd", "javascript:alert(1)"):
        allowed, _ = check_url_safety(url, resolver=lambda host: ["93.184.216.34"])
        assert allowed is False, url


def test_unresolvable_hosts_are_reported():
    allowed, reason = check_url_safety("https://does-not-exist.example/x", resolver=lambda host: [])
    assert allowed is False
    assert "resolve" in reason


def test_overlong_urls_are_refused():
    allowed, _ = check_url_safety("https://example.com/" + "x" * 3000, resolver=lambda host: ["93.184.216.34"])
    assert allowed is False


def test_private_urls_can_be_allowed_for_local_testing():
    """The end-to-end harness serves its fixture on 127.0.0.1."""
    blocked, _ = check_url_safety("http://127.0.0.1:8092/clip.mp4", allow_private=False)
    allowed, reason = check_url_safety("http://127.0.0.1:8092/clip.mp4", allow_private=True)
    assert blocked is False
    assert allowed is True and reason == ""
