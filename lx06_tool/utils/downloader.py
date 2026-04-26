"""
Async file downloader for LX06 Flash Tool.

Provides resumable downloads with progress tracking,
suitable for downloading aml-flash-tool, firmware images, and
pre-compiled binaries from GitHub releases.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]  # (bytes_downloaded, total_bytes)


class AsyncDownloader:
    """Async HTTP file downloader with progress tracking.

    Usage:
        downloader = AsyncDownloader()
        await downloader.download_file(
            "https://github.com/radxa/aml-flash-tool/archive/refs/heads/main.tar.gz",
            output_path=Path("./tools/aml-flash-tool.tar.gz"),
            on_progress=lambda done, total: print(f"{done}/{total}"),
        )
    """

    def __init__(self, proxy: str = "", chunk_size: int = 65536):
        self._proxy = proxy
        self._chunk_size = chunk_size

    async def download_file(
        self,
        url: str,
        output_path: Path,
        *,
        on_progress: ProgressCallback | None = None,
        headers: dict[str, str] | None = None,
        resume: bool = True,
    ) -> Path:
        """Download a file asynchronously with optional resume support.

        Args:
            url: URL to download from.
            output_path: Destination file path.
            on_progress: Callback with (bytes_downloaded, total_bytes).
            headers: Extra HTTP headers.
            resume: If True, resume partial downloads.

        Returns:
            Path to the downloaded file.

        Raises:
            DownloadError: If the download fails.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Check for partial download
        resume_from = 0
        if resume and output_path.exists():
            resume_from = output_path.stat().st_size
            if resume_from > 0:
                logger.info("Resuming download from %d bytes: %s", resume_from, output_path.name)
            else:
                resume_from = 0

        request_headers = dict(headers or {})
        if resume_from > 0:
            request_headers["Range"] = f"bytes={resume_from}-"

        proxy_url = self._proxy or None

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                proxy=proxy_url,
                timeout=httpx.Timeout(30.0, read=300.0),
            ) as client, client.stream("GET", url, headers=request_headers) as response:

                # Handle HTTP 416 Range Not Satisfiable — delete partial and restart
                if response.status_code == 416:
                    logger.warning(
                        "HTTP 416 (Range Not Satisfiable) for %s, restarting from scratch",
                        output_path.name,
                    )
                    # Consume the response body to release the connection cleanly
                    async for _ in response.aiter_bytes(self._chunk_size):
                        pass

                    # Now outside the stream context we'll retry (see below)
                    # Signal retry by raising a sentinel
                    raise _RetryWithoutResume()

                if response.status_code not in (200, 206):
                    raise DownloadError(
                        f"HTTP {response.status_code} downloading {url}: {response.reason_phrase}"
                    )

                total_size = int(response.headers.get("content-length", 0))
                if response.status_code == 206:
                    # Partial content — total is the full file size from Content-Range
                    content_range = response.headers.get("content-range", "")
                    if "/" in content_range:
                        total_size = int(content_range.split("/")[-1])
                elif resume_from > 0 and total_size > 0:
                    total_size += resume_from

                mode = "ab" if response.status_code == 206 else "wb"
                bytes_downloaded = resume_from

                with open(output_path, mode) as f:
                    async for chunk in response.aiter_bytes(self._chunk_size):
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        if on_progress:
                            try:
                                on_progress(bytes_downloaded, total_size)
                            except Exception:
                                pass

            logger.info(
                "Downloaded %s (%d bytes) → %s",
                url.split("?")[0].split("/")[-1],
                bytes_downloaded,
                output_path,
            )
            return output_path

        except _RetryWithoutResume:
            # Delete the partial file and retry from scratch
            if output_path.exists():
                output_path.unlink()
                logger.info("Deleted partial file %s, retrying from scratch", output_path.name)
            return await self.download_file(
                url, output_path, on_progress=on_progress, headers=headers, resume=False,
            )
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(f"Failed to download {url}: {exc}") from exc

    async def download_string(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        """Download a URL and return the response body as a string.

        Useful for fetching API responses, version checks, etc.
        """
        proxy_url = self._proxy or None
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                proxy=proxy_url,
                timeout=httpx.Timeout(30.0),
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            raise DownloadError(f"Failed to fetch {url}: {exc}") from exc

    @staticmethod
    async def clone_git_repo(
        repo_url: str,
        dest_dir: Path,
        branch: str = "main",
        depth: int = 1,
    ) -> Path:
        """Clone a git repository.

        Uses a shallow clone by default for speed.

        Args:
            repo_url: Git repository URL.
            dest_dir: Destination directory.
            branch: Branch or tag to clone.
            depth: Clone depth (1 for shallow).

        Returns:
            Path to the cloned directory.
        """
        if dest_dir.exists() and (dest_dir / ".git").exists():
            logger.info("Git repo already exists at %s, pulling latest", dest_dir)
            from lx06_tool.utils.compat import AsyncRunner
            runner = AsyncRunner()
            await runner.run(["git", "-C", str(dest_dir), "pull"], check=True)
            return dest_dir

        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning %s (branch=%s, depth=%d) → %s", repo_url, branch, depth, dest_dir)

        from lx06_tool.utils.compat import AsyncRunner
        runner = AsyncRunner()
        await runner.run(
            ["git", "clone", "--depth", str(depth), "--branch", branch, repo_url, str(dest_dir)],
            check=True,
            timeout=120,
        )
        return dest_dir


class _RetryWithoutResume(Exception):
    """Internal sentinel to signal a retry without resume after HTTP 416."""


class DownloadError(Exception):
    """A file download failed."""
