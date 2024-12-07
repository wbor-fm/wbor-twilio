IMAGE_NAME = wbor-twilio-image
CONTAINER_NAME = wbor-twilio
NETWORK_NAME = wbor-network
HOST_DIR = "/var/wbor-twilio"

default: clean build run logsf

q: clean build run

exec:
	docker exec -it $(CONTAINER_NAME) /bin/bash

logsf:
	docker logs -f $(CONTAINER_NAME)

build:
	@echo "Building..."
	nice -n 10 docker buildx build -q -t $(IMAGE_NAME) .

run: stop
	docker run -d --network $(NETWORK_NAME) --name $(CONTAINER_NAME) -v ${HOST_DIR}/logs:/app/logs $(IMAGE_NAME)

stop:
	@echo "Checking if container $(CONTAINER_NAME) is running..."
	@if [ "$$(docker ps -a -q -f name=$(CONTAINER_NAME))" != "" ]; then \
		echo "Stopping $(CONTAINER_NAME)..."; \
		docker stop $(CONTAINER_NAME) > /dev/null; \
		echo "Removing the container $(CONTAINER_NAME)..."; \
		docker rm -f $(CONTAINER_NAME) > /dev/null; \
	else \
		echo "No running container with name $(CONTAINER_NAME) found."; \
	fi

clean: stop
	@IMAGE_ID=$$(docker images -q $(IMAGE_NAME)); \
	if [ "$$IMAGE_ID" ]; then \
		echo "Removing image $(IMAGE_NAME) with ID $$IMAGE_ID..."; \
		docker rmi $$IMAGE_ID > /dev/null; \
	else \
		echo "No image found with name $(IMAGE_NAME)."; \
	fi
