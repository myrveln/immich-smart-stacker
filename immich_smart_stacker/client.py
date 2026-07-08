import shutil
import subprocess
import time
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image

from .logging_config import logger
from .models import Asset


class ImmichClient:
    """Minimal Immich API client."""

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_url: str,
        api_key: str,
        request_timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
    ):
        self.api_url = self._normalize_api_url(api_url)
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({'x-api-key': api_key})
        self.request_timeout = request_timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._me_user_id: Optional[str] = None
        self._thumbnail_permission_warning_emitted = False
        self.last_thumbnail_status: Optional[int] = None

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue HTTP request with timeout, retries and exponential backoff."""
        attempts = self.max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(method, url, timeout=self.request_timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise
                delay = self.retry_backoff * (2 ** (attempt - 1))
                logger.debug(
                    f"HTTP request error on {method} {url}: {exc}. "
                    f"Retrying in {delay:.2f}s ({attempt}/{attempts})"
                )
                if delay > 0:
                    time.sleep(delay)
                continue

            if response.status_code in self.RETRYABLE_STATUS_CODES and attempt < attempts:
                retry_after = response.headers.get('Retry-After')
                if retry_after and str(retry_after).isdigit():
                    delay = float(retry_after)
                else:
                    delay = self.retry_backoff * (2 ** (attempt - 1))

                logger.debug(
                    f"HTTP {response.status_code} on {method} {url}. "
                    f"Retrying in {delay:.2f}s ({attempt}/{attempts})"
                )
                if delay > 0:
                    time.sleep(delay)
                continue

            return response

        raise RuntimeError(f"Unreachable retry loop for {method} {url}")

    @staticmethod
    def _normalize_api_url(api_url: str) -> str:
        """Normalize API URL so both host root and /api suffix work."""
        normalized = api_url.rstrip('/')
        if normalized.endswith('/api'):
            return normalized
        return f"{normalized}/api"

    def _search_metadata(self, payload: Dict) -> requests.Response:
        """Search metadata using adaptive payloads across Immich versions."""
        url = f"{self.api_url}/search/metadata"

        resp = self._request('POST', url, json=payload)
        if resp.status_code < 400:
            return resp

        if resp.status_code == 400 and ('skip' in payload or 'take' in payload):
            compact_payload = dict(payload)
            compact_payload.pop('skip', None)
            compact_payload.pop('take', None)
            retry = self._request('POST', url, json=compact_payload)
            if retry.status_code < 400:
                logger.debug("Metadata search succeeded after removing legacy skip/take fields")
                return retry

        if resp.status_code == 400 and 'page' in payload and 'size' in payload and 'skip' not in payload and 'take' not in payload:
            page_for_math = self._to_int(payload.get('page'), default=1)
            take = self._to_int(payload.get('size'), default=250)
            legacy_payload = dict(payload)
            legacy_payload['take'] = take
            legacy_payload['skip'] = (max(page_for_math, 1) - 1) * take
            retry = self._request('POST', url, json=legacy_payload)
            if retry.status_code < 400:
                logger.debug("Metadata search succeeded after adding legacy skip/take fields")
                return retry

        if resp.status_code in (404, 405):
            fallback = self._request('GET', url, params=payload)
            if fallback.status_code < 400:
                return fallback

        if resp.status_code == 400:
            try:
                body = resp.json()
                message = body.get('message')
                errors = body.get('errors')
                if errors:
                    logger.error(f"Metadata search validation failed: {errors}")
                else:
                    logger.error(f"Metadata search failed (400): {message or body}")
            except Exception:
                logger.error(f"Metadata search failed (400): {resp.text}")

        resp.raise_for_status()
        return resp

    @staticmethod
    def _extract_assets_page(response_json: Dict) -> Tuple[List[Dict], Optional[Any]]:
        """Return (items, next_page) from different Immich response shapes."""
        assets_field = response_json.get('assets', [])

        if isinstance(assets_field, dict):
            items = assets_field.get('items', [])
            next_page = assets_field.get('nextPage')
            return items, next_page

        if isinstance(assets_field, list):
            return assets_field, None

        return [], None

    @staticmethod
    def _to_int(value: Any, default: int = 1) -> int:
        """Best-effort conversion to int for pagination math."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_asset(item: Dict[str, Any]) -> Optional[Asset]:
        """Build Asset with field fallbacks for Immich schema differences."""
        asset_id = item.get('id')
        if not asset_id:
            return None

        file_name = item.get('fileName') or item.get('originalFileName') or f"asset-{asset_id}"
        file_created_at = item.get('fileCreatedAt') or item.get('createdAt') or item.get('localDateTime')
        updated_at = item.get('updatedAt') or file_created_at
        owner_id = item.get('ownerId') or item.get('userId')
        asset_type = item.get('type') or item.get('assetType') or 'IMAGE'
        width = ImmichClient._to_int(
            item.get('exifInfo', {}).get('exifImageWidth')
            if isinstance(item.get('exifInfo'), dict)
            else None,
            default=0,
        )
        if width <= 0:
            width = ImmichClient._to_int(item.get('exifImageWidth'), default=0)
        if width <= 0:
            width = ImmichClient._to_int(item.get('width'), default=0)

        height = ImmichClient._to_int(
            item.get('exifInfo', {}).get('exifImageHeight')
            if isinstance(item.get('exifInfo'), dict)
            else None,
            default=0,
        )
        if height <= 0:
            height = ImmichClient._to_int(item.get('exifImageHeight'), default=0)
        if height <= 0:
            height = ImmichClient._to_int(item.get('height'), default=0)

        is_favorite_raw = item.get('isFavorite')
        is_favorite = bool(is_favorite_raw) if is_favorite_raw is not None else False
        stack_id = item.get('stackId')
        if stack_id is None and isinstance(item.get('stack'), dict):
            stack_id = item['stack'].get('id')

        if not file_created_at:
            return None

        return Asset(
            id=asset_id,
            userId=owner_id,
            fileName=file_name,
            fileCreatedAt=file_created_at,
            updatedAt=updated_at,
            type=str(asset_type).lower(),
            stackId=stack_id,
            width=width or None,
            height=height or None,
            isFavorite=is_favorite,
        )

    def get_all_assets(self, user_id: str = None) -> List[Asset]:
        """Fetch all assets from Immich."""
        logger.info(f"Fetching assets from {self.api_url}...")
        assets = []
        page: Any = 1
        take = 250

        while True:
            payload = {
                'page': page,
                'size': take,
            }
            resp = self._search_metadata(payload)
            response_json = resp.json()
            batch, next_page = self._extract_assets_page(response_json)
            if not batch:
                break

            skipped = 0
            for item in batch:
                asset = self._to_asset(item)
                if asset is None:
                    skipped += 1
                    logger.debug(f"Skipping unparsable asset payload keys: {sorted(item.keys())}")
                    continue

                if user_id is None or asset.userId == user_id:
                    assets.append(asset)

            if skipped:
                logger.debug(f"Skipped {skipped} assets in current page due to missing required fields")

            if len(assets) % 1000 == 0 or len(batch) < take:
                logger.debug(f"Fetched {len(assets)} assets so far...")

            if next_page is not None:
                next_page_int = self._to_int(next_page, default=0)
                if next_page_int > 0:
                    page = next_page_int
                    continue

                logger.debug(
                    f"Ignoring non-numeric nextPage={next_page!r}; falling back to sequential pagination"
                )

            if len(batch) < take:
                break

            page_for_math = self._to_int(page, default=1)
            page = page_for_math + 1

        logger.info(f"Total assets loaded: {len(assets)}")
        return assets

    def get_current_user_id(self) -> Optional[str]:
        """Fetch authenticated user id for default filtering behavior."""
        if self._me_user_id is not None:
            return self._me_user_id

        try:
            resp = self._request('GET', f"{self.api_url}/users/me")
            resp.raise_for_status()
            body = resp.json()
            self._me_user_id = body.get('id')
            return self._me_user_id
        except Exception as exc:
            logger.debug(f"Could not resolve current user id from /users/me: {exc}")
            return None

    def get_asset_thumbnail(
        self,
        asset_id: str,
        asset_type: Optional[str] = None,
        skip_video_preview_404: bool = True,
    ) -> Optional[Image.Image]:
        """Download thumbnail for an asset."""
        self.last_thumbnail_status = None

        for size in ('preview', 'thumbnail'):
            resp = self._request(
                'GET',
                f"{self.api_url}/assets/{asset_id}/thumbnail",
                params={'size': size},
            )
            self.last_thumbnail_status = resp.status_code

            if resp.status_code == 404:
                if asset_type == 'video' and size == 'preview' and skip_video_preview_404:
                    logger.debug(
                        f"Video preview not available for {asset_id}; skipping thumbnail fallback request"
                    )
                    return None
                continue

            if resp.status_code in (401, 403):
                if not self._thumbnail_permission_warning_emitted:
                    try:
                        body = resp.json()
                        message = body.get('message', '')
                        if 'asset.view' in message:
                            logger.warning(
                                "Immich reports missing 'asset.view' permission for thumbnails. "
                                "Add 'asset.view' to this API key."
                            )
                        else:
                            logger.warning(f"Thumbnail access denied: {message or resp.status_code}")
                    except Exception:
                        logger.warning(f"Thumbnail access denied with status {resp.status_code}")
                    self._thumbnail_permission_warning_emitted = True
                return None

            if resp.status_code < 400:
                return Image.open(BytesIO(resp.content)).convert('RGB')

            resp.raise_for_status()
            return None

        logger.debug(f"Thumbnail not found for asset {asset_id}; skipping hash")
        return None

    def get_video_frame_from_playback(
        self,
        asset_id: str,
        ffmpeg_timeout: float = 10.0,
    ) -> Tuple[Optional[Image.Image], str]:
        """Attempt to extract a frame from the video playback endpoint via ffmpeg."""
        ffmpeg_bin = shutil.which('ffmpeg')
        if not ffmpeg_bin:
            return None, 'ffmpeg-unavailable'

        playback_url = f"{self.api_url}/assets/{asset_id}/video/playback"
        command = [
            ffmpeg_bin,
            '-loglevel',
            'error',
            '-headers',
            f"x-api-key: {self.api_key}\r\n",
            '-i',
            playback_url,
            '-frames:v',
            '1',
            '-f',
            'image2pipe',
            '-vcodec',
            'png',
            'pipe:1',
        ]

        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=ffmpeg_timeout,
            )
        except subprocess.TimeoutExpired:
            return None, 'ffmpeg-timeout'
        except Exception:
            return None, 'ffmpeg-error'

        if proc.returncode != 0 or not proc.stdout:
            return None, 'ffmpeg-no-frame'

        try:
            image = Image.open(BytesIO(proc.stdout)).convert('RGB')
            return image, 'ffmpeg-frame'
        except Exception:
            return None, 'ffmpeg-decode-error'

    def create_stack(self, primary_asset_id: str, stacked_asset_ids: List[str]) -> bool:
        """Create a stack with given assets."""
        if not stacked_asset_ids:
            return False

        payload = {
            'assetIds': [primary_asset_id] + stacked_asset_ids,
        }

        resp = self._request('POST', f"{self.api_url}/stacks", json=payload)

        if resp.status_code == 201:
            logger.info(f"Created stack: {primary_asset_id} + {len(stacked_asset_ids)} assets")
            return True

        logger.warning(f"Failed to create stack: {resp.status_code} {resp.text}")
        return False

    def delete_stack(self, stack_id: str) -> bool:
        """Delete a single stack by id."""
        resp = self._request('DELETE', f"{self.api_url}/stacks/{stack_id}")

        if resp.status_code == 204:
            return True

        logger.warning(f"Failed to delete stack {stack_id}: {resp.status_code} {resp.text}")
        return False

    @staticmethod
    def _stack_owner_id(stack_payload: Dict[str, Any]) -> Optional[str]:
        """Best-effort owner id extraction from stack response payload."""
        assets = stack_payload.get('assets', []) or []
        primary_id = stack_payload.get('primaryAssetId')
        primary_asset = stack_payload.get('primaryAsset') if isinstance(stack_payload.get('primaryAsset'), dict) else None

        if primary_asset:
            owner_id = primary_asset.get('ownerId') or primary_asset.get('userId')
            if owner_id:
                return owner_id

        for asset in assets:
            if asset.get('id') == primary_id:
                return asset.get('ownerId') or asset.get('userId')

        if assets:
            return assets[0].get('ownerId') or assets[0].get('userId')

        return None

    @staticmethod
    def _stack_asset_ids(stack_payload: Dict[str, Any]) -> List[str]:
        """Best-effort asset id extraction from varying Immich stack response shapes."""
        asset_ids: List[str] = []

        assets = stack_payload.get('assets', []) or []
        asset_ids.extend(asset.get('id') for asset in assets if isinstance(asset, dict) and asset.get('id'))

        primary_id = stack_payload.get('primaryAssetId')
        if primary_id:
            asset_ids.append(primary_id)

        primary_asset = stack_payload.get('primaryAsset')
        if isinstance(primary_asset, dict) and primary_asset.get('id'):
            asset_ids.append(primary_asset.get('id'))

        direct_asset_ids = stack_payload.get('assetIds')
        if isinstance(direct_asset_ids, list):
            asset_ids.extend(asset_id for asset_id in direct_asset_ids if asset_id)

        secondary_asset_ids = stack_payload.get('secondaryAssetIds')
        if isinstance(secondary_asset_ids, list):
            asset_ids.extend(asset_id for asset_id in secondary_asset_ids if asset_id)

        return list(dict.fromkeys(asset_ids))

    def get_stacks(self) -> List[Dict[str, Any]]:
        """Get detailed stack list with ids, asset ids, primary id and owner id."""
        resp = self._request('GET', f"{self.api_url}/stacks")
        resp.raise_for_status()

        stacks: List[Dict[str, Any]] = []
        for stack in resp.json():
            asset_ids = self._stack_asset_ids(stack)
            primary_id = stack.get('primaryAssetId')
            if primary_id is None and isinstance(stack.get('primaryAsset'), dict):
                primary_id = stack['primaryAsset'].get('id')
            stacks.append(
                {
                    'id': stack.get('id'),
                    'primaryAssetId': primary_id,
                    'assetIds': asset_ids,
                    'ownerId': self._stack_owner_id(stack),
                }
            )

        return stacks

    def get_existing_stacks(self) -> Dict[str, List[str]]:
        """Get existing stacks to avoid duplicates."""
        stacks = {}
        for stack in self.get_stacks():
            stack_id = stack.get('id')
            if not stack_id:
                continue
            stacks[stack_id] = stack.get('assetIds', [])

        logger.info(f"Found {len(stacks)} existing stacks")
        return stacks
