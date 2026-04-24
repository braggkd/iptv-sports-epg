#!/bin/bash
# Wrapper for scheduled EPG generation + publish to GitHub.
# Called by launchd; safe to invoke manually too.
#
# - Regenerates epg.xml from provider
# - Refuses to commit if output isn't well-formed XML
# - Skips commit if nothing changed
# - Logs everything to logs/epg.log

set -u

REPO_DIR="/Users/kylebragg/Library/CloudStorage/GoogleDrive-kyle@oneworldarchitecture.com/My Drive/Kyle/IPTV EPG/claude_epg"
PYTHON="/usr/bin/python3"
GIT="/usr/bin/git"
LOG_DIR="${REPO_DIR}/logs"
LOG_FILE="${LOG_DIR}/epg.log"

mkdir -p "${LOG_DIR}"

{
    echo "============================================================"
    echo "Run started: $(date -Iseconds)"

    cd "${REPO_DIR}" || { echo "FATAL: cannot cd to repo"; exit 2; }

    # 1. Generate
    if ! "${PYTHON}" generate_epg.py --output epg.xml --save-cache --cache-dir cache --categories 911; then
        echo "FATAL: generate_epg.py failed"
        exit 3
    fi

    # 2. Sanity-check the output before we let git near it
    if ! "${PYTHON}" -c "import xml.etree.ElementTree as ET; ET.parse('epg.xml')" 2>/dev/null; then
        echo "FATAL: epg.xml is not well-formed XML; refusing to commit"
        exit 4
    fi

    # 3. Stage and see if there's anything to commit
    "${GIT}" add epg.xml
    if "${GIT}" diff --cached --quiet -- epg.xml; then
        echo "No changes to epg.xml; skipping commit"
        echo "Run finished: $(date -Iseconds)"
        exit 0
    fi

    # 4. Commit + push
    MSG="epg refresh $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! "${GIT}" commit -m "${MSG}"; then
        echo "FATAL: git commit failed"
        exit 5
    fi

    if ! "${GIT}" push origin main; then
        echo "FATAL: git push failed (credentials / network?)"
        exit 6
    fi

    echo "Pushed: ${MSG}"
    echo "Run finished: $(date -Iseconds)"
} >> "${LOG_FILE}" 2>&1
