from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Optional, Tuple

import requests


@dataclass
class GithubFileResult:
    file_name: str
    content_text: str
    size_bytes: int
    source: str  # 'raw' or 'api'


def _is_github_blob_url(url: str) -> bool:
    return url.startswith("https://github.com/") and "/blob/" in url


def _is_github_repo_root(url: str) -> bool:
    if not url.startswith("https://github.com/"):
        return False
    remainder = url[len("https://github.com/") :].strip("/")
    parts = remainder.split("/")
    return len(parts) == 2  # owner/repo


def _split_repo_root(url: str) -> Tuple[str, str]:
    remainder = url[len("https://github.com/") :].strip("/")
    owner, repo = remainder.split("/")
    return owner, repo


def _convert_blob_to_raw(url: str) -> Optional[str]:
    # https://github.com/{owner}/{repo}/blob/{ref}/{path} ->
    # https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
    try:
        prefix = "https://github.com/"
        remainder = url[len(prefix):]
        parts = remainder.split("/")
        owner, repo = parts[0], parts[1]
        # find 'blob' segment
        blob_index = parts.index("blob")
        ref = parts[blob_index + 1]
        path_parts = parts[blob_index + 2 :]
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/" + "/".join(path_parts)
        return raw
    except Exception:
        return None


def _infer_filename_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def fetch_public_raw(url: str, timeout_sec: int = 20) -> GithubFileResult:
    response = requests.get(url, timeout=timeout_sec)
    response.raise_for_status()
    content_bytes = response.content
    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Best-effort: fall back to latin-1 to keep bytes mapping 1:1
        content_text = content_bytes.decode("latin-1")
    return GithubFileResult(
        file_name=_infer_filename_from_url(url),
        content_text=content_text,
        size_bytes=len(content_bytes),
        source="raw",
    )


def fetch_via_github_api(url: str, token: str, timeout_sec: int = 20) -> GithubFileResult:
    # Support two forms:
    # 1) Full blob URL like https://github.com/{owner}/{repo}/blob/{ref}/{path}
    # 2) Raw URL like https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}
    owner, repo, ref, path = _parse_github_url_to_components(url)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
    }
    response = requests.get(api_url, headers=headers, timeout=timeout_sec)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        raise ValueError("The provided URL points to a directory, not a file.")
    if data.get("encoding") == "base64" and "content" in data:
        content_bytes = base64.b64decode(data["content"])  # type: ignore[arg-type]
    else:
        # Fallback: attempt to fetch 'download_url'
        download_url = data.get("download_url")
        if not download_url:
            raise ValueError("Unable to retrieve file content from GitHub API response.")
        headers_no_accept = {k: v for k, v in headers.items() if k != "Accept"}
        resp_bin = requests.get(download_url, headers=headers_no_accept, timeout=timeout_sec)
        resp_bin.raise_for_status()
        content_bytes = resp_bin.content

    try:
        content_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content_text = content_bytes.decode("latin-1")

    file_name = data.get("name") or _infer_filename_from_url(url)
    return GithubFileResult(
        file_name=file_name,
        content_text=content_text,
        size_bytes=len(content_bytes),
        source="api",
    )


def _parse_github_url_to_components(url: str) -> Tuple[str, str, str, str]:
    # Returns (owner, repo, ref, path)
    if url.startswith("https://raw.githubusercontent.com/"):
        remainder = url[len("https://raw.githubusercontent.com/") :]
        parts = remainder.split("/")
        if len(parts) < 4:
            raise ValueError("Invalid raw.githubusercontent.com URL format")
        owner, repo, ref = parts[0], parts[1], parts[2]
        path = "/".join(parts[3:])
        return owner, repo, ref, path

    if _is_github_blob_url(url):
        converted = _convert_blob_to_raw(url)
        if not converted:
            raise ValueError("Invalid GitHub blob URL format")
        return _parse_github_url_to_components(converted)

    raise ValueError("Unsupported GitHub URL. Provide a standard blob or raw URL.")


def _get_default_branch(owner: str, repo: str, token: Optional[str], timeout_sec: int = 15) -> Optional[str]:
    api = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(api, headers=headers, timeout=timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        return data.get("default_branch")
    except Exception:
        return None


def read_github_file(url: str, token: Optional[str] = None) -> GithubFileResult:
    """Read a GitHub file from a standard URL or repo root.

    - If token is provided, uses GitHub API (works for private repos).
    - If a repo root URL is provided, attempts to read README.md from default/main/master.
    - Otherwise, uses the raw URL path (public files) or API with token.
    """
    if not url:
        raise ValueError("url is required")

    # Handle repository root by targeting README.md
    if _is_github_repo_root(url):
        owner, repo = _split_repo_root(url)
        if token:
            default_branch = _get_default_branch(owner, repo, token) or "main"
            guess_blob = f"https://github.com/{owner}/{repo}/blob/{default_branch}/README.md"
            return fetch_via_github_api(url=guess_blob, token=token)
        else:
            # Try public raw main then master
            for branch in ("main", "master"):
                raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
                try:
                    return fetch_public_raw(raw)
                except Exception:
                    continue
            raise ValueError(
                "Could not locate README.md at main or master. Provide a direct file URL or a token for private repos."
            )

    if token:
        return fetch_via_github_api(url=url, token=token)

    # No token: try raw first, otherwise convert blob to raw
    raw_url = url
    if _is_github_blob_url(url):
        raw_url = _convert_blob_to_raw(url) or url

    return fetch_public_raw(raw_url)