"""
Handle Gunicorn worker post-fork initialization.
"""

import threading
from app import start_outgoing_message_consumer


def post_fork(_server, _worker):
    """
    Start the outgoing message consumer in a separate thread in the worker process.
    """

    consumer_thread = threading.Thread(
        target=start_outgoing_message_consumer,
        daemon=True,
        name="OutgoingMessageConsumer",
    )
    consumer_thread.start()
