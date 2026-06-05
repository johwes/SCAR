REGISTRY ?= quay.io
ORG      ?= jwesterl
TAG      ?= latest

IKOS_IMAGE  := $(REGISTRY)/$(ORG)/scar-ikos:$(TAG)
AGENT_IMAGE := $(REGISTRY)/$(ORG)/scar-agent:$(TAG)

.PHONY: build push build-ikos build-agent push-ikos push-agent

build: build-ikos build-agent

build-ikos:
	docker build --no-cache -t $(IKOS_IMAGE) containers/ikos/

build-agent:
	docker build --no-cache -t $(AGENT_IMAGE) containers/scar/

push: push-ikos push-agent

push-ikos:
	docker push $(IKOS_IMAGE)

push-agent:
	docker push $(AGENT_IMAGE)
