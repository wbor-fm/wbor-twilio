"""
Twilio Handler.

We have a Twilio phone number that can receive SMS messages & more.
When a SMS message is received at this number, the primary handler is configured as this Flask app.
If this app doesn't respond with an OK status code, Twilio will fall back to the secondary 
handler (which is a Twilio function).

This app has two primary functions:
1. Publish incoming SMS messages to RabbitMQ for processing by other services.
2. Send outgoing SMS messages using the Twilio API.

Future functionality may include (e.g. logging in PG):
- Handling incoming voice intelligence data (at /voice-intelligence).
- Handling incoming call events (at /call-events).

The workflows are as follows.

Incoming SMS:
1. A SMS message is received by Twilio.
2. Twilio sends the message to this app at the `/sms` endpoint.
3. @validate_twilio_request decorator validates the authenticity of the request.
4. A unique message ID is generated and added to the message data as `wbor_message_id`.
5. An acknowledgment event is set in Redis with the message ID.
    - This is used to ensure the message is processed properly downstream by wbor-groupme.
    - (Upon receipt from the consumer of successful forwarding to GroupMe, 
        response is sent to Twilio.)
6. Twilio phone number lookup is used to fetch the name of the sender and added to the 
   message data if available as `SenderName`. If the lookup fails, the sender name is set to 
   `Unknown`.
    - If the lookup fails, the sender name is set to `Unknown`.
7. The message data is published to RabbitMQ with the routing key `source.twilio.sms.incoming`.
    - Sent to `source_exchange` (after asserting that it exists)
    - Routing key is in the format `source.<source>.<type>`
        - e.g. `source.twilio.sms.incoming` or `source.twilio.sms.outgoing`
    - The message type is also included in the JSON message body for downstream consumers.
8. The main thread waits for an acknowledgment from the `/acknowledge` endpoint, indicating 
   successful processing by GroupMe.
    - If the acknowledgment is not received within a timeout, the message is discarded.
    - Subsequently, Twilio will fall back to the secondary handler.
    - NOTE: `/acknowledge` requires more than one worker process to work properly.
    - NOTE: `/acknowledge` does not validate the SOURCE of the acknowledgment, which is a potential 
      security risk (though unlikely in our closed network).
9. Upon receiving the acknowledgment, return an empty TwiML response to Twilio to acknowledge 
   receipt of the message.

Outgoing SMS:
0. Upon launching the app, a consumer thread is started to listen for outgoing SMS messages.
    - The consumer listens for messages with the routing key 'source.twilio.sms.outgoing'.
1. A GET request is made using the `/send` endpoint in a browser.
    - Expects a password for authorization set by APP_PASSWORD.
    - Expects `recipient_number` and `message` as query parameters.
2. Validates the recipient number and message body.
    - `recipient_number` must be in E.164 format.
    - `message` must not exceed the Twilio character limit.
    - `message` body must exist.
3. Generates a unique message ID for tracking, set as `wbor_message_id`.
4. Prepares the outgoing message data, including a timestamp.
5. Publishes it to RabbitMQ with the routing key `source.twilio.sms.outgoing`.
6. The consumer thread consumes by running process_outgoing_message().

Emits keys:
- `source.twilio.sms.incoming`
    - Routed to wbor-groupme for processing.
- `source.twilio.sms.outgoing`
    - Local queue for sending outgoing SMS messages.
- `source.twilio.voice-intelligence`
- `source.twilio.call-events`

Ideas:
- Use Twilio SID instead of UUID?

TODO:
- Log incoming voice intelligence data
    - Store transcripts in PG?
- Log incoming call events
    - Store audio files?
- Log sent SMS messages
    - Possibly use SID instead of UUID?
    - Or both
"""

