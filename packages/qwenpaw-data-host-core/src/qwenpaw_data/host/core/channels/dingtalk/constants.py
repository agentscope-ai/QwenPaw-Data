# -*- coding: utf-8 -*-
"""DingTalk channel constants."""

# Dedup cache cap (message_id -> None, FIFO eviction)
DINGTALK_PROCESSED_IDS_MAX = 1000

# session_id short-suffix length (last 8 chars of conversation_id)
DINGTALK_SESSION_ID_SUFFIX_LEN = 8

# Max chars per markdown message sent via sessionWebhook (degrade to truncated text if exceeded)
DINGTALK_MARKDOWN_MAX_CHARS = 3500

# WebSocket reconnect backoff
DINGTALK_WS_INITIAL_RETRY_DELAY = 1.0
DINGTALK_WS_MAX_RETRY_DELAY = 60.0
DINGTALK_WS_BACKOFF_FACTOR = 2

# Minimum interval between AI-card streaming updates (seconds). The DingTalk
# /v1.0/card/streaming endpoint is rate-limited per card; 0.2s is conservative.
DINGTALK_STREAM_MIN_INTERVAL_S = 0.2
