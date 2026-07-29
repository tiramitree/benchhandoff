GO ?= go
GOFMT ?= gofmt
DOCKER ?= docker
MANAGER_IMAGE ?= benchhandoff-agentrun-controller:e2e
RUNNER_IMAGE ?= benchhandoff-runner:e2e
override BUILD_ROOT := .build
IMAGE_CONTEXT := $(BUILD_ROOT)/image
MANAGER_BINARY := $(IMAGE_CONTEXT)/manager
GOFMT_PATHS := api cmd internal

.PHONY: all build build-linux clean clean-e2e fmt-check image mod-check runner-image test verify vet

all: verify

fmt-check:
	@unformatted="$$($(GOFMT) -l $(GOFMT_PATHS))" || { \
		echo "gofmt failed"; \
		exit 1; \
	}; \
	if test -n "$$unformatted"; then \
		echo "Go files require gofmt:"; \
		printf '%s\n' "$$unformatted"; \
		exit 1; \
	fi

mod-check:
	$(GO) mod tidy -diff
	$(GO) mod verify

vet:
	$(GO) vet -mod=readonly ./...

test:
	$(GO) test -mod=readonly -race ./...

verify: fmt-check mod-check vet test

build:
	$(GO) build -mod=readonly -trimpath -o bin/manager ./cmd/manager

build-linux:
	mkdir -p $(IMAGE_CONTEXT)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
		$(GO) build -mod=readonly -trimpath -ldflags="-s -w -buildid=" \
		-o $(MANAGER_BINARY) ./cmd/manager

image: build-linux
	$(DOCKER) build --file Dockerfile --tag $(MANAGER_IMAGE) $(IMAGE_CONTEXT)

runner-image:
	$(DOCKER) build --file runner.Dockerfile --tag $(RUNNER_IMAGE) ..

clean:
	rm -rf -- bin .build

clean-e2e:
	rm -rf -- .build
