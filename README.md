# Immich Smart Stacker

[![codecov](https://codecov.io/gh/myrveln/immich-smart-stacker/branch/master/graph/badge.svg)](https://app.codecov.io/gh/myrveln/immich-smart-stacker)

Smart visual similarity grouping for Immich photos, designed for iPhone burst detection and similar photo sequences.

## Features

- **Temporal Clustering**: Groups photos taken within configurable time window (default: 2 seconds)
- **Visual Similarity**: Uses perceptual hashing to group visually similar photos
- **Burst Detection**: Ideal for iPhone burst sequences
- **Improved Video Handling**: Optional ffmpeg frame fallback and clearer video diagnostics when `--include-videos` is enabled
- **Dry Run Mode**: Preview changes before applying
- **Multi-User Support**: Can be run per-user
- **Resilient API Calls**: Built-in timeout and retry/backoff for transient API failures
- **Docker Ready**: Includes a container image and publish workflow

## Setup

### Run with Docker

Docker is the recommended way to run Immich Smart Stacker.

Run the published image with environment variables:

```bash
docker run --rm \
  -e IMMICH_API_URL=http://127.0.0.1:2283/api \
  -e IMMICH_API_KEY=YOUR_API_KEY \
  -e IMMICH_USER_FILTER=12345-abcde-67890-fghij \
  -e TEMPORAL_WINDOW=30.0 \
  -e HASH_THRESHOLD=12 \
  -e INCLUDE_VIDEOS=true \
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
      # Optional tuning
      # IMMICH_USER_FILTER: "12345-abcde-67890-fghij"
      # TEMPORAL_WINDOW: "30.0"
      # HASH_THRESHOLD: "12"
      # INCLUDE_VIDEOS: "true"
      # DRY_RUN: "true"
      # UNSTACK_ALL: "false"
    volumes:
      - ./immich-smart-stacker-data:/data
```

Notes:
- `IMMICH_API_URL` uses the Immich server container name on the same Compose network (`immich-server` is the default service name in many Immich setups).
- Put `IMMICH_API_KEY` in your `.env` file next to `docker-compose.yml`.
- Create a key in Immich with `asset:view`, `asset:read`, and `stack:*` permissions.

Start or update the service:

```bash
docker compose up -d immich-smart-stacker
```

See the [Docker](#docker) section for image links and pull details.

## Development (Local)

Use local Python only for development, debugging, and tests. Regular usage should use Docker or docker-compose.

### Python venv setup

```bash
# From the smart-stacker directory
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies inside the venv for local development
pip install -r requirements.txt

python immich-smart-stacker.py --help
```

Deactivate when done:

```bash
deactivate
```

## Docker

Docker Hub: [dockerhub/myrveln/immich-smart-stacker](https://hub.docker.com/r/myrveln/immich-smart-stacker)

Pull latest image:

```bash
docker pull docker.io/myrveln/immich-smart-stacker:latest
```

## Configuration

### Command Line Arguments

- `--api-url` (required unless `IMMICH_API_URL` is set): Immich API URL (e.g., `http://localhost:2283` or `http://localhost:2283/api`)
- `--api-key` (required unless `IMMICH_API_KEY` is set): Immich API key with `asset:view`, `asset:read`, and `stack:*` permissions
- `--user-filter`: Filter results to specific user ID (optional)
- `--all-users`: Process all users returned by the API (by default, script auto-filters to current user)
- `--temporal-window` (default: 2.0): Burst detection window in seconds
  - iPhone bursts: ~0-10ms between frames; 2 seconds captures most bursts
  - Adjust up if you want more lenient grouping
- `--hash-threshold` (default: 8): Hamming distance threshold for visual similarity
  - Lower = stricter matching (fewer false positives)
  - 5-8 = good for burst detection (same motive, rapid succession)
  - 10-15 = lenient (catches similar compositions)
- `--dry-run`: Preview stacks without creating them
- `--unstack-all`: Delete stacks instead of creating them (scoped by `--user-filter` when provided)
- `--include-videos`: Also try hashing videos (disabled by default; image-only is more reliable)
- `--video-frame-fallback`: For videos, attempt ffmpeg frame extraction if thumbnail hashing fails (off by default)
- `--video-skip-preview` / `--no-video-skip-preview`: Control whether video preview `404` skips thumbnail fallback request (default: skip)
- `--video-frame-fallback-timeout` (default: 10.0): Timeout in seconds for ffmpeg frame extraction fallback
- `--verbose`: Enable debug logging

Notes:
- `--verbose` now focuses on script internals and avoids noisy low-level HTTP connection spam.
- If your key can list metadata beyond assets it can read thumbnails for, default auto-filtering helps avoid repeated `403` thumbnail warnings.
- Videos are skipped by default to avoid noisy `404` thumbnail misses on some media; use `--include-videos` to opt in.
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

## Environment Variables

The Docker image reads these variables:

- `IMMICH_API_URL`: Immich API base URL, usually ending in `/api`
- `IMMICH_API_KEY`: Immich API key
- `IMMICH_USER_FILTER`: Optional user ID filter
- `TEMPORAL_WINDOW`: Optional temporal window in seconds
- `HASH_THRESHOLD`: Optional visual similarity threshold
- `INCLUDE_VIDEOS`: Set to `true` to enable video hashing
- `VIDEO_FRAME_FALLBACK`: Set to `true` to try ffmpeg frame extraction for videos when thumbnails fail
- `VIDEO_SKIP_PREVIEW`: Set to `true` (default) to skip thumbnail fallback request when video preview is missing
- `VIDEO_FRAME_FALLBACK_TIMEOUT`: Timeout in seconds for ffmpeg fallback frame extraction
- `DRY_RUN`: Set to `true` to preview only
- `UNSTACK_ALL`: Set to `true` to delete all matching stacks
- `SMART_STACKER_STATE_FILE`: Optional path for the local idempotency cache

## Testing and Coverage

The repository includes a GitHub Actions test workflow that runs pytest with coverage, writes a coverage summary to the job summary, and uploads `tests/coverage.xml` as an artifact.

## Release Flow

Releases are automated after the test workflow succeeds on `master`.

### Versioning

This project uses Semantic Versioning: `MAJOR.MINOR.PATCH`.
