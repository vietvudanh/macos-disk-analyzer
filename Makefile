SHELL := /bin/zsh
export PATH := $(HOME)/.local/bin:/opt/homebrew/bin:/usr/local/bin:$(PATH)

.PHONY: all scan snapshot snapshots compare compare-export analyze analyze-rs map clean help schedule-install schedule-uninstall schedule-status schedule-run compress-snapshots parquet duckdb sql diff-shell

# Variables
PYTHON ?= /usr/bin/python3
DUCKDB ?= $(shell if [ -x ./bin/duckdb ]; then echo ./bin/duckdb; elif which duckdb >/dev/null 2>&1; then which duckdb; elif [ -x $(HOME)/.local/bin/duckdb ]; then echo $(HOME)/.local/bin/duckdb; elif [ -x /opt/homebrew/bin/duckdb ]; then echo /opt/homebrew/bin/duckdb; elif [ -x /usr/local/bin/duckdb ]; then echo /usr/local/bin/duckdb; else echo duckdb; fi)
HOSTNAME := $(shell hostname)
DATE := $(shell date +"%Y-%m-%d_%H%M%S")
ROOT_DIR := $(shell pwd)
DATA_DIR := data/$(HOSTNAME)
SNAPSHOTS_DIR := $(DATA_DIR)/snapshots
NCDU_OUTPUT := $(DATA_DIR)/disk_usage.json.zst
SNAPSHOT_OUTPUT := $(SNAPSHOTS_DIR)/disk_usage_$(DATE).json.zst

# LaunchDaemon Schedule configuration
PLIST_LABEL := com.vietvu.macos-disk-analyzer.snapshot
PLIST_SRC := scripts/$(PLIST_LABEL).plist
PLIST_DEST := /Library/LaunchDaemons/$(PLIST_LABEL).plist

all: snapshot analyze

## scan: Scan the disk and capture a dated compressed snapshot (requires sudo)
scan: snapshot

## snapshot: Capture a dated disk usage snapshot with ncdu using Zstandard compression (requires sudo if not root)
snapshot:
	@mkdir -p $(SNAPSHOTS_DIR)
	@echo "Starting disk scan with ncdu (Zstandard compression)..."
	@if [ "$$(id -u)" -eq 0 ]; then \
		ncdu -c -o $(SNAPSHOT_OUTPUT) /; \
	else \
		sudo ncdu -c -o $(SNAPSHOT_OUTPUT) /; \
	fi
	@ln -sf snapshots/disk_usage_$(DATE).json.zst $(NCDU_OUTPUT) 2>/dev/null || cp $(SNAPSHOT_OUTPUT) $(NCDU_OUTPUT)
	@chmod 644 $(SNAPSHOT_OUTPUT) $(NCDU_OUTPUT) 2>/dev/null || true
	@echo "Compressed snapshot saved to $(SNAPSHOT_OUTPUT)"
	@echo "Updated latest pointer $(NCDU_OUTPUT)"

## snapshots: List all available disk usage snapshots with dates, format, and sizes
snapshots:
	@$(PYTHON) compare_ncdu.py --list

## compress-snapshots: Compress any raw uncompressed .json snapshots to .json.zst
compress-snapshots:
	@$(PYTHON) compare_ncdu.py --compress-all

## parquet: Convert latest snapshot to Parquet format via DuckDB
parquet:
	@$(PYTHON) compare_ncdu.py --to-parquet

## duckdb: Launch interactive DuckDB SQL shell on the latest Parquet snapshot
duckdb:
	@if [ ! -f $(DATA_DIR)/disk_usage.parquet ]; then \
		$(MAKE) parquet; \
	fi
	@$(DUCKDB) -dark-mode $(DATA_DIR)/disk_usage.parquet

## sql: Run an ad-hoc SQL query on the Parquet snapshot (e.g. make sql QUERY="SELECT ...")
sql:
	@if [ ! -f $(DATA_DIR)/disk_usage.parquet ]; then \
		$(MAKE) parquet; \
	fi
	@$(DUCKDB) -dark-mode -c "$(if $(QUERY),$(QUERY),SELECT category, count(*) as items, ROUND(SUM(size_bytes)/1024/1024/1024, 2) as gb FROM '$(DATA_DIR)/disk_usage.parquet' WHERE category != 'other' GROUP BY category ORDER BY gb DESC;)"

