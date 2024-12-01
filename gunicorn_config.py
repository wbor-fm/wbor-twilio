def post_fork(server, worker):
    from app import start_outgoing_message_consumer
    import threading
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Starting outgoing message consumer in worker process.")
    consumer_thread = threading.Thread(target=start_outgoing_message_consumer, daemon=True)
    consumer_thread.start()