import logging
import json
import re
import sys
import os
import signal
from threading import Thread
from functools import wraps
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from uuid import uuid4
import pika
from pika.exceptions import (
    AMQPError,
    AMQPConnectionError,
    AMQPChannelError,
    ChannelClosedByBroker,
)
from flask import Flask, abort, request
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException
from twilio.twiml.messaging_response import MessagingResponse
from utils.logging import configure_logging
from utils.redis import redis_client, set_ack_event, get_ack_event, delete_ack_event
from config import (
    APP_PORT,
    APP_PASSWORD,
    SOURCE,
    OUTGOING_QUEUE,
    SMS_OUTGOING_KEY,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    TWILIO_CHARACTER_LIMIT,
    RABBITMQ_HOST,
    RABBITMQ_USER,
    RABBITMQ_PASS,
    RABBITMQ_EXCHANGE,
    REDIS_ACK_EXPIRATION,
)

logging.root.handlers = []
logger = configure_logging()

app = Flask(__name__)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_client.http_client.logger.setLevel(logging.INFO)


def terminate(exit_code=1):
    """Terminate the process."""
    os.kill(os.getppid(), signal.SIGTERM)  # Gunicorn master
    os._exit(exit_code)  # Current thread


# RabbitMQ


def publish_to_exchange(key, sub_key, data):
    """
    Publishes a message to a RabbitMQ queue.

    Parameters:
    - key (str): The name of the message key.
    - sub_key (str): The name of the sub-key for the message. (e.g. 'sms', 'call')
    - data (dict): The message content, which will be converted to JSON format.
    """
    try:
        logger.debug("Attempting to connect to RabbitMQ...")
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        parameters = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            credentials=credentials,
            client_properties={"connection_name": "TwilioConsumerConnection"},
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        logger.debug("RabbitMQ connected!")

        # Assert the exchange exists
        channel.exchange_declare(
            exchange=RABBITMQ_EXCHANGE,
            exchange_type="topic",
            durable=True,
        )

        # Publish message to exchange
        channel.basic_publish(
            exchange=RABBITMQ_EXCHANGE,
            routing_key=f"source.{key}.{sub_key}",
            body=json.dumps(
                {**data, "type": sub_key}  # Include type in the message body
            ).encode(),  # Encodes msg as bytes. RabbitMQ requires byte data
            properties=pika.BasicProperties(
                headers={
                    "x-retry-count": 0
                },  # Initialize retry count for other consumers
                delivery_mode=2,  # Persistent message - write to disk for safety
            ),
        )
        logger.debug(
            "Publishing message body: %s", json.dumps({**data, "type": sub_key})
        )
        logger.info(
            "Published message to `%s` with routing key: `source.%s.%s`: %s - %s - UID: %s",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            data.get("SenderName", "Unknown"),
            data.get("Body", "No message body"),
            data.get("wbor_message_id"),
        )
        connection.close()
    except AMQPConnectionError as conn_error:
        error_message = str(conn_error)
        logger.error(
            "Connection error when publishing to `%s` with routing key "
            "`source.%s.%s`: %s",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            error_message,
        )
        if "CONNECTION_FORCED" in error_message and "shutdown" in error_message:
            logger.critical("Broker shut down the connection. Shutting down consumer.")
            sys.exit(1)
        if "ACCESS_REFUSED" in error_message:
            logger.critical(
                "Access refused. Check RabbitMQ user permissions. Shutting down consumer."
            )
        terminate()
    except AMQPChannelError as chan_error:
        logger.error(
            "Channel error when publishing to `%s` with routing key `source.%s.%s`: %s",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            chan_error,
        )
    except json.JSONDecodeError as json_error:
        logger.error("JSON encoding error for message %s: %s", data, json_error)