## compare: Compare two snapshots over time (default: latest two)
compare:
	@$(PYTHON) compare_ncdu.py $(if $(BEFORE),--before "$(BEFORE)") $(if $(AFTER),--after "$(AFTER)") $(if $(TOP),--top $(TOP)) $(if $(MIN),--min-size $(MIN)) $(if $(HOST),--host $(HOST))

## compare-export: Compare snapshots and export detailed diff files (increased, decreased, new, deleted, same)
compare-export:
	@$(PYTHON) compare_ncdu.py --export $(if $(BEFORE),--before "$(BEFORE)") $(if $(AFTER),--after "$(AFTER)") $(if $(TOP),--top $(TOP)) $(if $(MIN),--min-size $(MIN)) $(if $(HOST),--host $(HOST))

## diff-shell: Open interactive DuckDB SQL shell on the diff view between snapshots
diff-shell:
	@$(PYTHON) compare_ncdu.py --diff-shell $(if $(BEFORE),--before "$(BEFORE)") $(if $(AFTER),--after "$(AFTER)") $(if $(HOST),--host $(HOST))

## analyze: Run the analysis script on the scanned data
analyze:
	@if [ ! -f $(NCDU_OUTPUT) ] && [ ! -f $(DATA_DIR)/disk_usage.json ] && [ ! -f $(DATA_DIR)/disk_usage.json.gz ]; then \
		echo "Error: no snapshot found in $(DATA_DIR). Run 'make scan' or 'make snapshot' first."; \
		exit 1; \
	fi
	@echo "Analyzing latest snapshot..."
	uvx --with tqdm python analyze_ncdu_v2.py

## analyze-rs: Run the Rust analysis binary (faster, same output)
analyze-rs:
	@if [ ! -f $(NCDU_OUTPUT) ] && [ ! -f $(DATA_DIR)/disk_usage.json ] && [ ! -f $(DATA_DIR)/disk_usage.json.gz ]; then \
		echo "Error: no snapshot found in $(DATA_DIR). Run 'make scan' or 'make snapshot' first."; \
		exit 1; \
	fi
	@cargo build --release --manifest-path rs/Cargo.toml
	@echo "Analyzing latest snapshot..."
	@./rs/target/release/analyze-ncdu

## map: Build the interactive disk map + cleanup suggestions
map:
	@if [ ! -f $(NCDU_OUTPUT) ] && [ ! -f $(DATA_DIR)/disk_usage.json ] && [ ! -f $(DATA_DIR)/disk_usage.json.gz ]; then \
		echo "Error: no snapshot found in $(DATA_DIR). Run 'make scan' or 'make snapshot' first."; \
		exit 1; \
	fi
	uvx python disk_map.py
	open $(DATA_DIR)/disk_map.html

## schedule-install: Install and load the daily snapshot LaunchDaemon at 02:00 AM (requires sudo)
schedule-install:
	@echo "Configuring daily LaunchDaemon at 02:00 AM..."
	@chmod +x scripts/run_daily_snapshot.sh
	@sed -e "s|/Users/vietvu/works/github.com/vietvudanh/macos-disk-analyzer|$(ROOT_DIR)|g" $(PLIST_SRC) > /tmp/$(PLIST_LABEL).plist
	@echo "Installing $(PLIST_LABEL) to /Library/LaunchDaemons/ (requires sudo)..."
	sudo cp /tmp/$(PLIST_LABEL).plist $(PLIST_DEST)
	sudo chown root:wheel $(PLIST_DEST)
	sudo chmod 644 $(PLIST_DEST)
	@rm -f /tmp/$(PLIST_LABEL).plist
	@echo "Loading LaunchDaemon into system..."
	-sudo launchctl bootout system/$(PLIST_LABEL) 2>/dev/null || sudo launchctl unload $(PLIST_DEST) 2>/dev/null
	sudo launchctl bootstrap system $(PLIST_DEST) 2>/dev/null || sudo launchctl load -w $(PLIST_DEST)
	@echo "Daily snapshot successfully scheduled for 02:00 AM!"
	@echo "Logs will be recorded in: data/snapshot.log"
	@echo "Run 'make schedule-status' to check daemon status."

