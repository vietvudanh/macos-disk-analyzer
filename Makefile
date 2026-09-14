.PHONY: all scan analyze analyze-rs clean help

# Variables
HOSTNAME := $(shell hostname)
DATA_DIR := data/$(HOSTNAME)
NCDU_OUTPUT := $(DATA_DIR)/disk_usage.json

all: scan analyze

## scan: Scan the disk with ncdu (requires sudo)
scan:
	@mkdir -p $(DATA_DIR)
	@echo "Starting disk scan with ncdu (requires sudo)..."
	sudo ncdu -o $(NCDU_OUTPUT) /

## analyze: Run the analysis script on the scanned data
analyze:
	@if [ ! -f $(NCDU_OUTPUT) ]; then \
		echo "Error: $(NCDU_OUTPUT) not found. Run 'make scan' first."; \
		exit 1; \
	fi
	@echo "Analyzing $(NCDU_OUTPUT)..."
	uvx --with tqdm python analyze_ncdu_v2.py

## analyze-rs: Run the Rust analysis binary (faster, same output)
analyze-rs:
	@if [ ! -f $(NCDU_OUTPUT) ]; then \
		echo "Error: $(NCDU_OUTPUT) not found. Run 'make scan' first."; \
		exit 1; \
	fi
	@cargo build --release --manifest-path rs/Cargo.toml
	@echo "Analyzing $(NCDU_OUTPUT)..."
	@./rs/target/release/analyze-ncdu

## clean: Remove the data directory
clean:
	@echo "Removing all data..."
	rm -rf data/

## help: Show this help message
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  scan    Run ncdu to scan the disk (requires sudo)"
	@echo "  analyze Run the analysis script on the scanned data"
	@echo "  analyze-rs Run the Rust binary (faster, same output)"
	@echo "  all     Run both scan and analyze"
	@echo "  clean   Remove the data directory"
	@echo "  help    Show this help message"
