import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Type

from .client import ImmichClient
from .logging_config import logger
from .operations import unstack_all
from .stacker import SmartStacker


def _parse_datetime_arg(value: str, name: str) -> datetime:
    """Parse datetime argument and return timezone-aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(
            f"Invalid {name} value '{value}'. Use ISO-8601, e.g. 2026-07-08T12:34:56Z"
        ) from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _format_datetime_utc(value: Optional[datetime]) -> Optional[str]:
    """Return ISO-8601 UTC string with Z suffix for JSON output/state."""
    if value is None:
        return None

    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _load_state_json(state_file: Path) -> Dict[str, Any]:
    """Load state file content as JSON dict, tolerating missing/invalid files."""
    if not state_file.exists():
        return {}

    try:
        data = json.loads(state_file.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state_json(state_file: Path, data: Dict[str, Any]) -> None:
    """Persist state JSON to disk."""
    state_file.write_text(json.dumps(data, indent=2, sort_keys=True))


def _load_watermark(state_file: Path, key: str) -> Optional[datetime]:
    """Load persisted watermark for a run key from state file."""
    data = _load_state_json(state_file)
    raw = data.get('watermarks', {}).get(key)
    if not raw:
        return None

    try:
        return _parse_datetime_arg(str(raw), 'watermark')
    except ValueError:
        return None


def _save_watermark(state_file: Path, key: str, value: datetime) -> None:
    """Save watermark for a run key into state file."""
    data = _load_state_json(state_file)
    watermarks = data.get('watermarks', {})
    watermarks[key] = _format_datetime_utc(value)
    data['watermarks'] = watermarks
    _save_state_json(state_file, data)


def _emit_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_args(argv: Optional[list[str]] = None):
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
        help='Immich API URL (e.g., http://localhost:2283 or http://localhost:2283/api)',
    )
    parser.add_argument(
        '--api-key',
        default=os.getenv('IMMICH_API_KEY'),
        required=os.getenv('IMMICH_API_KEY') is None,
        help='Immich API key',
    )
    parser.add_argument('--user-filter', default=os.getenv('IMMICH_USER_FILTER'), help='Filter to specific user ID')
    parser.add_argument(
        '--all-users',
        action='store_true',
        default=env_bool('ALL_USERS', False),
        help='Process all users returned by the API (default auto-filters to current user)',
    )
    parser.add_argument(
        '--temporal-window',
        type=float,
        default=float(os.getenv('TEMPORAL_WINDOW', '2.0')),
        help='Temporal window in seconds for burst detection (default: 2.0)',
    )
    parser.add_argument(
        '--since',
        default=os.getenv('SINCE'),
        help='Only process assets created at/after this ISO-8601 timestamp',
    )
    parser.add_argument(
        '--until',
        default=os.getenv('UNTIL'),
        help='Only process assets created at/before this ISO-8601 timestamp',
    )
    parser.add_argument(
        '--last-n-days',
        type=float,
        default=float(os.getenv('LAST_N_DAYS')) if os.getenv('LAST_N_DAYS') is not None else None,
        help='Only process assets from the last N days (overrides --since)',
    )
    parser.add_argument(
        '--use-watermark',
        action='store_true',
        default=env_bool('USE_WATERMARK', False),
        help='Load/save last-successful timestamp watermark in state file for incremental runs',
    )
    parser.add_argument(
        '--hash-threshold',
        type=int,
        default=int(os.getenv('HASH_THRESHOLD', '8')),
        help='Hamming distance threshold for visual similarity (default: 8, lower=stricter)',
    )
    parser.add_argument('--dry-run', action='store_true', default=env_bool('DRY_RUN', False), help='Preview stacks without creating them')
    parser.add_argument(
        '--unstack-all',
        action='store_true',
        default=env_bool('UNSTACK_ALL', False),
        help='Delete all stacks (use --user-filter to scope to a specific user)',
    )
    parser.add_argument(
        '--include-videos',
        action='store_true',
        default=env_bool('INCLUDE_VIDEOS', False),
        help='Also attempt hashing for video assets (off by default)',
    )
    parser.add_argument(
        '--video-frame-fallback',
        action='store_true',
        default=env_bool('VIDEO_FRAME_FALLBACK', False),
        help='When video thumbnail hashing fails, try extracting a frame via ffmpeg playback endpoint',
    )
    parser.add_argument(
        '--video-skip-preview',
        dest='video_skip_preview',
        action='store_true',
        default=env_bool('VIDEO_SKIP_PREVIEW', True),
        help='For videos, skip thumbnail fallback request after preview 404 (default: enabled)',
    )
    parser.add_argument(
        '--no-video-skip-preview',
        dest='video_skip_preview',
        action='store_false',
        help='For videos, allow thumbnail fallback request even when preview returns 404',
    )
    parser.add_argument(
        '--video-frame-fallback-timeout',
        type=float,
        default=float(os.getenv('VIDEO_FRAME_FALLBACK_TIMEOUT', '10.0')),
        help='Timeout in seconds for ffmpeg frame extraction fallback (default: 10.0)',
    )
    parser.add_argument(
        '--state-file',
        default=os.getenv('SMART_STACKER_STATE_FILE', str(Path(__file__).resolve().parent.parent / '.immich-smart-stacker-state.json')),
        help='Path to the local idempotency cache file',
    )
    parser.add_argument(
        '--interval-seconds',
        type=float,
        default=float(os.getenv('INTERVAL_SECONDS', '0')),
        help='Run repeatedly with this sleep interval in seconds (0 runs once)',
    )
    parser.add_argument(
        '--max-runs',
        type=int,
        default=int(os.getenv('MAX_RUNS')) if os.getenv('MAX_RUNS') is not None else None,
        help='Optional limit on run iterations when --interval-seconds is enabled',
    )
    parser.add_argument(
        '--output-json',
        action='store_true',
        default=env_bool('OUTPUT_JSON', False),
        help='Emit machine-readable JSON summary to stdout',
    )
    parser.add_argument('--verbose', action='store_true', default=env_bool('VERBOSE', False), help='Enable debug logging')

    return parser.parse_args(argv)


def _run_once(
    args,
    immich_client_cls: Type[ImmichClient],
    stacker_cls: Type[SmartStacker],
    unstack_fn: Callable,
):
    if not args.api_key or not args.api_key.strip():
        logger.error(
            "--api-key is empty. Provide a valid Immich API key with asset.view, asset.read, and stack:* permissions."
        )
        return 1

    if args.interval_seconds < 0:
        logger.error('--interval-seconds must be >= 0')
        return 1

    if args.max_runs is not None and args.max_runs <= 0:
        logger.error('--max-runs must be >= 1 when provided')
        return 1

    if args.last_n_days is not None and args.last_n_days < 0:
        logger.error('--last-n-days must be >= 0 when provided')
        return 1

    try:
        until_dt = _parse_datetime_arg(args.until, '--until') if args.until else None
        since_dt = _parse_datetime_arg(args.since, '--since') if args.since else None
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    if args.last_n_days is not None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.last_n_days)

    if since_dt and until_dt and since_dt > until_dt:
        logger.error('--since must be <= --until')
        return 1

    try:
        client = immich_client_cls(args.api_url, args.api_key)

        if args.unstack_all:
            deleted = unstack_fn(
                client,
                dry_run=args.dry_run,
                user_filter=args.user_filter,
            )
            logger.info(f"Completed. Deleted {deleted} stacks.")
            return 0

        effective_user_filter = args.user_filter
        if not effective_user_filter and not args.all_users:
            effective_user_filter = client.get_current_user_id()
            if effective_user_filter:
                logger.info(f"Auto-filtering to current user id: {effective_user_filter}")
            else:
                logger.info('Could not determine current user id; processing all returned assets')

        assets = client.get_all_assets()

        if not assets:
            logger.warning('No assets found')
            return 1

        state_file = Path(args.state_file)
        watermark_loaded: Optional[str] = None
        watermark_saved: Optional[str] = None

        stacker = stacker_cls(
            client,
            temporal_window=args.temporal_window,
            since_dt=since_dt,
            until_dt=until_dt,
            hash_threshold=args.hash_threshold,
            dry_run=args.dry_run,
            include_videos=args.include_videos,
            state_file=state_file,
            run_scope=effective_user_filter if effective_user_filter else '__all_users__',
            video_skip_preview_404=args.video_skip_preview,
            video_frame_fallback=args.video_frame_fallback,
            video_frame_fallback_timeout=args.video_frame_fallback_timeout,
        )

        if args.use_watermark and args.since is None and args.last_n_days is None:
            loaded_watermark = _load_watermark(state_file, stacker.run_key)
            if loaded_watermark is not None:
                since_dt = loaded_watermark
                watermark_loaded = _format_datetime_utc(loaded_watermark)
                stacker = stacker_cls(
                    client,
                    temporal_window=args.temporal_window,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    hash_threshold=args.hash_threshold,
                    dry_run=args.dry_run,
                    include_videos=args.include_videos,
                    state_file=state_file,
                    run_scope=effective_user_filter if effective_user_filter else '__all_users__',
                    video_skip_preview_404=args.video_skip_preview,
                    video_frame_fallback=args.video_frame_fallback,
                    video_frame_fallback_timeout=args.video_frame_fallback_timeout,
                )

        stacks_created = stacker.run(assets, user_filter=effective_user_filter)

        if args.use_watermark and stacker.last_processed_max_created_dt is not None:
            _save_watermark(state_file, stacker.run_key, stacker.last_processed_max_created_dt)
            watermark_saved = _format_datetime_utc(stacker.last_processed_max_created_dt)

        logger.info(f"Completed. Created {stacks_created} stacks.")

        if args.output_json:
            _emit_json(
                {
                    'status': 'ok',
                    'summary': stacker.last_run_summary,
                    'watermark': {
                        'loaded': watermark_loaded,
                        'saved': watermark_saved,
                    },
                }
            )

        return 0

    except Exception as exc:
        logger.error(f'Fatal error: {exc}', exc_info=True)
        if args.output_json:
            _emit_json({'status': 'error', 'error': str(exc)})
        return 1


def main(
    argv: Optional[list[str]] = None,
    immich_client_cls: Type[ImmichClient] = ImmichClient,
    stacker_cls: Type[SmartStacker] = SmartStacker,
    unstack_fn: Callable = unstack_all,
    logger_override=None,
):
    active_logger = logger_override or logger

    args = parse_args(argv)

    if args.verbose:
        active_logger.setLevel(logging.DEBUG)
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    if args.interval_seconds <= 0:
        return _run_once(args, immich_client_cls=immich_client_cls, stacker_cls=stacker_cls, unstack_fn=unstack_fn)

    run_number = 0
    failures = 0

    while True:
        run_number += 1
        active_logger.info(f"Scheduled mode: starting run {run_number}")
        exit_code = _run_once(args, immich_client_cls=immich_client_cls, stacker_cls=stacker_cls, unstack_fn=unstack_fn)
        if exit_code != 0:
            failures += 1
            active_logger.warning(f"Run {run_number} failed with exit code {exit_code}")

        if args.max_runs is not None and run_number >= args.max_runs:
            if failures:
                active_logger.warning(f"Scheduled mode completed with {failures} failed run(s) out of {run_number}")
                return 1
            return 0

        active_logger.info(f"Scheduled mode: sleeping {args.interval_seconds:.2f}s before next run")
        time.sleep(args.interval_seconds)
