"""
Handle Gunicorn worker post-fork initialization.
"""

import threading
import logging
from app import start_outgoing_message_consumer


def post_fork(_server, worker):
    """
    Start the outgoing message consumer in a separate thread in the worker process.
    """

    logger = logging.getLogger(__name__)
    logger.info("Starting outgoing message consumer in worker process: %s", worker.pid)

    consumer_thread = threading.Thread(
        target=start_outgoing_message_consumer,
        daemon=True,
        name="OutgoingMessageConsumer",
    )
    consumer_thread.start()
