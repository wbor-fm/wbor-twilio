"""
App configuration file. Load environment variables from .env file.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
APP_PORT = os.getenv("APP_PORT", "5000")
APP_PASSWORD = os.getenv("APP_PASSWORD")
SOURCE = "twilio"  # Define source name for RabbitMQ exchange purposes
OUTGOING_QUEUE = "outgoing_sms"
SMS_OUTGOING_KEY = "source.twilio.sms.outgoing"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_CHARACTER_LIMIT = 1600  # Twilio SMS character limit

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
RABBITMQ_EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "source_exchange")
RABBITMQ_DL_EXCHANGE = os.getenv("RABBITMQ_DL_EXCHANGE", "dead_letter_exchange")

REDIS_HOST = os.getenv("REDIS_HOST", "wbor-redis-server")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_ACK_EXPIRATION = int(os.getenv("REDIS_ACK_EXPIRATION", "60"))  # in seconds
