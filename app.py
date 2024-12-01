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
2. Twilio sends the message to this app at the /sms endpoint.
3. @validate_twilio_request decorator validates the authenticity of the request.
4. A unique message ID is generated and added to the message data as 'wbor_message_id'.
    - TODO: use Twilio SID instead of UUID?
5. An acknowledgment event is set in Redis with the message ID.
    - This is used to ensure the message is processed properly downstream by wbor-groupme.
6. Twilio phone number lookup is used to fetch the name of the sender and add it to the 
   message data if available.
    - If the lookup fails, the sender name is set to 'Unknown'.
7. The message data is published to RabbitMQ with the routing key 'source.twilio.sms.incoming'.
    - Asserts that `source_exchange` exists
    - Publishes the message to the exchange with the routing key
        - Routing key is in the format 'source.<source>.<type>'
            - e.g. 'source.twilio.sms.incoming' or 'source.twilio.sms.outgoing'
        - The message type is also included in the message body
8. The main thread waits for an acknowledgment from the /acknowledge endpoint, indicating 
   successful processing by GroupMe.
    - If the acknowledgment is not received within a timeout, the message is discarded.
    - Subsequently, Twilio will fall back to the secondary handler.
    - NOTE: /acknowledge requires more than one worker process to work properly.
    - NOTE: /acknowledge does not validate the source of the acknowledgment, which is a potential 
      security risk (though unlikely).
9. Upon receiving the acknowledgment, return an empty TwiML response to Twilio to acknowledge 
   receipt of the message.

Outgoing SMS:

0. Upon launching the app, a consumer thread is started to listen for outgoing SMS messages.
    - The consumer listens for messages with the routing key 'source.twilio.sms.outgoing'.
1. GET using the /send endpoint in a browser.
    - Expects a password for authorization set by APP_PASSWORD.
    - Expects `recipient_number` and `message` as query parameters.
2. Validates the recipient number and message body.
    - `recipient_number` must be in E.164 format.
    - `message` must not exceed the Twilio character limit.
    - `message` body must exist.
3. Generates a unique message ID for tracking, set as `wbor_message_id`.
    - TODO: consider using Twilio SID instead of UUID? Later in the process?
4. Prepares the outgoing message data, including a timestamp.
5. Publishes it to RabbitMQ with the routing key 'source.twilio.sms.outgoing'.
6. The consumer thread consumes by running process_outgoing_message.