## schedule-uninstall: Stop and remove the daily snapshot LaunchDaemon (requires sudo)
schedule-uninstall:
	@echo "Unloading LaunchDaemon (requires sudo)..."
	-sudo launchctl bootout system/$(PLIST_LABEL) 2>/dev/null || sudo launchctl unload $(PLIST_DEST) 2>/dev/null
	sudo rm -f $(PLIST_DEST)
	@echo "Daily snapshot schedule removed."

## schedule-status: Check the status of the daily snapshot LaunchDaemon and show latest logs
schedule-status:
	@echo "Checking LaunchDaemon status..."
	@if [ -f $(PLIST_DEST) ]; then \
		echo "Installed at: $(PLIST_DEST)"; \
		sudo launchctl print system/$(PLIST_LABEL) 2>/dev/null || sudo launchctl list | grep $(PLIST_LABEL) || echo "Service loaded"; \
	else \
		echo "LaunchDaemon is NOT installed. Run 'make schedule-install' to enable."; \
	fi
	@echo ""
	@if [ -f data/snapshot.log ]; then \
		echo "=== Last 20 lines of data/snapshot.log ==="; \
		tail -n 20 data/snapshot.log; \
	else \
		echo "No snapshot log found yet at data/snapshot.log"; \
	fi

## schedule-run: Manually trigger the daily snapshot runner now (requires sudo)
schedule-run:
	@echo "Running snapshot via runner script..."
	sudo ./scripts/run_daily_snapshot.sh

## clean: Remove the data directory
clean:
	@echo "Removing all data..."
	rm -rf data/

## help: Show this help message
help:
	@echo "Usage: make [target] [VARIABLES...]"
	@echo ""
	@echo "Targets:"
	@echo "  snapshot           Capture a new dated compressed snapshot with ncdu (requires sudo)"
	@echo "  scan               Alias for snapshot"
	@echo "  snapshots          List all available snapshots across hosts and dates"
	@echo "  compress-snapshots Compress any raw uncompressed .json snapshots to .json.zst"
	@echo "  parquet            Convert latest snapshot into Parquet format using DuckDB"
	@echo "  duckdb             Open interactive DuckDB SQL shell on latest Parquet snapshot"
	@echo "  sql                Run ad-hoc SQL query on Parquet snapshot (e.g. make sql QUERY=\"...\")"
	@echo "  compare            Compare two snapshots over time using DuckDB FULL OUTER JOIN"
	@echo "  compare-export     Compare snapshots and export diff.parquet and detailed text files"
	@echo "  diff-shell         Launch interactive DuckDB SQL shell on snapshot diff view"
	@echo "  analyze            Run the analysis script on the latest scanned data"
	@echo "  analyze-rs         Run the Rust binary on the latest scanned data"
	@echo "  map                Build the interactive disk map (data/<host>/disk_map.html)"
	@echo "  schedule-install   Install daily 02:00 AM LaunchDaemon (requires sudo)"
	@echo "  schedule-uninstall Remove daily LaunchDaemon (requires sudo)"
	@echo "  schedule-status    Check LaunchDaemon status and show recent logs"
	@echo "  schedule-run       Run the scheduled snapshot script immediately (requires sudo)"
	@echo "  all                Run both snapshot and analyze"
	@echo "  clean              Remove the data directory"
	@echo "  help               Show this help message"
	@echo ""
	@echo "Examples:"
	@echo "  make snapshots"
	@echo "  make compare"
	@echo "  make compare-export"
	@echo "  make diff-shell"
	@echo "  make parquet"
	@echo "  make sql"
	@echo "  make sql QUERY=\"SELECT extension, count(*), ROUND(SUM(size_bytes)/1e9, 2) as gb FROM 'data/VietVu-02/disk_usage.parquet' WHERE NOT is_dir GROUP BY extension ORDER BY gb DESC LIMIT 10;\""
	@echo "  sudo make schedule-install"
	@echo "  make schedule-status"

