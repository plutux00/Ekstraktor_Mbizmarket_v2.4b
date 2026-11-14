#!/bin/bash
playwright install chromium
# Kalau perlu deps (mungkin gagal tanpa sudo, coba dulu):
playwright install-deps chromium || true
