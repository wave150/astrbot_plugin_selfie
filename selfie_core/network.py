"""Bounded public-image downloads, independent of API authentication sessions."""
from __future__ import annotations

import asyncio
import ipaddress
import math
import socket

import aiohttp
from yarl import URL


class ImageDownloadError(ValueError):
    """Unsafe target, failed HTTP request, or invalid/oversized image."""


def _public_ip(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ImageDownloadError("DNS returned an invalid IP address") from exc
    if (not address.is_global or address.is_multicast or
            address.is_unspecified or address.is_reserved or
            getattr(address, "ipv4_mapped", None) is not None or
            getattr(address, "sixtofour", None) is not None or
            getattr(address, "teredo", None) is not None or "%" in host):
        raise ImageDownloadError("Image target must use a public unicast IP address")


def _validated_url(value) -> URL:
    try:
        url = URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ImageDownloadError("Image URL must be absolute HTTP(S)")
        if url.user is not None or url.password is not None:
            raise ImageDownloadError("Image URL must not contain credentials")
        host = url.raw_host
        if not host or "%" in host:
            raise ImageDownloadError("Invalid image hostname")
        _ = url.port  # Reject malformed ports before any network I/O.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host.rstrip(".").lower() == "localhost":
                raise ImageDownloadError("Localhost is not a public image target")
        else:
            _public_ip(host)
        return url.with_fragment(None)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ImageDownloadError):
            raise
        raise ImageDownloadError("Invalid image URL") from exc


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the exact numeric answers consumed by TCPConnector.

    No separate preflight lookup: the connection uses these validated answers,
    closing the DNS-check/second-resolution rebinding race. Reject mixed answers.
    Network-level routing/proxy configuration remains outside this module's scope.
    """

    def __init__(self):
        self._delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        records = await self._delegate.resolve(host, port, family)
        if not records:
            raise ImageDownloadError("Image hostname has no addresses")
        for record in records:
            _public_ip(record["host"])
        return records

    async def close(self):
        await self._delegate.close()


def _is_image(data: bytes) -> bool:
    # Signature screening, not a full decoder or decompression-bomb guarantee.
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n") or
        data.startswith(b"\xff\xd8\xff") or
        data.startswith((b"GIF87a", b"GIF89a")) or
        (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP") or
        data.startswith((b"II*\x00", b"MM\x00*")) or
        (len(data) >= 14 and data[:2] == b"BM")
    )


def decode_image_base64(value: str, *, max_bytes=20 * 1024 * 1024) -> bytes:
    """Bound encoded and decoded data before accepting an image signature."""
    import base64
    import binascii
    if not isinstance(value, str) or len(value) > ((max_bytes + 2) // 3) * 4:
        raise ImageDownloadError("Encoded image exceeds size limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageDownloadError("Invalid encoded image") from exc
    if len(data) > max_bytes or not _is_image(data):
        raise ImageDownloadError("Invalid or oversized image data")
    return data


async def download_public_image(url: str, *, timeout_seconds=120,
                                max_bytes=20 * 1024 * 1024) -> bytes:
    """Return image bytes or raise ImageDownloadError (cancellation propagates).

    Allows JPEG/PNG/GIF/WebP/TIFF/BMP signatures. Uses no Authorization, cookies,
    netrc, environment proxies, Referer, or inherited API-session headers.
    At most five redirects; the time budget covers the entire operation.
    """
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or max_bytes <= 0):
        raise ImageDownloadError("max_bytes must be a positive integer")
    if (isinstance(timeout_seconds, bool) or
            not isinstance(timeout_seconds, (int, float)) or
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ImageDownloadError("timeout_seconds must be positive and finite")
    current = _validated_url(url)
    resolver = _PublicResolver()
    try:
        async with asyncio.timeout(timeout_seconds):
            connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                headers={"Accept": "image/*"},
            ) as session:
                for hop in range(6):
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            if hop == 5:
                                raise ImageDownloadError("Too many image redirects")
                            location = response.headers.get("Location")
                            if not location:
                                raise ImageDownloadError("Image redirect has no Location")
                            current = _validated_url(current.join(URL(location)))
                            continue
                        if response.status != 200:
                            raise ImageDownloadError(f"Image download HTTP {response.status}")
                        if (response.content_length is not None and
                                response.content_length > max_bytes):
                            raise ImageDownloadError("Image exceeds max_bytes")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if len(data) + len(chunk) > max_bytes:
                                raise ImageDownloadError("Image exceeds max_bytes")
                            data.extend(chunk)
                        result = bytes(data)
                        if not _is_image(result):
                            raise ImageDownloadError("Unsupported or invalid image signature")
                        return result
    except ImageDownloadError:
        raise
    except (aiohttp.ClientError, OSError, ValueError, TimeoutError) as exc:
        # Avoid echoing URL query tokens or credentials in user-facing errors.
        raise ImageDownloadError("Image download failed (network, URL, or timeout error)") from exc
    finally:
        await resolver.close()
