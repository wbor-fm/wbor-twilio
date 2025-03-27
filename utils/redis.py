"""
Redis utilities.
"""

from redis import Redis

from config import REDIS_ACK_EXPIRATION_S, REDIS_DB, REDIS_HOST, REDIS_PORT
from utils.logging import configure_logging

logger = configure_logging(__name__)

# Initialize Redis client
redis_client = Redis(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
)


def set_ack_event(message_id):
    """
    Set an acknowledgment event in Redis with an expiration time.

    Parameters:
    - message_id (str): The unique message ID to set an acknowledgment event for.
    """
    redis_client.set(message_id, "pending", ex=REDIS_ACK_EXPIRATION_S)
    logger.debug("Set ack event: %s", message_id)


def get_ack_event(message_id):
    """
    Get the acknowledgment status for a message ID.

    Parameters:
    - message_id (str): The unique message ID to check for acknowledgment.

    Returns:
    - str: The status of the acknowledgment event (e.g. 'pending', 'acknowledged', None
    """
    ack_event = redis_client.get(message_id)
    logger.debug("Retrieved ack event: %s", message_id)
    return ack_event


def delete_ack_event(message_id):
    """
    Delete an acknowledgment event from Redis. Used after the message has been processed.

    Parameters:
    - message_id (str): The unique message ID to delete the acknowledgment
    """
    if redis_client.exists(message_id):
        redis_client.delete(message_id)
        logger.debug("Deleted ack event: %s", message_id)
    else:
        logger.warning(
            "Attempted to delete non-existent ack event for message_id: %s", message_id
        )
