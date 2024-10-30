# wbor-twilio

Basic Flask + Gunicorn server that deals with Twilio related operations:

- Receiving SMS messages and subsequently forwarding them to RabbitMQ for distribution to consuming services
- Sending SMS messages from our station's number

## Activity Diagrams

### Incoming Messages
![Incoming messages activity diagram](/diagrams/diagrams/Inbound%20Messages.png)

### Outgoing Messages
![Incoming messages activity diagram](/diagrams/diagrams/Outbound%20Messages.png)
