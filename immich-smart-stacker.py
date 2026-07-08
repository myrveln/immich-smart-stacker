#!/usr/bin/env python3
"""Immich Smart Stacker groups photos by temporal proximity and visual similarity."""

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any, Set
from dataclasses import dataclass
from pathlib import Path
import json

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: requests library required. Install with: pip install requests")
    sys.exit(1)

try:
    import imagehash
    from PIL import Image
    from io import BytesIO
except ImportError:  # pragma: no cover
    print("ERROR: PIL and imagehash libraries required. Install with: pip install Pillow imagehash")
    sys.exit(1)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


@dataclass
class Asset:
    """Represents an Immich asset (photo/video)."""
    id: str
    userId: str
    fileName: str
    fileCreatedAt: str  # ISO 8601 timestamp
    updatedAt: str
    type: str  # image or video
    stackId: Optional[str] = None

    @property
    def created_dt(self):
        """Parse fileCreatedAt as datetime."""
        return datetime.fromisoformat(self.fileCreatedAt.replace('Z', '+00:00'))

    def __repr__(self):
        return f"Asset({self.fileName}, {self.fileCreatedAt})"


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

        # Immich metadata search is POST in current versions.
        resp = self._request('POST', url, json=payload)
        if resp.status_code < 400:
            return resp

        # Immich 3.x rejects legacy pagination keys (e.g., skip/take) with HTTP 400.
        # Retry with strict DTO-compatible fields before giving up.
        if resp.status_code == 400 and ('skip' in payload or 'take' in payload):
            compact_payload = dict(payload)
            compact_payload.pop('skip', None)
            compact_payload.pop('take', None)
            retry = self._request('POST', url, json=compact_payload)
            if retry.status_code < 400:
                logger.debug("Metadata search succeeded after removing legacy skip/take fields")
                return retry

        # Some older deployments/proxies may only work with skip/take pagination.
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

        # Fallback compatibility for variants/proxies that expose GET.
        if resp.status_code in (404, 405):
            fallback = self._request('GET', url, params=payload)
            if fallback.status_code < 400:
                return fallback

        if resp.status_code == 400:
            # Immich v3 returns structured validation errors; surface them for troubleshooting.
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

        # Newer shape: {"assets": {"items": [...], "nextPage": 2}}
        if isinstance(assets_field, dict):
            items = assets_field.get('items', [])
            next_page = assets_field.get('nextPage')
            return items, next_page

        # Legacy/simple shape: {"assets": [...]}
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

        # Different versions/exposed DTOs may use different names.
        file_name = item.get('fileName') or item.get('originalFileName') or f"asset-{asset_id}"
        file_created_at = item.get('fileCreatedAt') or item.get('createdAt') or item.get('localDateTime')
        updated_at = item.get('updatedAt') or file_created_at
        owner_id = item.get('ownerId') or item.get('userId')
        asset_type = item.get('type') or item.get('assetType') or 'IMAGE'
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
        )

    def get_all_assets(self, user_id: str = None) -> List[Asset]:
        """Fetch all assets from Immich."""
        logger.info(f"Fetching assets from {self.api_url}...")
        assets = []
        page: Any = 1
        take = 250  # Pagination size

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

                # Filter by user if specified
                if user_id is None or asset.userId == user_id:
                    assets.append(asset)

            if skipped:
                logger.debug(f"Skipped {skipped} assets in current page due to missing required fields")

            if len(assets) % 1000 == 0 or len(batch) < take:
                logger.debug(f"Fetched {len(assets)} assets so far...")

            # Use API-provided pagination when available.
            if next_page is not None:
                next_page_int = self._to_int(next_page, default=0)
                if next_page_int > 0:
                    page = next_page_int
                    continue

                logger.debug(
                    f"Ignoring non-numeric nextPage={next_page!r}; falling back to sequential pagination"
                )

            # Legacy fallback: continue while full page is returned.
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

    def get_asset_thumbnail(self, asset_id: str) -> Optional[Image.Image]:
        """Download thumbnail for an asset."""
        self.last_thumbnail_status = None

        # Prefer preview for better hash quality, then fallback to thumbnail.
        for size in ('preview', 'thumbnail'):
            resp = self._request(
                'GET',
                f"{self.api_url}/assets/{asset_id}/thumbnail",
                params={'size': size}
            )
            self.last_thumbnail_status = resp.status_code

            # Asset media may not be generated/available yet (common for some videos).
            if resp.status_code == 404:
                continue

            # Common when metadata includes assets not accessible by this API key.
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

    def create_stack(self, primary_asset_id: str, stacked_asset_ids: List[str]) -> bool:
        """Create a stack with given assets."""
        if not stacked_asset_ids:
            return False

        # Immich create-stack uses assetIds where first item becomes primary.
        payload = {
            'assetIds': [primary_asset_id] + stacked_asset_ids
        }

        resp = self._request('POST', f"{self.api_url}/stacks", json=payload)

        if resp.status_code == 201:
            logger.info(f"Created stack: {primary_asset_id} + {len(stacked_asset_ids)} assets")
            return True
        else:
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

        for asset in assets:
            if asset.get('id') == primary_id:
                return asset.get('ownerId') or asset.get('userId')

        if assets:
            return assets[0].get('ownerId') or assets[0].get('userId')

        return None

    def get_stacks(self) -> List[Dict[str, Any]]:
        """Get detailed stack list with ids, asset ids, primary id and owner id."""
        resp = self._request('GET', f"{self.api_url}/stacks")
        resp.raise_for_status()

        stacks: List[Dict[str, Any]] = []
        for stack in resp.json():
            assets = stack.get('assets', []) or []
            asset_ids = [asset.get('id') for asset in assets if asset.get('id')]
            primary_id = stack.get('primaryAssetId')
            if primary_id and primary_id not in asset_ids:
                asset_ids.append(primary_id)
            stacks.append({
                'id': stack.get('id'),
                'primaryAssetId': primary_id,
                'assetIds': asset_ids,
                'ownerId': self._stack_owner_id(stack),
            })

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


