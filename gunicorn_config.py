"""
Handle Gunicorn worker post-fork initialization.
"""

import threading
import os
import signal
from app import start_outgoing_message_consumer


def post_fork(_server, _worker):
    """
    Start the outgoing message consumer in a separate thread in the worker process.
    """

    def terminate(exit_code=1):
        """Terminate Gunicorn."""
        print("Terminating process due to critical error.")
        os.kill(os.getppid(), signal.SIGTERM)
        os._exit(exit_code)

    try:
        consumer_thread = threading.Thread(
            target=start_outgoing_message_consumer,
            daemon=True,
            name="OutgoingMessageConsumer",
        )
        consumer_thread.start()
    except (ConnectionError, RuntimeError) as e:
        print(f"Critical error in PrimaryQueueConsumer: {e}")
        terminate()
