"""Unit tests for server/doc_images.py (ms-43 e-1660).

Covers validation (content-type allowlist / size cap / empty data), URL
construction, and GCS upload call shape (mocked, no real network).
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import doc_images  # noqa: E402
from doc_images import UploadResult, upload_image  # noqa: E402


# ---------------------------------------------------------------------------
# Validation tests (no GCS mock needed — they raise before client init)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_data_raises(self):
        with pytest.raises(ValueError, match="empty image data"):
            upload_image("proj-x", "x.png", b"")

    def test_size_cap_raises(self):
        # 11 MiB > 10 MiB cap
        big = b"x" * (11 * 1024 * 1024)
        with pytest.raises(ValueError, match="too large"):
            upload_image("proj-x", "x.png", big)

    def test_unsupported_extension_raises(self):
        with pytest.raises(ValueError, match="Unsupported content type"):
            upload_image("proj-x", "evil.exe", b"MZ\x90\x00...")

    def test_text_file_raises(self):
        with pytest.raises(ValueError, match="Unsupported content type"):
            upload_image("proj-x", "notes.txt", b"hello")

    def test_unsupported_image_subtype_raises(self):
        # BMP is image/* but not in our explicit allowlist
        with pytest.raises(ValueError, match="Unsupported content type"):
            upload_image("proj-x", "x.bmp", b"BM\x00\x00")

    def test_invalid_project_id_with_slash_raises(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            upload_image("proj/sub", "x.png", b"abc")

    def test_empty_project_id_raises(self):
        with pytest.raises(ValueError, match="invalid project_id"):
            upload_image("", "x.png", b"abc")


# ---------------------------------------------------------------------------
# Upload happy path (GCS client mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gcs(monkeypatch):
    """Patch the GCS client so tests run without network / credentials."""
    fake_blob = MagicMock()
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    monkeypatch.setattr(doc_images, "_gcs_client", None)
    monkeypatch.setattr(doc_images, "_get_client", lambda: fake_client)
    return {"client": fake_client, "bucket": fake_bucket, "blob": fake_blob}


class TestUploadHappyPath:
    def test_png_upload_returns_public_url(self, mock_gcs):
        result = upload_image("proj-test", "wireframe.png", b"\x89PNG\r\n...")
        assert isinstance(result, UploadResult)
        assert result.url.startswith("https://storage.googleapis.com/")
        assert result.content_type == "image/png"
        assert result.size == len(b"\x89PNG\r\n...")
        assert "proj-test" in result.key
        assert result.key.endswith(".png")

    def test_markdown_tag_constructed(self, mock_gcs):
        result = upload_image("proj-test", "design.jpg", b"\xff\xd8\xff\xe0...")
        assert result.markdown.startswith("![design.jpg](")
        assert result.markdown.endswith(")")
        assert result.url in result.markdown

    def test_alt_text_sanitizes_brackets(self, mock_gcs):
        # filename with markdown-breaking characters should be stripped from alt
        result = upload_image("proj-test", "[bad]name.png", b"\x89PNG")
        # extract alt: between `![` and `](`
        alt_section = result.markdown.split("](")[0][2:]
        assert "[" not in alt_section
        assert "]" not in alt_section
        assert alt_section == "badname.png"

    def test_gcs_blob_upload_called(self, mock_gcs):
        upload_image("proj-test", "x.png", b"PNG_BYTES")
        blob = mock_gcs["blob"]
        # uploaded_from_file should have been invoked exactly once
        assert blob.upload_from_file.call_count == 1
        kwargs = blob.upload_from_file.call_args.kwargs
        assert kwargs["content_type"] == "image/png"
        assert kwargs["size"] == len(b"PNG_BYTES")

    def test_blob_metadata_set(self, mock_gcs):
        upload_image("proj-foo", "wire.png", b"PNG")
        blob = mock_gcs["blob"]
        assert blob.metadata["original_filename"] == "wire.png"
        assert blob.metadata["uploaded_by_project"] == "proj-foo"

    def test_make_public_called(self, mock_gcs):
        upload_image("proj-test", "x.png", b"PNG")
        assert mock_gcs["blob"].make_public.call_count == 1

    def test_make_public_failure_does_not_abort(self, mock_gcs):
        # Uniform-policy buckets reject ACL changes — we must still succeed.
        mock_gcs["blob"].make_public.side_effect = RuntimeError("uniform bucket")
        result = upload_image("proj-test", "x.png", b"PNG")
        assert result.url.startswith("https://storage.googleapis.com/")

    @pytest.mark.parametrize("ext,mime", [
        ("a.png", "image/png"),
        ("b.jpg", "image/jpeg"),
        ("c.jpeg", "image/jpeg"),
        ("d.gif", "image/gif"),
        ("e.webp", "image/webp"),
        ("f.svg", "image/svg+xml"),
    ])
    def test_allowed_image_types(self, mock_gcs, ext, mime):
        result = upload_image("proj-x", ext, b"binary")
        assert result.content_type == mime


# ---------------------------------------------------------------------------
# Key generation: uniqueness + structure
# ---------------------------------------------------------------------------


class TestKeyGeneration:
    def test_keys_are_unique_across_calls(self, mock_gcs):
        r1 = upload_image("proj-x", "x.png", b"a")
        r2 = upload_image("proj-x", "x.png", b"b")
        assert r1.key != r2.key

    def test_key_includes_project_id(self, mock_gcs):
        r = upload_image("my-cool-project", "x.png", b"a")
        assert "my-cool-project" in r.key

    def test_key_preserves_extension(self, mock_gcs):
        r = upload_image("p", "design.jpeg", b"a")
        assert r.key.endswith(".jpeg")

    def test_key_under_env_prefix(self, mock_gcs):
        r = upload_image("p", "x.png", b"a")
        # Default env is "dev" in test envs without BEACON_ENV set
        assert r.key.startswith("doc-images/")
