# -*- coding: utf-8 -*-
"""Feishu channel constants."""

# Dedup cache cap (message_id -> None, FIFO eviction)
FEISHU_PROCESSED_IDS_MAX = 1000

# session_id short-suffix length (last 8 chars of chat_id / open_id)
FEISHU_SESSION_ID_SUFFIX_LEN = 8

# WebSocket reconnect backoff (exponential)
FEISHU_WS_INITIAL_RETRY_DELAY = 1.0
FEISHU_WS_MAX_RETRY_DELAY = 60.0
FEISHU_WS_BACKOFF_FACTOR = 2

# ---- CardKit streaming card ----

# Minimum interval between streaming updates (seconds). CardKit allows 10 QPS per card; conservatively 0.15.
FEISHU_STREAM_MIN_INTERVAL_S = 0.15

# element_id of the markdown component inside the streaming card.
FEISHU_STREAM_ELEMENT_ID = "streaming_content"
