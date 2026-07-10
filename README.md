# Immich Smart Stacker

[![Tests](https://github.com/myrveln/immich-smart-stacker/actions/workflows/test.yml/badge.svg?branch=master)](https://github.com/myrveln/immich-smart-stacker/actions/workflows/test.yml) [![codecov](https://codecov.io/gh/myrveln/immich-smart-stacker/branch/master/graph/badge.svg)](https://app.codecov.io/gh/myrveln/immich-smart-stacker) [![Release](https://github.com/myrveln/immich-smart-stacker/actions/workflows/release.yml/badge.svg?branch=master)](https://github.com/myrveln/immich-smart-stacker/actions/workflows/release.yml) [![Docker Pulls](https://img.shields.io/docker/pulls/myrveln/immich-smart-stacker)](https://hub.docker.com/r/myrveln/immich-smart-stacker)

Smart visual similarity grouping for Immich photos, designed for iPhone burst detection and similar photo sequences.

## Features

- **Temporal Clustering**: Groups photos taken within configurable time window (default: 2 seconds)
- **Visual Similarity**: Uses perceptual hashing to group visually similar photos
- **Burst Detection**: Ideal for iPhone burst sequences
- **Improved Video Handling**: Optional ffmpeg frame fallback and clearer video diagnostics when `--include-videos` is enabled
- **Scheduled Mode**: Optional loop mode with configurable run interval and max-runs
- **Incremental Processing**: Time-window filtering with `since`/`until` and rolling `last-n-days`
- **Watermark State**: Resume recurring runs from last successful high-water timestamp
- **Machine-Readable Output**: Optional JSON run summary for automation/monitoring
- **Automatic User Scoping**: Defaults to current authenticated user unless `--all-users` is set
- **Idempotent Merge Behavior**: Expands/intersects existing stacks instead of creating duplicates
- **Dry Run Mode**: Preview changes before applying
- **Multi-User Support**: Can be run per-user
- **Resilient API Calls**: Built-in timeout and retry/backoff for transient API failures
- **Docker Ready**: Includes a container image and publish workflow

## Setup

### Run with Docker

Docker is the recommended way to run Immich Smart Stacker.
Docker Hub: [dockerhub/myrveln/immich-smart-stacker](https://hub.docker.com/r/myrveln/immich-smart-stacker)

Run the published image with environment variables:

```bash
docker run --rm \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_API_KEY \
  docker.io/myrveln/immich-smart-stacker:latest
```

For a persistent state cache, mount a volume at `/data`:

```bash
docker run --rm \
  -v "$PWD/data:/data" \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_API_KEY \
  docker.io/myrveln/immich-smart-stacker:latest
```

For all optional runtime environment variables, see [Configuration](#configuration).

For repeated scheduled runs, keep `/data` mounted so the local idempotency cache is preserved between container executions.

### Run with docker-compose

If you already run Immich with Docker Compose, add this service to your existing `docker-compose.yml` under `services:`:

```yaml
  immich-smart-stacker:
    image: docker.io/myrveln/immich-smart-stacker:latest
    container_name: immich-smart-stacker
    restart: "no"
    environment:
      IMMICH_API_URL: http://immich-server:2283/api
      IMMICH_API_KEY: ${IMMICH_API_KEY}
    volumes:
      - ./immich-smart-stacker-data:/data
```

Notes:
- `IMMICH_API_URL` uses the Immich server container name on the same Compose network (`immich-server` is the default service name in many Immich setups).
- Put `IMMICH_API_KEY` in your `.env` file next to `docker-compose.yml`.
- Create a key in Immich with `asset:view`, `asset:read`, and `stack:*` permissions.
- For all optional runtime environment variables, see [Configuration](#configuration).
- Keep the `/data` volume in place for repeated scheduled runs so idempotency state persists.

## Configuration

The table below maps environment variables to their equivalent command line arguments.

| Environment Variable | CLI Argument | Default | Description |
|---|---|---|---|
| `IMMICH_API_URL` | `--api-url` | none | Immich API URL. Required unless `--api-url` is provided. |
| `IMMICH_API_KEY` | `--api-key` | none | Immich API key. Required unless `--api-key` is provided. |
| `IMMICH_USER_FILTER` | `--user-filter` | empty | Restrict processing to one user ID. |
| `ALL_USERS` | `--all-users` | false | Process all users instead of auto-scoping to current user. |
| `TEMPORAL_WINDOW` | `--temporal-window` | 2.0 | Temporal grouping window in seconds for burst clustering. |
| `SINCE` | `--since` | empty | ISO-8601 lower bound timestamp. |
| `UNTIL` | `--until` | empty | ISO-8601 upper bound timestamp. |
| `LAST_N_DAYS` | `--last-n-days` | empty | Rolling lookback window in days. Overrides `SINCE`. |
| `USE_WATERMARK` | `--use-watermark` | false | Enable incremental watermark load/save. Auto-load is skipped when `SINCE` or `LAST_N_DAYS` is set. |
| `HASH_THRESHOLD` | `--hash-threshold` | 8 | Visual similarity threshold. Lower is stricter. |
| `DRY_RUN` | `--dry-run` | false | Preview mode; no stack writes are made. |
| `UNSTACK_ALL` | `--unstack-all` | false | Delete stacks instead of creating/merging stacks. |
| `INCLUDE_VIDEOS` | `--include-videos` | false | Include video assets in hashing flow. |
| `VIDEO_FRAME_FALLBACK` | `--video-frame-fallback` | false | Use ffmpeg frame extraction when video thumbnail hashing fails. |
| `VIDEO_SKIP_PREVIEW` | `--video-skip-preview` / `--no-video-skip-preview` | true | Control whether preview `404` skips thumbnail fallback request. |
| `VIDEO_FRAME_FALLBACK_TIMEOUT` | `--video-frame-fallback-timeout` | 10.0 | Timeout (seconds) for ffmpeg frame extraction fallback. |
| `INTERVAL_SECONDS` | `--interval-seconds` | 0 | Scheduled loop interval in seconds. `0` means run once. |
| `MAX_RUNS` | `--max-runs` | empty | Optional cap on scheduled loop iterations. |
| `OUTPUT_JSON` | `--output-json` | false | Emit machine-readable JSON summary. |
| `VERBOSE` | `--verbose` | false | Enable debug logging. |
| `SMART_STACKER_STATE_FILE` | `--state-file` | `/data/.immich-smart-stacker-state.json` | Path used for idempotency cache and incremental watermark state. |

Notes:
- `--verbose` now focuses on script internals and avoids noisy low-level HTTP connection spam.
- If your key can list metadata beyond assets it can read thumbnails for, default auto-filtering helps avoid repeated `403` thumbnail warnings.
- Videos are skipped by default to avoid noisy `404` thumbnail misses on some media; use `--include-videos` to opt in.
- Set `--interval-seconds > 0` for daemon/scheduled mode in a single container.
- Use `--max-runs` to bound scheduled mode in CI/tests or one-shot batch jobs.
- `--last-n-days` takes precedence over `--since`.
- `--use-watermark` only loads a watermark automatically when `--since` and `--last-n-days` are not set.
- Existing stacks are not treated as immutable: if a new run finds a larger matching group that intersects an existing stack, the script will merge/extend the stack.
- In `--unstack-all` mode: with `--user-filter <userId>`, only that user's stacks are deleted; without `--user-filter`, stacks for all users are deleted.

### iPhone Burst Patterns

iPhones capture burst sequences with:
- **Temporal spacing**: 0-10ms between frames (within one photo moment)
- **Composition**: Nearly identical framing and content
- **Default window**: 2 seconds catches all burst photos

## Getting an API Key

1. Log into Immich web UI
2. Navigate to **Settings > API Keys**
3. Create a new API key with permissions:
  - `asset:view` (required for `/assets/{id}/thumbnail` access)
  - `asset:read` (metadata/search)
  - `stack:*` (to create/modify stacks)

## Performance Notes

- **Hash Computation**: Downloads thumbnails (~50-100KB each) for hashing
- **Typical Runtime**: ~10-30 seconds for 1000 photos
- **Recommended Interval**: 3600 seconds (1 hour) via cron or Docker scheduler
- **Avoid**: Running too frequently (< 300s) to prevent unnecessary API load
- **Minimum Stack Size**: Immich requires stacks to have at least 2 assets (1 primary + 1 secondary)

## Example: Running for Specific User

```bash
# Get user ID from Immich (Settings > Profile or API response)
docker run --rm \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e IMMICH_USER_FILTER=12345-abcde-67890-fghij \
  -e DRY_RUN=true \
  docker.io/myrveln/immich-smart-stacker:latest
```

## Example: Watermark and Scheduled Modes

### Use watermark for recurring one-shot runs

Use this mode when an external scheduler (cron, systemd timer, Kubernetes CronJob) runs the container repeatedly.

```bash
docker run --rm \
  -v "$PWD/data:/data" \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e USE_WATERMARK=true \
  docker.io/myrveln/immich-smart-stacker:latest
```

### Use internal scheduled loop mode

Use this mode when one container should keep running and execute periodically.

```bash
docker run --rm \
  -v "$PWD/data:/data" \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e INTERVAL_SECONDS=3600 \
  docker.io/myrveln/immich-smart-stacker:latest
```

### Use `USE_WATERMARK` and `INTERVAL_SECONDS` together

This is the recommended mode for long-running incremental processing in one container.

```bash
docker run --rm \
  -v "$PWD/data:/data" \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e USE_WATERMARK=true \
  -e INTERVAL_SECONDS=3600 \
  -e MAX_RUNS=24 \
  docker.io/myrveln/immich-smart-stacker:latest
```

Behavior notes:
- `USE_WATERMARK=true` loads the previous successful high-water timestamp from `SMART_STACKER_STATE_FILE` and saves a new value after each successful run.
- Keep `/data` mounted so watermark and idempotency state persist across container restarts.
- If `SINCE` or `LAST_N_DAYS` is set, watermark auto-load is skipped for that run by design.

## Troubleshooting

### "Failed to hash asset"
- Network issue downloading thumbnail
- Corrupted image file in Immich
- Check logs with `--verbose`

### "Connection refused"
- Ensure Immich server is running and accessible
- Check API URL host/port (script accepts both root URL and `/api` URL)
- Verify firewall rules

### "404 on /api/search/metadata"
- Use the latest script version (it uses the correct metadata search method)
- Keep using your API server URL (`http://<host>:2283` is recommended)
- Verify the key has `asset:read` permission

### "400 on /api/search/metadata" after upgrading to Immich 3.x
- Immich 3.x validates metadata search payloads more strictly than older versions.
- Use the latest script version: it now prefers `page`/`size` and auto-falls back for legacy servers.
- If this persists, run with `--verbose` and check the first error response body for invalid fields.

### Too many/few stacks created
- Adjust `--hash-threshold`:
  - Lower (5-6) for stricter similarity matching
  - Higher (10-12) for more lenient grouping
- Adjust `--temporal-window` (default 2.0s works for iPhones)

### Incremental recurring runs
- Use `--last-n-days` for simple rolling windows.
- Use `--use-watermark` to persist and resume from the previous successful high-water mark.
- Keep `SMART_STACKER_STATE_FILE` on a persistent volume (for Docker, mount `/data`) so watermark and idempotency cache survive container restarts.

### API Key Permissions Error
- Regenerate API key with proper permissions
- Ensure `asset:view`, `asset:read`, and `stack:*` are selected

### Unstack everything
```bash
docker run --rm \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e UNSTACK_ALL=true \
  docker.io/myrveln/immich-smart-stacker:latest
```

For one user only:

```bash
docker run --rm \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_KEY \
  -e UNSTACK_ALL=true \
  -e IMMICH_USER_FILTER=12345-abcde-67890-fghij \
  docker.io/myrveln/immich-smart-stacker:latest
```

### Repeated "401/403 thumbnail" messages
- Your key can query metadata but cannot fetch many thumbnails.
- Ensure API key includes `asset:view` (and `asset:read`).
- Run without `--verbose` for minimal output, or with `--user-filter <ownerId>` to scope processing.

### "No assets found"
- Verify `IMMICH_API_URL` points to the Immich server API endpoint that this container can reach.
- Check whether your API key can read metadata (`asset:read`).
- If you use `IMMICH_USER_FILTER`, confirm that user actually owns assets in your library.
- If you use `SINCE`, `UNTIL`, or `LAST_N_DAYS`, your time window may currently exclude all assets.

### Scheduled mode runs only once (or exits immediately)
- Set `INTERVAL_SECONDS` to a value greater than `0`.
- If `MAX_RUNS` is set, the container exits after that many iterations by design.
- Remove `MAX_RUNS` for continuous daemon behavior.

### Watermark is not being used
- `USE_WATERMARK=true` only auto-loads when neither `SINCE` nor `LAST_N_DAYS` is set.
- If you set `SINCE` or `LAST_N_DAYS`, that explicit filter takes precedence for the run.
- Ensure `SMART_STACKER_STATE_FILE` points to a writable location.

### Watermark/state resets after container restart
- Mount persistent storage for `/data` (for example `-v "$PWD/data:/data"`).
- Keep `SMART_STACKER_STATE_FILE` stable across runs (default: `/data/.immich-smart-stacker-state.json`).
- If you run with ephemeral containers and no volume mount, watermark/idempotency state is lost.

### Video frame fallback is not used
- Enable both `INCLUDE_VIDEOS=true` and `VIDEO_FRAME_FALLBACK=true`.
- Keep `VIDEO_SKIP_PREVIEW=true` for the default fast path on missing previews.
- If ffmpeg extraction fails, increase `VIDEO_FRAME_FALLBACK_TIMEOUT`.

## Development (Local)

Use local Python only for development, debugging, and tests. Regular usage should use Docker or docker-compose.

### Python venv setup

```bash
# From the repository root
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies inside the venv for local development
pip install -r requirements.txt

# Module entrypoint
python -m immich_smart_stacker --help
```

Deactivate when done:

```bash
deactivate
```

## Comparison: [immich-stack](https://github.com/Majorfi/immich-stack) vs immich-smart-stacker

| Capability | immich-stack | immich-smart-stacker |
|---------|---------|---------------|
| Matching approach | Filename/regex pattern matching | Temporal proximity + visual similarity (perceptual hash) |
| Visual similarity grouping | ✗ | ✓ |
| Temporal burst grouping | Limited | ✓ (configurable `--temporal-window`) |
| Tuning strictness | Pattern/regex driven | Hash-distance driven (`--hash-threshold`) |
| Burst/near-duplicate focus | Partial | Strong |
| Best fit | RAW+JPG style filename pairing | iPhone bursts and near-duplicate cleanup |

**Recommendation**: Use [immich-stack](https://github.com/Majorfi/immich-stack) for filename/pattern-driven pairing, and immich-smart-stacker for burst detection (temporal + visual).

## Security Notes

- Store API keys in environment variables or `.env` file (never hardcode)
- Smart Stacker only reads assets and manages stacks; `--unstack-all` is the only delete mode

## Testing and Coverage

The repository includes a GitHub Actions test workflow that runs pytest with coverage, writes a coverage summary to the job summary, and uploads `tests/coverage.xml` as an artifact.

## Release Flow

Releases are automated after the test workflow succeeds on `master`.

### Versioning

This project uses Semantic Versioning: `MAJOR.MINOR.PATCH`.
