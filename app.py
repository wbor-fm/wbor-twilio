"""
Twilio Handler.
- Publishes incoming messages to a RabbitMQ queue.
- Endpoint to send messages from our station number (behind password).
"""

import os
import logging
import json
import re
from threading import Thread
from functools import wraps
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl
import pika
import pika.exceptions
import pytz
from flask import Flask, abort, request
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
GROUPME_QUEUE = os.getenv("GROUPME_QUEUE", "groupme")
POSTGRES_QUEUE = os.getenv("POSTGRES_QUEUE", "postgres")

TWILIO_CHARACTER_LIMIT = 1600  # Twilio SMS character limit

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

app = Flask(__name__)

# Twilio client initialization
twilio_client = Client(
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, region="us1", edge="ashburn"
)
twilio_client.http_client.logger.setLevel(logging.INFO)


def publish_to_queue(queue_name, message):
    """
    Publishes a message to a RabbitMQ queue.
    
    Routing key is set as the queue name. Uses default exchange.

    Parameters:
    - queue_name (str): The name of the RabbitMQ queue to publish to.
    - message (dict): The message content, which will be converted to JSON format.
    """
    try:
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(host=RABBITMQ_HOST, credentials=credentials)
        )
        channel = connection.channel()

        # Declare the queue with the name provided (if it doesn't already exist)
        # Setting 'durable=True' makes sure that the queue survives server restarts
        channel.queue_declare(queue=queue_name, durable=True)

        # Publish message to queue
        channel.basic_publish(
            exchange="",
            routing_key=queue_name,  # Routing key determines which queue gets the message
            body=json.dumps(
                message
            ).encode(),  # Encodes msg as bytes. RabbitMQ requires byte data
            properties=pika.BasicProperties(
                delivery_mode=2,  # Persistent message - write to disk for safety
            ),
        )
        logger.info("Published SMS message to queue: %s. Message: %s", queue_name, message)
        connection.close()
    except pika.exceptions.AMQPConnectionError as conn_error:
        logger.error(
            "Connection error when publishing to queue %s: %s", queue_name, conn_error
        )
    except pika.exceptions.AMQPChannelError as chan_error:
        logger.error(
            "Channel error when publishing to queue %s: %s", queue_name, chan_error
        )
    except json.JSONDecodeError as json_error:
        logger.error("JSON encoding error for message %s: %s", message, json_error)


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


@app.route("/sms", methods=["GET", "POST"])
@validate_twilio_request
def receive_sms():
    """
    Handler for incoming SMS messages from Twilio. Publishes messages to RabbitMQ.

    Returns:
    - str: A TwiML response to acknowledge receipt of the message (required by Twilio).
    """
    sms_data = request.form.to_dict()
    logger.debug("Received SMS message with data: %s", sms_data)
    resp = MessagingResponse()  # Required by Twilio

    # Publish to queues in separate threads to avoid blocking
    Thread(target=publish_to_queue, args=(POSTGRES_QUEUE, sms_data)).start()
    Thread(target=publish_to_queue, args=(GROUPME_QUEUE, sms_data)).start()

    return str(resp)


@app.route("/send", methods=["GET"])
def send_sms():
    """
    Send an SMS message using the Twilio API from a browser address bar. Requires a password.
    
    Expects recipient_number to be in E.164 format, e.g. +12077253250.
    Encoding the `+` as %2B also works.
    """
    # Don't let strangers send messages as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        abort(403, "Unauthorized access")

    recipient_number = request.args.get('recipient_number', '').replace(' ', '+')
    message = request.args.get("message")

    if not recipient_number or not re.fullmatch(r'^\+?\d{10,15}$', recipient_number):
        logger.warning("Invalid recipient number format: %s", recipient_number)
        abort(400, "Invalid recipient number format (must use the E.164 standard)")
    if not message:
        logger.warning("Message body content missing")
        abort(400, "Message body text is required")
    if len(message) > TWILIO_CHARACTER_LIMIT:
        logger.warning("Message too long: %d characters", len(message))
        abort(400, "Message exceeds character limit")
    try:
        logger.debug("Attempting to send message to %s", recipient_number)
        msg = twilio_client.messages.create(
            to=recipient_number, from_=TWILIO_PHONE_NUMBER, body=message
        )
        logger.info("Message sent successfully. SID: %s", msg.sid)
        message = f"Message sent to {recipient_number}\nBody: {message}"
        return message

    except TwilioRestException as e:
        logger.error("Failed to send message: %s", str(e))
        abort(500, f"Failed to send message: {e}")


@app.route("/")
def hello_world():
    """Serve a simple static Hello World page at the root"""
    return "<h1>wbor-twilio is online!</h1>"


if __name__ == "__main__":
    logger.info("Twilio now ready.")
    app.run(host="0.0.0.0", port=APP_PORT)
