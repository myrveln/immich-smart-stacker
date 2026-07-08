from .client import ImmichClient
from .logging_config import logger


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