TODO:
- Implement a retry mechanism for failed message processing
- Implement a message queue for outgoing messages?
- Do something with incoming voice intelligence data
- Do something with incoming call events
"""

import os
import logging
import json
import re
from threading import Thread
from functools import wraps
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from uuid import uuid4
import pika
import pika.exceptions
import pytz
from flask import Flask, abort, request
from redis import Redis
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
APP_PORT = os.getenv("APP_PORT", "5000")
APP_PASSWORD = os.getenv("APP_PASSWORD")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "wbor-rabbitmq")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
REDIS_HOST = os.getenv("REDIS_HOST", "wbor-redis-server")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_ACK_EXPIRATION = int(os.getenv("REDIS_ACK_EXPIRATION", "60"))  # in seconds

TWILIO_CHARACTER_LIMIT = 1600  # Twilio SMS character limit

SOURCE = "twilio"  # Define source name for RabbitMQ exchange purposes
EXCHANGE = "source_exchange"
OUTGOING_QUEUE = "outgoing_sms"

# Logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Define a handler to output to the console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)


class EasternTimeFormatter(logging.Formatter):
    """Custom log formatter to display timestamps in Eastern Time"""

    def formatTime(self, record, datefmt=None):
        # Convert UTC to Eastern Time
        eastern = pytz.timezone("America/New_York")
        utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        eastern_dt = utc_dt.astimezone(eastern)
        # Use ISO 8601 format
        return eastern_dt.isoformat()


formatter = EasternTimeFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logging.getLogger("werkzeug").setLevel(logging.INFO)

redis_client = Redis(
    host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
)

app = Flask(__name__)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_client.http_client.logger.setLevel(logging.INFO)


# Redis


def set_ack_event(message_id):
    """
    Set an acknowledgment event in Redis with an expiration time.

    Parameters:
    - message_id (str): The unique message ID to set an acknowledgment event for.
    """
    redis_client.set(message_id, "pending", ex=REDIS_ACK_EXPIRATION)
    logger.debug("Set ack event for message_id: %s", message_id)


def get_ack_event(message_id):
    """
    Get the acknowledgment status for a message ID.

    Parameters:
    - message_id (str): The unique message ID to check for acknowledgment.

    Returns:
    - str: The status of the acknowledgment event (e.g. 'pending', 'acknowledged', None
    """
    ack_event = redis_client.get(message_id)
    logger.debug("Retrieved ack event for message_id: %s", message_id)
    return ack_event


def delete_ack_event(message_id):
    """
    Delete an acknowledgment event from Redis. Used after the message has been processed.

    Parameters:
    - message_id (str): The unique message ID to delete the acknowledgment
    """
    if redis_client.exists(message_id):
        redis_client.delete(message_id)
        logger.debug("Deleted ack event for message_id: %s", message_id)
    else:
        logger.warning(
            "Attempted to delete non-existent ack event for message_id: %s", message_id
        )


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
        logger.debug("Connected!")

        # Assert the exchange exists
        channel.exchange_declare(
            exchange=EXCHANGE,
            exchange_type="topic",
            durable=True,
        )

        # Publish message to exchange
        channel.basic_publish(
            exchange=EXCHANGE,
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
        logger.info(
            "Published message to `%s` with routing key: "
            "`source.%s.%s`. Message UID:\n%s",
            EXCHANGE,
            key,
            sub_key,
            data.get("wbor_message_id"),
        )
        connection.close()
    except pika.exceptions.AMQPConnectionError as conn_error:
        logger.error(
            "Connection error when publishing to `%s` with routing key "
            "`source.%s.%s`: %s",
            EXCHANGE,
            key,
            sub_key,
            conn_error,
        )
    except pika.exceptions.AMQPChannelError as chan_error:
        logger.error(
            "Channel error when publishing to `%s` with routing key `source.%s.%s`: %s",
            EXCHANGE,
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
        encoded_url = urlunparse(
            (
                parsed_url.scheme.replace("http", "https"),
                parsed_url.netloc,
                parsed_url.path,
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
    Attempt to fetch the name of the sender of the SMS message.

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
        logger.debug("Fetched caller name: %s", caller_name)
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
        logger.info("Attempting to send SMS to %s", recipient_number)
        message = twilio_client.messages.create(
            to=recipient_number,
            from_=TWILIO_PHONE_NUMBER,
            body=message_body,
        )

        # TODO Log the message to Postgres here - possibly use SID instead of UUID?

        logger.info("SMS sent successfully. SID: %s", message.sid)
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
        if method.routing_key != "source.twilio.outgoing.sms":
            logger.warning(
                "Discarding message due to mismatched routing key: %s",
                method.routing_key,
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
                logger.info("Message sent successfully. SID: %s", msg_sid)
                channel.basic_ack(delivery_tag=method.delivery_tag)

        except ValueError as ve:
            logger.error("Validation error: %s. Discarding message.", ve)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        except Exception as e:
            logger.error("Failed to process message: %s", str(e))
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def consumer_thread():
        while True:
            try:
                logger.info("Connecting to RabbitMQ for outgoing SMS messages...")
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

                # Declare the queue
                channel.queue_declare(queue=OUTGOING_QUEUE, durable=True)
                channel.queue_bind(
                    queue=OUTGOING_QUEUE,
                    exchange=EXCHANGE,
                    routing_key="source.twilio.sms.outgoing",  # Only bind to this key
                )

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
            except pika.exceptions.AMQPConnectionError as conn_error:
                logger.error("Failed to connect to RabbitMQ: %s", conn_error)
            finally:
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
    logger.debug("Received acknowledgment for message_id: %s", message_id)

    if get_ack_event(message_id):
        delete_ack_event(message_id)
        return "Acknowledgment received", 200

    logger.warning(
        "Acknowledgment received for unknown wbor_message_id: %s", message_id
    )
    return "Unknown wbor_message_id", 404


@app.route("/sms", methods=["GET", "POST"])
@validate_twilio_request
def receive_sms():
    """
    Handler for incoming SMS messages from Twilio. Publishes messages to RabbitMQ.

    Returns:
    - str: A TwiML response to acknowledge receipt of the message (required by Twilio).
        If a response is not sent, Twilio will fall back to the secondary message handler.
    """
    sms_data = request.form.to_dict()
    logger.debug("Received SMS message with data: %s", sms_data)
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
                logger.error("Timeout occurred while fetching sender name")
                return "Unknown"

    try:
        sender_name = fetch_sender(sms_data)
    except (TwilioRestException, FuturesTimeoutError) as e:
        logger.error("Error fetching sender name: %s", str(e))
        sender_name = "Unknown"
    sms_data["SenderName"] = sender_name

    Thread(target=publish_to_exchange, args=(SOURCE, "sms.incoming", sms_data)).start()

    logger.debug("Waiting for acknowledgment for message_id: %s", message_id)
    # Wait for acknowledgment from the GroupMe consumer so that fallback handler can be
    # triggered if the message fails to process for any reason

    # Note: this requires more than one worker process to work properly
    # (since the main thread is blocked waiting for the /acknowledgment)
    start_time = datetime.now()
    while (datetime.now() - start_time).seconds < REDIS_ACK_EXPIRATION:
        if not redis_client.get(message_id):
            # /acknowledge was received
            # /acknowledge deletes the ack event from Redis
            # So if it's not found, the message was processed
            # Return an empty TwiML response to acknowledge receipt of the message
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
    logger.debug("Received request to send SMS from browser...")
    # Don't let strangers send messages as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        abort(403, "Unauthorized access")

    recipient_number = request.args.get("recipient_number", "").replace(" ", "+")
    message = request.args.get("message")

    if not recipient_number or not re.fullmatch(r"^\+?\d{10,15}$", recipient_number):
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


# Future functionality


@app.route("/voice-intelligence", methods=["POST"])
def log_webhook():
    """
    Endpoint for receiving Voice Intelligence webhook events.

    Expects a JSON payload with a 'transcript_sid' field.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.get_json()
    logger.debug("Received Voice Intelligence webhook fire with data: %s", data)
    logger.debug("Transcript SID: %s", data.get("transcript_sid"))
    return "Accepted", 202


@app.route("/call-events", methods=["POST"])
def log_call_event():
    """
    Endpoint for receiving Call Event webhook events.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.form.to_dict()
    logger.debug("Received Call Event webhook fire with data: %s", data)
    return "Accepted", 202


@app.route("/")
def hello_world():
    """Serve a simple static Hello World page at the root"""
    return "<h1>wbor-twilio is online!</h1>"


# Main


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