def validate_twilio_request(f):
    """Validates that incoming requests genuinely originated from Twilio"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        validator = RequestValidator(TWILIO_AUTH_TOKEN)

        # Parse and reconstruct the URL to ensure query strings are encoded
        parsed_url = urlparse(request.url)
        query = urlencode(parse_qsl(parsed_url.query, keep_blank_values=True))

        # Ensure the path includes /twilio prefix
        # This is a hack because I couldn't figure out how to get NGINX to not
        # strip /twilio when putting this app behind a proxy instead of serving at the root
        path_with_prefix = parsed_url.path
        if not path_with_prefix.startswith("/twilio"):
            path_with_prefix = "/twilio" + path_with_prefix

        encoded_url = urlunparse(
            (
                parsed_url.scheme.replace("http", "https"),
                parsed_url.netloc,
                path_with_prefix,  # Use the adjusted path
                parsed_url.params,
                query,
                parsed_url.fragment,
            )
        )

        # Validate request
        request_valid = validator.validate(
            encoded_url, request.form, request.headers.get("X-TWILIO-SIGNATURE", "")
        )

        if not request_valid:
            logger.error("Twilio request validation failed!")
            logger.debug("Form data used for validation: %s", request.form)
            return abort(403)
        return f(*args, **kwargs)

    return decorated_function


def fetch_name(sms_data):
    """
    Attempt to fetch the name associated with a phone number using the Twilio Lookup API.

    caller_name is the name associated with the phone number in the Twilio database.
    If the name is not available, it returns 'Unknown'.

    Don't confuse this with the 'From' field in the SMS data, which is the phone number.

    Parameters:
    - sms_data (dict): The SMS message data containing the sender's phone number.

    Returns:
    - str: The name of the sender if available, otherwise 'Unknown'.
    """
    phone_number = sms_data.get("From")
    if not phone_number:
        logger.warning("No 'From' field in SMS data: %s", sms_data)
        return "Unknown"

    try:
        phone_info = twilio_client.lookups.v2.phone_numbers(phone_number).fetch(
            fields="caller_name"
        )

        caller_name = phone_info.caller_name or "Unknown"
        logger.info("Fetched name: %s", caller_name.get("caller_name", "Unknown"))
        return caller_name.get("caller_name", "Unknown")
    except TwilioRestException as e:
        logger.error(
            "Failed to fetch caller name for number %s: %s", phone_number, str(e)
        )
        return "Unknown"


def send_sms(recipient_number, message_body):
    """
    Sends an SMS message using the Twilio API.
    Logs calls to Postgres.

    Parameters:
    - recipient_number (str): The phone number to send the message to (in E.164 format).
    - message_body (str): The body of the SMS message.

    Returns:
    - str: The SID of the sent message if successful.

    Raises:
    - Exception: If the message fails to send.
    """
    if not recipient_number or not message_body:
        logger.error("Recipient number or message body cannot be empty")
        raise ValueError("Recipient number and message body are required")

    if len(message_body) > TWILIO_CHARACTER_LIMIT:
        logger.error(
            "Message body exceeds Twilio's character limit of %d",
            TWILIO_CHARACTER_LIMIT,
        )
        raise ValueError(
            f"Message exceeds the character limit of {TWILIO_CHARACTER_LIMIT}"
        )

    try:
        logger.debug("Attempting to send SMS to %s", recipient_number)
        message = twilio_client.messages.create(
            to=recipient_number,
            from_=TWILIO_PHONE_NUMBER,
            body=message_body,
        )
        return message.sid
    except TwilioRestException as e:
        logger.error("Failed to send SMS to %s: %s", recipient_number, str(e))
        return None


def start_outgoing_message_consumer():
    """
    Starts a RabbitMQ consumer for the outgoing message queue.
    Handles sending SMS messages using the Twilio API.
    """

    def process_outgoing_message(channel, method, _properties, body):
        logger.debug("Received message with routing key: %s", method.routing_key)

        # Validate routing key
        if method.routing_key != SMS_OUTGOING_KEY:
            logger.warning(
                "Discarding message due to mismatched routing key: %s (expecting `%s`)",
                method.routing_key,
                SMS_OUTGOING_KEY,
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            message = json.loads(body)
            recipient_number = message.get("recipient_number")
            sms_body = message.get("message")
            if not recipient_number or not sms_body:
                logger.warning("Invalid message format: %s", message)
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return

            # Attempt to send the SMS
            msg_sid = send_sms(recipient_number, sms_body)
            if msg_sid:
                logger.info(
                    "Message sent successfully. SID: %s, UID: %s",
                    msg_sid,
                    message.get("wbor_message_id"),
                )
                channel.basic_ack(delivery_tag=method.delivery_tag)
                # TODO: log the sent message to PG

        except (AMQPError, json.JSONDecodeError) as e:
            logger.error("Failed to process message: %s", str(e))
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except ValueError as ve:
            logger.error("Validation error: %s. Discarding message.", ve)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def consumer_thread():
        while True:
            try:
                logger.debug("Connecting to RabbitMQ for outgoing SMS messages...")
                credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
                parameters = pika.ConnectionParameters(
                    host=RABBITMQ_HOST,
                    credentials=credentials,
                    client_properties={
                        "connection_name": "OutgoingSMSConsumerConnection"
                    },
                )
                connection = pika.BlockingConnection(parameters)
                channel = connection.channel()

                # Assert that the primary exchange exists
                channel.exchange_declare(
                    exchange=RABBITMQ_EXCHANGE, exchange_type="topic", durable=True
                )

                try:
                    # Declare the queue
                    channel.queue_declare(queue=OUTGOING_QUEUE, durable=True)
                    channel.queue_bind(
                        queue=OUTGOING_QUEUE,
                        exchange=RABBITMQ_EXCHANGE,
                        routing_key=SMS_OUTGOING_KEY,  # Only bind to this key
                    )
                except ChannelClosedByBroker as e:
                    if "inequivalent arg" in str(e):
                        # If the queue already exists with different attributes, log and terminate
                        logger.warning(
                            "Queue already exists with mismatched attributes. "
                            "Please resolve this conflict before restarting the application."
                        )
                        terminate()

                # Ensure one message is processed at a time
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=OUTGOING_QUEUE,
                    on_message_callback=process_outgoing_message,
                )

                logger.info(
                    "Outgoing message consumer is ready. Waiting for messages..."
                )
                channel.start_consuming()
            except AMQPConnectionError as conn_error:
                error_message = str(conn_error)
                logger.error(
                    "Failed to connect to RabbitMQ: %s",
                    error_message,
                )
                if "CONNECTION_FORCED" in error_message and "shutdown" in error_message:
                    logger.critical(
                        "Broker shut down the connection. Shutting down consumer..."
                    )
                    sys.exit(1)
                if "ACCESS_REFUSED" in error_message:
                    logger.critical(
                        "Access refused. Check RabbitMQ user permissions. Shutting down process..."
                    )
                    terminate()
            finally:
                if "connection" in locals() and connection.is_open:
                    connection.close()

    Thread(target=consumer_thread, daemon=True).start()


# Routes


@app.route("/acknowledge", methods=["POST"])
def groupme_acknowledge():
    """
    Endpoint for receiving acknowledgment from the GROUPME_QUEUE consumer.

    Expects a JSON payload with a 'wbor_message_id' field indicating the message processed.
    """
    ack_data = request.json
    if not ack_data or "wbor_message_id" not in ack_data:
        logger.error(
            "Invalid acknowledgment data received at /acknowledge: %s", ack_data
        )
        return "Invalid acknowledgment", 400

    message_id = ack_data.get("wbor_message_id")
    logger.debug("Received acknowledgment for: %s", message_id)

    if get_ack_event(message_id):
        delete_ack_event(message_id)
        return "Acknowledgment received", 200

    logger.warning("Acknowledgment received for unknown: %s", message_id)
    return "Unknown wbor_message_id", 404


@app.route("/sms", methods=["POST"])
@validate_twilio_request
def receive_sms():
    """
    Handler for incoming SMS messages from Twilio. Publishes messages to RabbitMQ.

    Returns:
    - str: A TwiML response to acknowledge receipt of the message (required by Twilio).
        If a response is not sent, Twilio will fall back to the secondary message handler.
    """
    sms_data = request.form.to_dict()
    logger.debug("Received SMS message: %s", sms_data)
    logger.info("Processing message from: `%s`", sms_data.get("From"))
    resp = MessagingResponse()  # Required by Twilio

    # Generate a unique message ID and add it to the SMS data
    message_id = str(uuid4())
    sms_data["wbor_message_id"] = message_id
    set_ack_event(message_id)

    logger.debug("Attempting to fetch caller name for SMS message")

    def fetch_sender(sms_data, timeout=3):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fetch_name, sms_data)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                logger.warning("Timeout occurred while fetching sender name")
                return "Unknown"

    try:
        sender_name = fetch_sender(sms_data)
    except (TwilioRestException, FuturesTimeoutError) as e:
        logger.error("Error fetching sender name: %s", str(e))
        sender_name = "Unknown"
    sms_data["SenderName"] = sender_name
    sms_data["source"] = SOURCE

    # `sms_data` now includes original Twilio content, `SenderName`, `source`, and `wbor_message_id`
    Thread(target=publish_to_exchange, args=(SOURCE, "sms.incoming", sms_data)).start()

    logger.debug("Waiting for acknowledgment for message_id: %s", message_id)
    # Wait for acknowledgment from the GroupMe consumer so that fallback handler can be
    # triggered if the message fails to process for any reason

    # Note: this requires more than one worker process to work properly
    # (since the main thread is blocked waiting for the /acknowledgment)
    start_time = datetime.now()
    while (datetime.now() - start_time).seconds < REDIS_ACK_EXPIRATION:
        ack_status = redis_client.get(message_id)
        if not ack_status:  # ACK received (deleted by /acknowledge endpoint)
            # So if it's not found, the message was processed
            # Return an empty TwiML response to acknowledge receipt of the message
            logger.debug("Acknowledgment received: %s", message_id)
            return str(resp)
    logger.error(
        "Timeout met while waiting for acknowledgment for message_id: %s", message_id
    )
    delete_ack_event(message_id)
    return "Failed to process message", 500


@app.route("/send", methods=["GET"])
def browser_queue_outgoing_sms():
    """
    Send an SMS message using the Twilio API from a browser address bar. Requires a password.

    Expects recipient_number to be in E.164 format, e.g. +12077253250.
    Encoding the `+` as %2B also works.
    """
    logger.info("Received request to send SMS from browser...")
    # Don't let strangers send messages as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        # TODO: fix this to log the IP address from outside the container
        # logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        logger.warning("Unauthorized access attempt")
        abort(403, "Unauthorized access")

    recipient_number = request.args.get("recipient_number", "").replace(" ", "+")
    message = request.args.get("message")

    if not recipient_number or not re.fullmatch(r"^\+?\d{10,15}$", recipient_number):
        if not recipient_number:
            logger.warning("Recipient number missing")
            abort(400, "Recipient missing")
        logger.warning("Invalid recipient number format: %s", recipient_number)
        abort(400, "Invalid recipient number format (must use the E.164 standard)")
    if not message:
        logger.warning("Message body content missing")
        abort(400, "Message body text is required")
    if len(message) > TWILIO_CHARACTER_LIMIT:
        logger.warning("Message too long: %d characters", len(message))
        abort(400, "Message exceeds character limit")

    # Queue the message for sending
    message_id = str(uuid4())  # Generate a unique ID for tracking
    outgoing_message = {
        "wbor_message_id": message_id,
        "recipient_number": recipient_number,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    publish_to_exchange(SOURCE, "sms.outgoing", outgoing_message)
    logger.info("Message queued for sending. Message ID: %s", message_id)
    return f"Message queued for sending to {recipient_number}"


@app.route("/voice-intelligence", methods=["POST"])
def log_webhook():
    """
    Endpoint for receiving Voice Intelligence webhook events.

    Expects a JSON payload with a 'transcript_sid' field.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.get_json()
    logger.info("Received Voice Intelligence webhook fire with data: %s", data)
    logger.info("Transcript SID: %s", data.get("transcript_sid"))
    return "Accepted", 202


@app.route("/call-events", methods=["POST"])
def log_call_event():
    """
    Endpoint for receiving Call Event webhook events.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.form.to_dict()
    logger.info("Received Call Event webhook fire with data: %s", data)
    return "Accepted", 202


@app.route("/")
def hello_world():
    """Serve a simple static Hello World page at the root"""
    return "<h1>wbor-twilio is online!</h1>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
