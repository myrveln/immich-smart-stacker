import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import imagehash

from .client import ImmichClient
from .logging_config import logger
from .models import Asset


class SmartStacker:
    """Groups assets by temporal proximity + visual similarity."""

    def __init__(
        self,
        client: ImmichClient,
        temporal_window: float = 2.0,
        hash_threshold: int = 8,
        dry_run: bool = False,
        include_videos: bool = False,
        state_file: Optional[Path] = None,
        run_key: Optional[str] = None,
        run_scope: Optional[str] = None,
        video_skip_preview_404: bool = True,
        video_frame_fallback: bool = False,
        video_frame_fallback_timeout: float = 10.0,
    ):
        self.client = client
        self.temporal_window = timedelta(seconds=temporal_window)
        self.hash_threshold = hash_threshold
        self.dry_run = dry_run
        self.include_videos = include_videos
        self.state_file = state_file
        self.run_scope = run_scope if run_scope is not None else '__all_users__'
        self.video_skip_preview_404 = video_skip_preview_404
        self.video_frame_fallback = video_frame_fallback
        self.video_frame_fallback_timeout = video_frame_fallback_timeout
        self.run_key = run_key or self._build_run_key()
        self.hashes: Dict[str, str] = {}
        self.existing_stacks = client.get_existing_stacks()
        self.inaccessible_assets_count = 0
        self.inaccessible_assets_logged = 0
        self.inaccessible_by_user: Dict[str, int] = {}
        self.inaccessible_by_status: Dict[str, int] = {}
        self.video_events: Dict[str, int] = {}
        self.seen_signatures: Set[str] = self._load_seen_signatures()
        self.seen_signatures.update(
            self._signature(stack_assets)
            for stack_assets in self.existing_stacks.values()
            if len(stack_assets) >= 2
        )

    def _build_run_key(self) -> str:
        material = (
            f"{self.client.api_url}|{self.temporal_window.total_seconds()}|{self.hash_threshold}|"
            f"{int(self.include_videos)}|scope:{self.run_scope}"
        )
        return hashlib.sha1(material.encode('utf-8')).hexdigest()

    def _record_unhashable_asset(self, asset: Asset, status_code: Optional[int]) -> None:
        self.inaccessible_assets_count += 1
        self.inaccessible_by_user[asset.userId] = self.inaccessible_by_user.get(asset.userId, 0) + 1

        status_key = str(status_code) if status_code is not None else 'unknown'
        self.inaccessible_by_status[status_key] = self.inaccessible_by_status.get(status_key, 0) + 1

    def _record_video_event(self, reason: str) -> None:
        self.video_events[reason] = self.video_events.get(reason, 0) + 1

    def _load_seen_signatures(self) -> Set[str]:
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

    def _augment_existing_stacks_from_assets(self, assets: List[Asset]) -> None:
        grouped: Dict[str, List[str]] = {}
        for asset in assets:
            if asset.stackId:
                grouped.setdefault(asset.stackId, []).append(asset.id)

        for stack_id, asset_ids in grouped.items():
            unique_ids = list(dict.fromkeys(asset_ids))
            if len(unique_ids) >= 2:
                self.existing_stacks.setdefault(stack_id, unique_ids)

    def compute_hash(self, asset: Asset) -> str:
        if asset.type != 'image' and not self.include_videos:
            return None

        try:
            thumbnail = self.client.get_asset_thumbnail(
                asset.id,
                asset_type=asset.type,
                skip_video_preview_404=self.video_skip_preview_404,
            )

            if thumbnail is None and asset.type == 'video' and self.video_frame_fallback:
                frame, reason = self.client.get_video_frame_from_playback(
                    asset.id,
                    ffmpeg_timeout=self.video_frame_fallback_timeout,
                )
                self._record_video_event(reason)
                if frame is not None:
                    self._record_video_event('frame-fallback-used')
                    thumbnail = frame

            if thumbnail is None:
                if asset.type == 'video':
                    status = self.client.last_thumbnail_status
                    if status == 404 and self.video_skip_preview_404:
                        self._record_video_event('preview-unsupported')
                    elif status in (401, 403):
                        self._record_video_event('thumbnail-access-denied')
                    elif status is not None:
                        self._record_video_event(f'thumbnail-http-{status}')
                    else:
                        self._record_video_event('thumbnail-unknown')

                self._record_unhashable_asset(asset, self.client.last_thumbnail_status)
                if self.inaccessible_assets_logged < 10:
                    logger.debug(f"Skipping hash for unhashable asset: {asset.fileName} ({asset.id})")
                    self.inaccessible_assets_logged += 1
                elif self.inaccessible_assets_logged == 10:
                    logger.debug("More unhashable assets detected; suppressing further per-asset logs")
                    self.inaccessible_assets_logged += 1
                return None

            avg_hash = str(imagehash.average_hash(thumbnail, hash_size=8))
            return avg_hash
        except Exception as e:
            logger.warning(f"Failed to hash {asset.fileName}: {e}")
            return None

    @staticmethod
    def _resolution_score(asset: Asset) -> int:
        width = asset.width or 0
        height = asset.height or 0
        return width * height

    @classmethod
    def select_primary_asset(cls, assets: List[Asset]) -> Asset:
        if not assets:
            raise ValueError("Cannot select a primary asset from an empty list")

        return sorted(
            assets,
            key=lambda asset: (
                not bool(asset.isFavorite),
                -cls._resolution_score(asset),
                asset.created_dt,
                asset.id,
            ),
        )[0]

    def hamming_distance(self, hash1: str, hash2: str) -> int:
        if hash1 is None or hash2 is None:
            return float('inf')
        return bin(int(hash1, 16) ^ int(hash2, 16)).count('1')

    def cluster_by_temporal_proximity(self, assets: List[Asset]) -> List[List[Asset]]:
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

        if len(current_cluster) > 1:
            clusters.append(current_cluster)

        logger.info(f"Found {len(clusters)} temporal clusters")
        return clusters

    def filter_by_visual_similarity(self, cluster: List[Asset], threshold: int = None) -> List[List[Asset]]:
        if threshold is None:
            threshold = self.hash_threshold

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
        asset_set = set(asset_ids)
        for stack_assets in self.existing_stacks.values():
            if asset_set.issubset(set(stack_assets)):
                return True
        return False

    @staticmethod
    def _all_in_same_stack(assets: List[Asset]) -> bool:
        stack_ids = {asset.stackId for asset in assets if asset.stackId}
        return len(stack_ids) == 1 and len(assets) >= 2

    @staticmethod
    def _signature(asset_ids: List[str]) -> str:
        return hashlib.sha1('|'.join(sorted(set(asset_ids))).encode('utf-8')).hexdigest()

    @staticmethod
    def _merge_overlapping_sets(groups: List[set]) -> List[set]:
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
        new_set = set(asset_ids)
        for stack_id in list(self.existing_stacks.keys()):
            stack_set = set(self.existing_stacks.get(stack_id, []))
            if stack_set.intersection(new_set):
                del self.existing_stacks[stack_id]

        self.existing_stacks[local_key] = list(asset_ids)

    def run(self, assets: List[Asset], user_filter: str = None) -> int:
        if user_filter:
            assets = [a for a in assets if a.userId == user_filter]
            logger.info(f"Filtered to user {user_filter}: {len(assets)} assets")

        if not assets:
            logger.info("No assets to process")
            return 0

        self._augment_existing_stacks_from_assets(assets)
        assets_by_id = {a.id: a for a in assets}

        temporal_clusters = self.cluster_by_temporal_proximity(assets)

        stacks_created = 0
        candidate_groups: List[set] = []

        for i, cluster in enumerate(temporal_clusters, 1):
            if i <= 5 or i % 100 == 0:
                logger.debug(f"Processing cluster {i}/{len(temporal_clusters)} ({len(cluster)} assets)")

            visual_groups = self.filter_by_visual_similarity(cluster)

            for group in visual_groups:
                if len(group) < 2:
                    continue

                group_asset_ids = [a.id for a in group]
                expanded_ids = self.expand_with_existing_stacks(group_asset_ids)

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

            if self.is_already_stacked(expanded_ids):
                continue

            expanded_assets = [assets_by_id[asset_id] for asset_id in expanded_ids if asset_id in assets_by_id]

            if len(expanded_assets) < 2:
                continue

            if self._all_in_same_stack(expanded_assets):
                logger.debug("Skipping group already in same stack based on stackId metadata")
                continue

            signature = self._signature(expanded_ids)
            if signature in self.seen_signatures:
                logger.debug("Skipping previously processed stack signature")
                continue

            expanded_assets.sort(key=lambda a: (a.created_dt, a.id))
            primary = self.select_primary_asset(expanded_assets)
            children = [a.id for a in expanded_assets if a.id != primary.id]

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
                logger.info("[DRY RUN] Would create/merge stack")
                stacks_created += 1

        if self.inaccessible_assets_count:
            logger.warning(
                f"Skipped {self.inaccessible_assets_count} assets due to thumbnail access/unavailability."
            )
            top_users = sorted(self.inaccessible_by_user.items(), key=lambda kv: kv[1], reverse=True)[:5]
            top_users_text = ', '.join([f"{uid}:{count}" for uid, count in top_users])
            top_statuses = sorted(self.inaccessible_by_status.items(), key=lambda kv: kv[1], reverse=True)
            top_statuses_text = ', '.join([f"{code}:{count}" for code, count in top_statuses])
            owner_warning = f"Inaccessible asset owner distribution (top 5): {top_users_text}."
            if not user_filter:
                owner_warning += " Consider --user-filter <ownerId> to scope processing."
            logger.warning(owner_warning)
            logger.warning(f"Unhashable thumbnail status distribution: {top_statuses_text}")
            if self.video_events:
                video_events_text = ', '.join(
                    [
                        f"{reason}:{count}"
                        for reason, count in sorted(self.video_events.items(), key=lambda kv: kv[1], reverse=True)
                    ]
                )
                logger.warning(f"Video handling events: {video_events_text}")
            if assets and (self.inaccessible_assets_count / len(assets)) > 0.2:
                logger.warning(
                    "High inaccessible ratio detected. Verify API key permissions include asset.view "
                    "(thumbnail endpoint) and asset.read."
                )

        logger.info(f"Stacks created: {stacks_created}")
        return stacks_created