class SmartStacker:
    """Groups assets by temporal proximity + visual similarity.

    TODO: Implement ML-grade similarity option (phash+dhash ensemble or embedding-based)
    as an optional mode (--similarity-method phash|dhash|embedding) while keeping
    current average-hash behavior as default. This would enable semantic understanding
    beyond pixel-level perceptual hashing.
    """

    def __init__(self, client: ImmichClient, temporal_window: float = 2.0,
                 hash_threshold: int = 8, dry_run: bool = False, include_videos: bool = False,
                 state_file: Optional[Path] = None, run_key: Optional[str] = None,
                 run_scope: Optional[str] = None):
        self.client = client
        self.temporal_window = timedelta(seconds=temporal_window)
        self.hash_threshold = hash_threshold
        self.dry_run = dry_run
        self.include_videos = include_videos
        self.state_file = state_file
        self.run_scope = run_scope if run_scope is not None else '__all_users__'
        self.run_key = run_key or self._build_run_key()
        self.hashes: Dict[str, str] = {}
        self.existing_stacks = client.get_existing_stacks()
        self.inaccessible_assets_count = 0
        self.inaccessible_assets_logged = 0
        self.inaccessible_by_user: Dict[str, int] = {}
        self.inaccessible_by_status: Dict[str, int] = {}
        self.seen_signatures: Set[str] = self._load_seen_signatures()
        self.seen_signatures.update(
            self._signature(stack_assets)
            for stack_assets in self.existing_stacks.values()
            if len(stack_assets) >= 2
        )

    def _build_run_key(self) -> str:
        """Create a stable key for the current run configuration."""
        material = (
            f"{self.client.api_url}|{self.temporal_window.total_seconds()}|{self.hash_threshold}|"
            f"{int(self.include_videos)}|scope:{self.run_scope}"
        )
        return hashlib.sha1(material.encode('utf-8')).hexdigest()

    def _record_unhashable_asset(self, asset: Asset, status_code: Optional[int]) -> None:
        """Track assets that cannot be hashed due to thumbnail availability/access."""
        self.inaccessible_assets_count += 1
        self.inaccessible_by_user[asset.userId] = self.inaccessible_by_user.get(asset.userId, 0) + 1

        status_key = str(status_code) if status_code is not None else 'unknown'
        self.inaccessible_by_status[status_key] = self.inaccessible_by_status.get(status_key, 0) + 1

    def _load_seen_signatures(self) -> Set[str]:
        """Load previously processed stack signatures for this configuration."""
        if self.state_file is None or not self.state_file.exists():
            return set()

        try:
            data = json.loads(self.state_file.read_text())
            seen = data.get('seen', {})
            signatures = seen.get(self.run_key, [])
            return set(signatures)
        except Exception:
            return set()

    def _save_seen_signatures(self) -> None:
        """Persist processed stack signatures for this configuration."""
        if self.state_file is None:
            return

        try:
            data = {}
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())

            seen = data.get('seen', {})
            seen[self.run_key] = sorted(self.seen_signatures)
            data['seen'] = seen
            self.state_file.write_text(json.dumps(data, indent=2, sort_keys=True))
        except Exception as exc:
            logger.debug(f"Could not persist stacker state: {exc}")

    def expand_with_existing_stacks(self, asset_ids: List[str]) -> List[str]:
        """Expand candidate group by unioning any intersecting existing stacks."""
        expanded = set(asset_ids)
        changed = True

        while changed:
            changed = False
            for stack_assets in self.existing_stacks.values():
                stack_set = set(stack_assets)
                if not stack_set:
                    continue
                if expanded.intersection(stack_set) and not stack_set.issubset(expanded):
                    expanded.update(stack_set)
                    changed = True

        return list(expanded)

    def compute_hash(self, asset: Asset) -> str:
        """Compute perceptual hash for an asset."""
        # Default mode targets still photos; videos are opt-in due to less reliable previews.
        if asset.type != 'image' and not self.include_videos:
            return None

        try:
            thumbnail = self.client.get_asset_thumbnail(asset.id)
            if thumbnail is None:
                self._record_unhashable_asset(asset, self.client.last_thumbnail_status)
                # Avoid flooding logs in large libraries.
                if self.inaccessible_assets_logged < 10:
                    logger.debug(f"Skipping hash for unhashable asset: {asset.fileName} ({asset.id})")
                    self.inaccessible_assets_logged += 1
                elif self.inaccessible_assets_logged == 10:
                    logger.debug("More unhashable assets detected; suppressing further per-asset logs")
                    self.inaccessible_assets_logged += 1
                return None
            # Use average hash (fast) combined with perception hash for robustness
            avg_hash = str(imagehash.average_hash(thumbnail, hash_size=8))
            return avg_hash
        except Exception as e:
            logger.warning(f"Failed to hash {asset.fileName}: {e}")
            return None

    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """Compute hamming distance between two hashes."""
        if hash1 is None or hash2 is None:
            return float('inf')
        return bin(int(hash1, 16) ^ int(hash2, 16)).count('1')

    def cluster_by_temporal_proximity(self, assets: List[Asset]) -> List[List[Asset]]:
        """Group assets by temporal proximity (iPhone bursts)."""
        # Sort by creation time
        sorted_assets = sorted(assets, key=lambda a: a.created_dt)

        clusters = []
        current_cluster = [sorted_assets[0]]

        for asset in sorted_assets[1:]:
            time_diff = asset.created_dt - current_cluster[-1].created_dt

            if time_diff <= self.temporal_window:
                current_cluster.append(asset)
            else:
                if len(current_cluster) > 1:
                    clusters.append(current_cluster)
                current_cluster = [asset]

        # Don't forget last cluster
        if len(current_cluster) > 1:
            clusters.append(current_cluster)

        logger.info(f"Found {len(clusters)} temporal clusters")
        return clusters

    def filter_by_visual_similarity(self, cluster: List[Asset],
                                    threshold: int = None) -> List[List[Asset]]:
        """Sub-cluster by visual similarity within a temporal cluster.

        Uses a graph model where each hashable asset is a node and edges connect
        assets within the hamming distance threshold. Groups are connected
        components, which removes order-dependence from the old greedy approach.
        """
        if threshold is None:
            threshold = self.hash_threshold

        # Compute hashes
        for asset in cluster:
            if asset.id not in self.hashes:
                self.hashes[asset.id] = self.compute_hash(asset)

        hashable_assets = [asset for asset in cluster if self.hashes.get(asset.id) is not None]
        if len(hashable_assets) < 2:
            return []

        adjacency: Dict[str, Set[str]] = {asset.id: set() for asset in hashable_assets}
        asset_by_id: Dict[str, Asset] = {asset.id: asset for asset in hashable_assets}
        cluster_order: Dict[str, int] = {asset.id: index for index, asset in enumerate(cluster)}

        for i, asset1 in enumerate(hashable_assets):
            hash1 = self.hashes[asset1.id]
            for asset2 in hashable_assets[i + 1:]:
                hash2 = self.hashes[asset2.id]
                if self.hamming_distance(hash1, hash2) <= threshold:
                    adjacency[asset1.id].add(asset2.id)
                    adjacency[asset2.id].add(asset1.id)

        groups: List[List[Asset]] = []
        visited: Set[str] = set()

        for asset in hashable_assets:
            if asset.id in visited:
                continue

            stack = [asset.id]
            component: Set[str] = set()

            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                stack.extend(neighbor for neighbor in adjacency[current] if neighbor not in visited)

            if len(component) > 1:
                groups.append(
                    sorted(
                        [asset_by_id[asset_id] for asset_id in component],
                        key=lambda item: cluster_order[item.id],
                    )
                )

        return groups

    def is_already_stacked(self, asset_ids: List[str]) -> bool:
        """Check if these assets are already in a stack together."""
        asset_set = set(asset_ids)
        for stack_assets in self.existing_stacks.values():
            if asset_set.issubset(set(stack_assets)):
                return True
        return False

    @staticmethod
    def _all_in_same_stack(assets: List[Asset]) -> bool:
        """True when every asset already belongs to the same existing stack."""
        stack_ids = {asset.stackId for asset in assets if asset.stackId}
        return len(stack_ids) == 1 and len(assets) >= 2

    @staticmethod
    def _signature(asset_ids: List[str]) -> str:
        """Build a stable signature for a candidate group."""
        return hashlib.sha1('|'.join(sorted(set(asset_ids))).encode('utf-8')).hexdigest()

    @staticmethod
    def _merge_overlapping_sets(groups: List[set]) -> List[set]:
        """Merge groups that share any asset id into disjoint sets."""
        if not groups:
            return []

        merged = [set(group) for group in groups if group]
        changed = True

        while changed:
            changed = False
            result = []

            while merged:
                base = merged.pop()
                i = 0
                while i < len(merged):
                    if base.intersection(merged[i]):
                        base.update(merged.pop(i))
                        changed = True
                    else:
                        i += 1
                result.append(base)

            merged = result

        return merged

    def _replace_overlapping_local_stacks(self, asset_ids: List[str], local_key: str) -> None:
        """Mirror server-side merge behavior in local cache for this run."""
        new_set = set(asset_ids)
        for stack_id in list(self.existing_stacks.keys()):
            stack_set = set(self.existing_stacks.get(stack_id, []))
            if stack_set.intersection(new_set):
                del self.existing_stacks[stack_id]

        self.existing_stacks[local_key] = list(asset_ids)

    def run(self, assets: List[Asset], user_filter: str = None) -> int:
        """Run the stacking algorithm."""
        # Filter by user if specified
        if user_filter:
            assets = [a for a in assets if a.userId == user_filter]
            logger.info(f"Filtered to user {user_filter}: {len(assets)} assets")

        if not assets:
            logger.info("No assets to process")
            return 0

        assets_by_id = {a.id: a for a in assets}

        # Cluster by temporal proximity
        temporal_clusters = self.cluster_by_temporal_proximity(assets)

        stacks_created = 0
        candidate_groups: List[set] = []

        for i, cluster in enumerate(temporal_clusters, 1):
            if i <= 5 or i % 100 == 0:
                logger.debug(f"Processing cluster {i}/{len(temporal_clusters)} ({len(cluster)} assets)")

            # Sub-cluster by visual similarity
            visual_groups = self.filter_by_visual_similarity(cluster)

            for group in visual_groups:
                if len(group) < 2:
                    continue

                group_asset_ids = [a.id for a in group]

                # Expand with any intersecting existing stacks so looser reruns can merge/extend stacks.
                expanded_ids = self.expand_with_existing_stacks(group_asset_ids)

                # Skip if final candidate is already fully in one existing stack.
                if self.is_already_stacked(expanded_ids):
                    logger.debug(f"Skipping already-stacked group: {[a.fileName for a in group]}")
                    continue

                expanded_assets = [assets_by_id[asset_id] for asset_id in expanded_ids if asset_id in assets_by_id]

                if len(expanded_assets) < 2:
                    logger.debug("Expanded group contains <2 resolvable assets in current dataset; skipping")
                    continue

                candidate_groups.append(set(a.id for a in expanded_assets))

        merged_candidate_groups = self._merge_overlapping_sets(candidate_groups)
        if candidate_groups:
            logger.info(
                f"Consolidated {len(candidate_groups)} candidate groups into "
                f"{len(merged_candidate_groups)} disjoint stack targets"
            )

        for merged_ids in merged_candidate_groups:
            expanded_ids = list(merged_ids)

            # Skip if final candidate is already fully in one existing stack.
            if self.is_already_stacked(expanded_ids):
                continue

            expanded_assets = [assets_by_id[asset_id] for asset_id in expanded_ids if asset_id in assets_by_id]

            if len(expanded_assets) < 2:
                continue

            # Idempotency guard: if all assets already point at the same stack id, do nothing.
            if self._all_in_same_stack(expanded_assets):
                logger.debug("Skipping group already in same stack based on stackId metadata")
                continue

            signature = self._signature(expanded_ids)
            if signature in self.seen_signatures:
                logger.debug("Skipping previously processed stack signature")
                continue

            # Sort by timestamp, first one is primary.
            expanded_assets.sort(key=lambda a: a.created_dt)
            primary = expanded_assets[0]
            children = [a.id for a in expanded_assets[1:]]

            logger.info(
                f"Merging/stacking: {primary.fileName} + {len(children)} similar photos "
                f"(temporal span: {(expanded_assets[-1].created_dt - expanded_assets[0].created_dt).total_seconds():.2f}s)"
            )

            if not self.dry_run:
                if self.client.create_stack(primary.id, children):
                    local_key = f"local-{primary.id}-{stacks_created}"
                    self._replace_overlapping_local_stacks([primary.id] + children, local_key)
                    self.seen_signatures.add(signature)
                    self._save_seen_signatures()
                    stacks_created += 1
            else:
                logger.info(f"[DRY RUN] Would create/merge stack")
                stacks_created += 1

        if self.inaccessible_assets_count:
            logger.warning(
                f"Skipped {self.inaccessible_assets_count} assets due to thumbnail access/unavailability."
            )
            top_users = sorted(self.inaccessible_by_user.items(), key=lambda kv: kv[1], reverse=True)[:5]
            top_users_text = ', '.join([f"{uid}:{count}" for uid, count in top_users])
            top_statuses = sorted(self.inaccessible_by_status.items(), key=lambda kv: kv[1], reverse=True)
            top_statuses_text = ', '.join([f"{code}:{count}" for code, count in top_statuses])
            logger.warning(
                "Inaccessible asset owner distribution (top 5): "
                f"{top_users_text}. Consider --user-filter <ownerId> to scope processing."
            )
            logger.warning(f"Unhashable thumbnail status distribution: {top_statuses_text}")
            if assets and (self.inaccessible_assets_count / len(assets)) > 0.2:
                logger.warning(
                    "High inaccessible ratio detected. Verify API key permissions include asset.view "
                    "(thumbnail endpoint) and asset.read."
                )

        logger.info(f"Stacks created: {stacks_created}")
        return stacks_created


