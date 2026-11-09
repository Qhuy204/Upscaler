import pytest

from server import fetch, webhook


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost:8080/", "http://10.0.0.5/a.mp4",
                                 "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
                                 "http://[::1]/", "ftp://example.com/a.mp4", "file:///etc/passwd",
                                 "http://user:pw@example.com/a.mp4"])
async def test_check_url_blocks(url):
    with pytest.raises(fetch.FetchError):
        await fetch.check_url(url)


def test_drive_ids():
    fid = "1n7KT99gcJ6PUgXCQTIwW8EtruXYubGfW"
    assert fetch.drive_file_id(f"https://drive.google.com/file/d/{fid}/view?usp=sharing") == fid
    assert fetch.drive_file_id(f"https://drive.google.com/open?id={fid}") == fid
    assert fetch.drive_file_id(f"https://drive.google.com/uc?export=download&id={fid}") == fid
    assert fetch.drive_file_id("https://example.com/file/d/abcdefghijkl/view") is None
    folder = "https://drive.google.com/drive/folders/11QmYP3gE7KX_Ni3jsPGs7-GyCsSOI7ad?usp=drive_link"
    assert fetch.drive_folder_id(folder) == "11QmYP3gE7KX_Ni3jsPGs7-GyCsSOI7ad"
    assert fetch.drive_file_id(folder) is None


def test_pick_confirm_url_prefers_usercontent_uuid():
    page = """<html><a href="/uc?export=download&amp;confirm=abc&amp;id=FILEID123456">Download anyway</a>
    <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
    <input type="hidden" name="id" value="FILEID123456"><input type="hidden" name="export" value="download">
    <input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="u-1"></form></html>"""
    url = fetch.pick_confirm_url(page, "https://drive.google.com/uc?export=download&id=FILEID123456")
    assert url.startswith("https://drive.usercontent.google.com/download?") and "uuid=u-1" in url


def test_pick_confirm_url_cookie_fallback():
    url = fetch.pick_confirm_url("<html>nothing</html>", "https://drive.google.com/uc?export=download&id=XYZ123",
                                 cookie_confirm="tok")
    assert "confirm=tok" in url and "id=XYZ123" in url


def test_drive_html_errors():
    assert "quota" in fetch.drive_html_error("Too many users have viewed or downloaded this file", 200)
    assert "not public" in fetch.drive_html_error('<a href="https://accounts.google.com/ServiceLogin">Sign in', 200)
    assert "not found" in fetch.drive_html_error("<html></html>", 404)


def test_filename_from_content_disposition():
    assert fetch.filename_from_cd("attachment; filename=\"a b.mp4\"; filename*=UTF-8''qu%E1%BA%A3ng%20c%C3%A1o.mp4") \
        == "quảng cáo.mp4"
    assert fetch.filename_from_cd('attachment; filename="clip.mp4"') == "clip.mp4"
    assert fetch.filename_from_cd(None) is None


def test_webhook_signature_roundtrip():
    import time
    ts = str(int(time.time()))
    sig = webhook.signature("whsec_x", ts, b'{"a":1}')
    assert webhook.verify("whsec_x", ts, b'{"a":1}', sig)
    assert not webhook.verify("whsec_x", ts, b'{"a":2}', sig)
