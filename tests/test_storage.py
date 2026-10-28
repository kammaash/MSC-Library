import os

import pytest

from storage import LocalStorage, R2Storage, get_storage, normalize_key


def test_normalize_key_converts_windows_paths():
    assert normalize_key("uploads\\pdfs\\a.pdf") == "uploads/pdfs/a.pdf"


def test_normalize_key_strips_leading_slash():
    assert normalize_key("/uploads/a.pdf") == "uploads/a.pdf"


def test_normalize_key_rejects_traversal():
    with pytest.raises(ValueError):
        normalize_key("uploads/../instance_secret.key")


def test_normalize_key_rejects_empty():
    for bad in ("", "/", "\\"):
        with pytest.raises(ValueError):
            normalize_key(bad)


def test_normalize_key_rejects_non_strings():
    for bad in (None, 5, b"uploads/a.pdf", ["uploads", "a.pdf"]):
        with pytest.raises(ValueError):
            normalize_key(bad)


def test_normalize_key_rejects_empty_and_dot_segments():
    for bad in ("a//b", "a/./b", "./a", "a/.", "uploads/", "uploads\\\\x.pdf/", "a/../b"):
        with pytest.raises(ValueError):
            normalize_key(bad)


def test_list_keys_still_accepts_trailing_slash_prefix(tmp_path):
    s = LocalStorage(str(tmp_path)); s.save_bytes(b"x", "uploads/a/x.png")
    assert s.list_keys("uploads/a/") == ["uploads/a/x.png"]
    assert s.list_keys("") == ["uploads/a/x.png"]


def test_local_roundtrip(tmp_path):
    s = LocalStorage(str(tmp_path)); s.save_bytes(b"hi", "uploads/qrcodes/x.png")
    assert open(s.local_path("uploads\\qrcodes\\x.png"), "rb").read() == b"hi"
    assert s.list_keys("uploads/qrcodes/") == ["uploads/qrcodes/x.png"]
    s.delete("uploads/qrcodes/x.png"); s.delete("uploads/qrcodes/x.png")  # second call is a no-op
    assert s.list_keys("uploads/qrcodes/") == []


def test_local_save_file_copies_and_keeps_source(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    base = tmp_path / "base"
    s = LocalStorage(str(base))
    assert s.save_file(str(src), "uploads\\pdfs\\a.pdf") == "uploads/pdfs/a.pdf"
    assert src.read_bytes() == b"data"
    assert (base / "uploads" / "pdfs" / "a.pdf").read_bytes() == b"data"


def test_local_delete_never_raises(tmp_path):
    s = LocalStorage(str(tmp_path))
    s.delete(None)
    s.delete("")
    s.delete("../outside")
    s.delete("uploads/")  # a directory path, not a file


def test_local_path_stays_inside_base_dir(tmp_path):
    s = LocalStorage(str(tmp_path))
    with pytest.raises(ValueError):
        s.local_path("../etc/passwd")
    assert s.local_path("uploads/a.pdf").startswith(os.path.realpath(str(tmp_path)))


def test_local_list_keys_empty_when_prefix_missing(tmp_path):
    assert LocalStorage(str(tmp_path)).list_keys("uploads/nothing/") == []


def test_r2_roundtrip_and_presign(r2):
    r2.save_bytes(b"%PDF", "uploads/pdfs/a.pdf", "application/pdf")
    url = r2.presigned_url("uploads/pdfs/a.pdf", expires_in=1800)
    assert "X-Amz-Expires=1800" in url and "response-content-disposition=inline" in url
    assert r2.list_keys("uploads/pdfs/") == ["uploads/pdfs/a.pdf"]


def test_r2_presign_attachment_filename(r2):
    url = r2.presigned_url("uploads/backups/b.db", expires_in=300, download_name="b.db")
    assert "response-content-disposition=attachment" in url
    assert "b.db" in url


def test_r2_presign_non_ascii_filename(r2):
    from urllib.parse import parse_qs, quote, urlparse
    url = r2.presigned_url("uploads/pdfs/a.pdf", expires_in=300, download_name='తెలుగు "x".pdf')
    disposition = parse_qs(urlparse(url).query)["response-content-disposition"][0]
    assert disposition == (
        'attachment; filename="______ x.pdf"; '
        f"filename*=UTF-8''{quote('తెలుగు \"x\".pdf', safe='')}"
    )
    disposition.encode("ascii")  # header-safe


def test_r2_save_file_and_delete(r2, tmp_path):
    src = tmp_path / "f.png"
    src.write_bytes(b"png")
    assert r2.save_file(str(src), "uploads\\qrcodes\\f.png", "image/png") == "uploads/qrcodes/f.png"
    assert src.exists()
    obj = r2.client.get_object(Bucket=r2.bucket, Key="uploads/qrcodes/f.png")
    assert obj["Body"].read() == b"png" and obj["ContentType"] == "image/png"
    r2.delete("uploads/qrcodes/f.png")
    r2.delete("uploads/qrcodes/f.png")
    r2.delete(None)
    r2.delete("../bad")
    assert r2.list_keys("uploads/qrcodes/") == []


def test_r2_list_keys_paginates_and_sorts(r2):
    for i in range(1005):
        r2.client.put_object(Bucket=r2.bucket, Key=f"uploads/many/{i:05d}.txt", Body=b"")
    keys = r2.list_keys("uploads/many/")
    assert len(keys) == 1005
    assert keys == sorted(keys)


def test_get_storage_local_when_no_r2_vars(tmp_path):
    assert isinstance(get_storage(str(tmp_path), env={}), LocalStorage)


def test_get_storage_treats_empty_values_as_unset(tmp_path):
    env = {"R2_ACCOUNT_ID": "", "R2_ACCESS_KEY_ID": "", "R2_SECRET_ACCESS_KEY": "", "R2_BUCKET_NAME": ""}
    assert isinstance(get_storage(str(tmp_path), env=env), LocalStorage)


def test_get_storage_rejects_partial_r2_config(tmp_path):
    env = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "b", "R2_BUCKET_NAME": "c"}
    with pytest.raises(RuntimeError, match="R2_SECRET_ACCESS_KEY"):
        get_storage(str(tmp_path), env=env)


def test_get_storage_partial_names_every_missing_var(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        get_storage(str(tmp_path), env={"R2_ACCOUNT_ID": "a"})
    for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        assert name in str(exc.value)


def test_get_storage_builds_r2_client(tmp_path):
    env = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "k",
           "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET_NAME": "bkt"}
    s = get_storage(str(tmp_path), env=env)
    assert isinstance(s, R2Storage) and s.remote is True and s.bucket == "bkt"
    assert s.client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    cfg = s.client.meta.config
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