def unstack_all(client: ImmichClient, dry_run: bool = False, user_filter: str = None) -> int:
    """Delete all stacks, optionally scoped to a specific owner user id."""
    stacks = client.get_stacks()

    if user_filter:
        stacks = [stack for stack in stacks if stack.get('ownerId') == user_filter]
        logger.info(f"Filtered stacks to user {user_filter}: {len(stacks)} stacks")
    else:
        logger.info(f"Unstack target scope: all users ({len(stacks)} stacks)")

    if not stacks:
        logger.info("No stacks to delete")
        return 0

    deleted = 0
    for stack in stacks:
        stack_id = stack.get('id')
        if not stack_id:
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would delete stack {stack_id} ({len(stack.get('assetIds', []))} assets)")
            deleted += 1
            continue

        if client.delete_stack(stack_id):
            deleted += 1

    logger.info(f"Stacks deleted: {deleted}")
    return deleted


def main():
    def env_bool(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}

    parser = argparse.ArgumentParser(
        description='Group Immich photos by temporal proximity + visual similarity'
    )
    parser.add_argument(
        '--api-url',
        default=os.getenv('IMMICH_API_URL'),
        required=os.getenv('IMMICH_API_URL') is None,
        help='Immich API URL (e.g., http://localhost:2283 or http://localhost:2283/api)'
    )
    parser.add_argument('--api-key', default=os.getenv('IMMICH_API_KEY'), required=os.getenv('IMMICH_API_KEY') is None, help='Immich API key')
    parser.add_argument('--user-filter', default=os.getenv('IMMICH_USER_FILTER'), help='Filter to specific user ID')
    parser.add_argument('--all-users', action='store_true',
                       default=env_bool('ALL_USERS', False),
                       help='Process all users returned by the API (default auto-filters to current user)')
    parser.add_argument('--temporal-window', type=float,
                       default=float(os.getenv('TEMPORAL_WINDOW', '2.0')),
                       help='Temporal window in seconds for burst detection (default: 2.0)')
    parser.add_argument('--hash-threshold', type=int,
                       default=int(os.getenv('HASH_THRESHOLD', '8')),
                       help='Hamming distance threshold for visual similarity (default: 8, lower=stricter)')
    parser.add_argument('--dry-run', action='store_true', default=env_bool('DRY_RUN', False), help='Preview stacks without creating them')
    parser.add_argument('--unstack-all', action='store_true',
                       default=env_bool('UNSTACK_ALL', False),
                       help='Delete all stacks (use --user-filter to scope to a specific user)')
    parser.add_argument('--include-videos', action='store_true',
                       default=env_bool('INCLUDE_VIDEOS', False),
                       help='Also attempt hashing for video assets (off by default)')
    parser.add_argument('--state-file', default=os.getenv('SMART_STACKER_STATE_FILE', str(Path(__file__).with_name('.immich-smart-stacker-state.json'))), help='Path to the local idempotency cache file')
    parser.add_argument('--verbose', action='store_true', default=env_bool('VERBOSE', False), help='Enable debug logging')

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
        # Keep third-party HTTP logs quieter; our own debug logs are enough.
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    if not args.api_key or not args.api_key.strip():
        logger.error(
            "--api-key is empty. Provide a valid Immich API key with asset.view, asset.read, and stack:* permissions."
        )
        return 1

    try:
        # Initialize client
        client = ImmichClient(args.api_url, args.api_key)

        # Unstack mode: by request, omit user-filter means all users (no auto-filter here).
        if args.unstack_all:
            deleted = unstack_all(
                client,
                dry_run=args.dry_run,
                user_filter=args.user_filter,
            )
            logger.info(f"Completed. Deleted {deleted} stacks.")
            return 0

        # By default, avoid cross-user 403 noise by auto-filtering to authenticated user.
        effective_user_filter = args.user_filter
        if not effective_user_filter and not args.all_users:
            effective_user_filter = client.get_current_user_id()
            if effective_user_filter:
                logger.info(f"Auto-filtering to current user id: {effective_user_filter}")
            else:
                logger.info("Could not determine current user id; processing all returned assets")

        # Fetch assets
        assets = client.get_all_assets()

        if not assets:
            logger.warning("No assets found")
            return 1

        # Run stacking algorithm
        stacker = SmartStacker(
            client,
            temporal_window=args.temporal_window,
            hash_threshold=args.hash_threshold,
            dry_run=args.dry_run,
            include_videos=args.include_videos,
            state_file=Path(args.state_file),
            run_scope=effective_user_filter if effective_user_filter else '__all_users__',
        )

        stacks_created = stacker.run(assets, user_filter=effective_user_filter)

        logger.info(f"Completed. Created {stacks_created} stacks.")
        return 0

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
