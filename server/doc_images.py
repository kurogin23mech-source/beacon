"""Document image upload (ms-43 e-1660): GCS-backed image asset storage for
Beacon Document scope (= SPEC, memo, retro 等の本文に挿入する画像)。

設計方針 (本日 (2026-06-13/14) の対話で確定):
- agent only: 人間 UI なし、CLI / API 経由でアップロード
- public read default: bucket policy で public-read、URL は推測困難な UUID
  (= doc 自体は project membership で gated、image URL は知ってる人のみ)
- doc 自動更新しない: upload は URL を返すだけ、doc 編集は別経路
- GCP 優先 (今日): trailnode.py と同じ google-cloud-storage パターン流用
- AWS S3 は将来: store_router と並走できる形に書いておく (= 別 task で拡張)

bucket 名は env で外注: BEACON_DOC_IMAGES_BUCKET を読む、未設定なら
TrailNode の `trailnode-artifacts` を共有する (= 既存 infra 流用、別 prefix
で衝突回避)。本番運用では terraform 側で `beacon-doc-images` を別建てる方が
クリーン (= 本コード変更不要、env 設定だけ)。
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from google.cloud import storage as gcs_storage

logger = logging.getLogger(__name__)

# Bucket selection: 専用 BEACON_DOC_IMAGES_BUCKET があればそれ、無ければ
# TrailNode の bucket に doc-images/ prefix で同居する (= terraform 側で
# 専用 bucket 立つまでの繋ぎ)。
_BUCKET_NAME = (
    os.environ.get("BEACON_DOC_IMAGES_BUCKET")
    or os.environ.get("TRAILNODE_BUCKET", "trailnode-artifacts")
)
_ENV = os.environ.get("BEACON_ENV", "dev")
_KEY_PREFIX = f"doc-images/{_ENV}/"

# 許可する MIME (= browser が img tag で render できる形式に限定、
# 任意 binary upload を防ぐ簡易 gate)。
_ALLOWED_MIME_PREFIXES = ("image/",)
_ALLOWED_MIME_EXACT = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
}

# サイズ上限 (= 1 image につき 10 MiB)。SPEC に貼る wireframe / screenshot
# のサイズとしては十分、これより大きいと bucket と転送が肥大化する。
_MAX_BYTES = 10 * 1024 * 1024

_gcs_client: Optional[gcs_storage.Client] = None


@dataclass
class UploadResult:
    url: str         # 公開 https URL (= markdown ![]() の中身)
    markdown: str    # `![filename](url)` の形、そのまま doc に貼れる
    bucket: str
    key: str
    size: int
    content_type: str


def _get_client() -> gcs_storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = gcs_storage.Client()
    return _gcs_client


def _resolve_content_type(filename: str, declared: Optional[str]) -> str:
    """Sanitize content-type: 信頼するのは python-mime の判定、declared (=
    client 自称) は補助。許可リストに無ければ ValueError。"""
    guessed, _ = mimetypes.guess_type(filename)
    content_type = guessed or declared or "application/octet-stream"
    if not any(content_type.startswith(p) for p in _ALLOWED_MIME_PREFIXES):
        raise ValueError(
            f"Unsupported content type {content_type!r}: only image/* allowed"
        )
    if content_type not in _ALLOWED_MIME_EXACT:
        # image/* だが許可リスト外 (= bmp / tiff 等)。許容範囲を狭めて bucket
        # に変な形式が溜まらないようにする。
        raise ValueError(
            f"Unsupported content type {content_type!r}: "
            f"supported = {sorted(_ALLOWED_MIME_EXACT)}"
        )
    return content_type


def _make_key(project_id: str, filename: str) -> str:
    """`<env-prefix>/<project>/<uuid>.<ext>` の形で衝突困難な key を作る。
    元 filename はメタデータに残すが key からは推測困難 (= UUID v4)。"""
    _, ext = os.path.splitext(filename)
    ext = ext.lower() if ext else ""
    return f"{_KEY_PREFIX}{project_id}/{uuid.uuid4().hex}{ext}"


def upload_image(
    project_id: str,
    filename: str,
    data: bytes,
    declared_content_type: Optional[str] = None,
) -> UploadResult:
    """画像 1 枚を GCS にアップロードして公開 URL を返す。

    Args:
        project_id: Beacon project の id (= bucket 内の namespace 用)
        filename: 元ファイル名 (= alt text と拡張子推定に使う)
        data: 画像バイナリ
        declared_content_type: HTTP multipart 等で client が宣言した型 (任意)

    Returns:
        UploadResult: 公開 URL と markdown img tag

    Raises:
        ValueError: 許可しない content-type、サイズ超過、空 data
    """
    if not data:
        raise ValueError("empty image data")
    if len(data) > _MAX_BYTES:
        raise ValueError(
            f"image too large: {len(data)} bytes > {_MAX_BYTES} (10 MiB cap)"
        )
    if not project_id or "/" in project_id:
        raise ValueError(f"invalid project_id {project_id!r}")

    content_type = _resolve_content_type(filename, declared_content_type)
    key = _make_key(project_id, filename)

    client = _get_client()
    bucket = client.bucket(_BUCKET_NAME)
    blob = bucket.blob(key)
    blob.metadata = {
        "original_filename": filename,
        "uploaded_by_project": project_id,
    }
    blob.upload_from_file(
        io.BytesIO(data),
        size=len(data),
        content_type=content_type,
        rewind=True,
    )
    # 公開 read: bucket policy が uniform / public のときはこれだけで GET 可能。
    # 万一 bucket が非 public でも blob 自体に public read を付与する (= legacy
    # ACL モードでも動かす、bucket policy が uniform なら NoOp に近い fail で
    # 続行)。
    try:
        blob.make_public()
    except Exception as e:
        logger.warning("blob.make_public failed (uniform bucket?): %s", e)

    public_url = f"https://storage.googleapis.com/{_BUCKET_NAME}/{key}"
    safe_alt = filename.replace("]", "").replace("[", "")
    markdown = f"![{safe_alt}]({public_url})"

    logger.info(
        "uploaded doc image: project=%s key=%s size=%d type=%s",
        project_id, key, len(data), content_type,
    )

    return UploadResult(
        url=public_url,
        markdown=markdown,
        bucket=_BUCKET_NAME,
        key=key,
        size=len(data),
        content_type=content_type,
    )
