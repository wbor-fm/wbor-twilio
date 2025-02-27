# wbor-twilio

This repository contains a Flask application (run under Gunicorn) for handling Twilio API interactions and sending messages to a RabbitMQ exchange for consumption by other services.

## Why This Exists

1. **Receiving SMS**: The app receives inbound SMS from Twilio, validates authenticity, and forwards message data to RabbitMQ.  
2. **Sending SMS**: The app also exposes protected endpoints to queue outgoing SMS messages. These are consumed by an internal consumer that fires off the SMS through Twilio.

## Core Features

- Validates incoming webhooks using Twilio's request signature.
- Publishes inbound messages to RabbitMQ exchange with a structured routing key.
- Offers endpoints for sending, banning, or unbanning phone numbers via GET requests.
- Uses Redis to track acknowledgments for inbound messages (e.g. to verify that the message was consumed).

## Activity Diagrams

### Incoming Messages

![Incoming messages activity diagram](/diagrams/png/Inbound%20Messages.png)

### Outgoing Messages

![Incoming messages activity diagram](/diagrams/png/Outbound%20Messages.png)

## Code Overview

### Main Components

- **`app.py`**:  
  - Sets up a Flask application with multiple routes (`/sms`, `/send`, `/ban`, `/unban`, etc.).  
  - Handles inbound SMS with `@validate_twilio_request`.  
  - Publishes inbound messages to RabbitMQ on the `source.twilio.sms.incoming` routing key.  
  - Launches a consumer thread that listens for `source.twilio.sms.outgoing` messages, which are then sent via the Twilio API.  
  - Acknowledgments flow back through a `/acknowledge` endpoint.

- **`config.py`**:  
  - Holds environment variables and constants (Twilio credentials, RabbitMQ info, etc.).

- **`utils/`**:  
  - Utility modules for logging and Redis connectivity.  

### Important Details

- Twilio will retry or fall back to a secondary handler if this app doesn't return a 200 status.  
- The `/acknowledge` endpoint needs multiple Flask/Gunicorn workers or processes. Otherwise, the main process might block while waiting for the acknowledgment.  
- Inbound message requests are validated through the Twilio signature in `validate_twilio_request()`.  
- Outbound message sending uses a background consumer that reads from the RabbitMQ queue, calls Twilio, and logs the result.

## How to Run

1. Install dependencies:  

   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables
3. Ensure your RabbitMQ and Redis server is online
4. Launch with Docker (using `make`)

## Usage

### Inbound SMS

Configure your Twilio phone number's webhook to point at `/sms`. For example: `https://yourdomain.com/sms`

When Twilio sends an inbound SMS:

 1. The app verifies Twilio's signature.
 2. It publishes the message into RabbitMQ with routing key source.`twilio.sms.incoming`.
 3. It waits briefly for a downstream acknowledgment on `/acknowledge`.
 4. Responds with TwiML (or returns an error if acknowledgment isn't received in time, to let Twilio know to use its fallback).

### Sending an SMS

There are two main ways to send a text from this app:

- Direct RabbitMQ (publish your own message to `source.twilio.sms.outgoing`).
- Via the `/send` endpoint in your browser (GET request).
  - Example:

    ```
    GET https://yourdomain.com/send?password=secret123&recipient_number=%2B12025550123&body=Hello+WBOR
    ```

  - Must pass the correct password defined in the `.env`.
  - Must supply recipient_number in E.164 format (e.g. `+12025550123`).
  - Must supply body text within 1600 characters.
