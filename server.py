import ctypes
import colorsys
import hashlib
import http.server
import socketserver
import json
import math
import os
import re
import statistics
import sqlite3
import base64
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import heapq
import bisect
from datetime import datetime, timezone, timedelta

import aa_sync


def is_cloud_offline_file(file_path):
    """Check if file is a cloud placeholder (OneDrive Files On-Demand) to avoid triggering auto-download."""
    if os.name != 'nt' or not file_path:
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(file_path))
        if attrs == -1:
            return False
        # 0x00400000 = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        # 0x00001000 = FILE_ATTRIBUTE_OFFLINE
        # 0x00040000 = FILE_ATTRIBUTE_RECALL_ON_OPEN
        if attrs & (0x00400000 | 0x00001000 | 0x00040000):
            return True
    except Exception:
        pass
    return False


PORT = 5050
SERVER_HOST = '127.0.0.1'
IDE_BRAIN_DIR = os.path.expanduser(os.path.join('~', '.gemini', 'antigravity-ide', 'brain'))
CLI_BRAIN_DIR = os.path.expanduser(os.path.join('~', '.gemini', 'antigravity-cli', 'brain'))
GEMINI_CLI_DIR = os.path.expanduser(os.path.join('~', '.gemini', 'tmp'))
GEMINI_WORKER_STATE_DIR = os.environ.get(
    'GEMINI_WORKER_STATE_DIR',
    os.path.expanduser(os.path.join('~', 'Documents', 'CodexLocal', 'gemini-worker', 'state')),
)
BRAIN_DIR = IDE_BRAIN_DIR
CODEX_SESSIONS_DIR = os.path.expanduser(os.path.join('~', '.codex', 'sessions'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, 'accounts.json')
CODEX_USAGE_FILE = os.path.join(BASE_DIR, 'codex_usage.json')
CODEX_MODELS_CACHE_FILE = os.path.join(BASE_DIR, 'codex_models_cache.json')
# Account IDs are private local metadata; the web server serves BASE_DIR.
CODEX_QUOTA_IDENTITY_FILE = os.path.expanduser(os.path.join('~', '.codex', 'usage-tracker-quota-identity.json'))
CODEX_RUNTIME_MODELS_CACHE_FILE = os.path.expanduser(os.path.join('~', '.codex', 'models_cache.json'))
CODEX_MISSION_TURNS_CACHE_FILE = os.path.join(BASE_DIR, 'codex_mission_turns_cache.json')
CODEX_MISSION_REVIEWS_FILE = os.path.join(BASE_DIR, 'codex_mission_reviews.json')
AA_BENCHMARK_CACHE_FILE = os.path.join(BASE_DIR, 'aa_benchmarks_cache.json')
# Keep credentials outside SimpleHTTPRequestHandler's served project root.
AA_API_KEY_FILE = os.path.expanduser(os.path.join('~', '.codex', 'usage-tracker-aa-api-key.txt'))
CODEX_MISSION_TURNS_CACHE_VERSION = 9
CODEX_MODELS_CACHE_VERSION = 7
QUOTA_OBSERVATIONS_FILE = os.path.join(BASE_DIR, 'quota_observations.json')
REAL_QUOTAS_FILE = os.path.join(BASE_DIR, 'real_quotas.json')
VSCDB_PATH = os.path.expanduser(os.path.join('~', 'AppData', 'Roaming', 'Antigravity IDE', 'User', 'globalStorage', 'state.vscdb'))

TRANSCRIPT_SOURCES = {
    'ide': {
        'key': 'ide',
        'label': 'Antigravity IDE',
        'path': IDE_BRAIN_DIR,
        'format': 'antigravity',
        'kind': 'ide',
    },
    'cli': {
        'key': 'cli',
        'label': 'Antigravity CLI',
        'path': CLI_BRAIN_DIR,
        'format': 'antigravity',
        'kind': 'cli',
    },
    'gemini_cli': {
        'key': 'gemini_cli',
        'label': 'Gemini CLI (Official)',
        'path': GEMINI_CLI_DIR,
        'format': 'gemini_cli',
        'kind': 'official_cli',
    },
}


def get_transcript_sources(sources=None):
    """Return dictionary of configured transcript sources, respecting runtime overrides."""
    if sources is not None:
        return {k: dict(v) for k, v in sources.items()}
    raw_sources = globals().get('TRANSCRIPT_SOURCES')
    if raw_sources is None:
        raw_sources = TRANSCRIPT_SOURCES
    out = {}
    for k, v in dict(raw_sources).items():
        out[k] = dict(v)
    return out


# Quota observations are expressed in the token units produced by this
# estimator. Version the unit explicitly so a future tokenizer upgrade cannot
# silently mix incompatible historical deltas into one calibration slope.
LEGACY_TOKEN_ESTIMATOR_ID = 'chars_div_3_v1'
TOKEN_ESTIMATOR_ID = LEGACY_TOKEN_ESTIMATOR_ID
GEMINI_WORKER_ESTIMATOR_ID = 'gemini_worker_report_total_tokens_v1'

# The HTTP server is multi-threaded, while the tracker persists several shared
# JSON documents. Keep every mutation serialized and replace files atomically so
# readers can never observe a half-written document.
PERSISTENCE_LOCK = threading.RLock()
CODEX_SCAN_LOCK = threading.RLock()


def _safe_nonnegative_int(v, default=0):
    """Safely extract a non-negative finite integer from arbitrary JSON values."""
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
        if math.isfinite(f) and f >= 0:
            return int(f)
    except Exception:
        pass
    return default


def _compute_file_prefix_hash(file_path, offset, chunk_bytes=1024 * 1024):
    """Hash the complete durable prefix so append detection cannot miss rewrites."""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset <= 0:
        return ""
    try:
        h = hashlib.sha256()
        remaining = offset
        chunk_bytes = max(4096, int(chunk_bytes))
        with open(file_path, 'rb') as f:
            while remaining > 0:
                chunk = f.read(min(chunk_bytes, remaining))
                if not chunk:
                    return ""
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _to_hashable(val):
    """Recursively convert nested lists to tuples so items can be placed in sets."""
    if isinstance(val, (list, tuple)):
        return tuple(_to_hashable(x) for x in val)
    return val


def _to_json_safe(val):
    """Recursively convert nested tuples and sets to lists for JSON serialization."""
    if isinstance(val, (list, tuple, set)):
        return [_to_json_safe(x) for x in val]
    return val


def _load_seen_request_ids(raw_entries):
    """Load only the narrow request-id -> latest usage-shape cache schema."""
    out = {}
    if not isinstance(raw_entries, list):
        return out
    for entry in raw_entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        request_id, usage_shape = entry
        if not isinstance(request_id, str) or not request_id:
            continue
        if not isinstance(usage_shape, (list, tuple)) or len(usage_shape) != 6:
            continue
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                   for value in usage_shape):
            continue
        out[request_id] = tuple(usage_shape)
    return out


def _serialize_seen_request_ids(entries, limit=200):
    return [
        [request_id, list(usage_shape)]
        for request_id, usage_shape in list(entries.items())[-limit:]
    ]


def _valid_codex_cache_payload(entry):
    """Reject malformed aggregate payloads so a corrupt cache is rebuilt safely."""
    if not isinstance(entry, dict):
        return False
    all_time = entry.get('all_time')
    recent = entry.get('recent_events')
    quota_observations = entry.get('quota_observations')
    finished_turn_ids = entry.get('finished_turn_ids')
    previous = entry.get('prev_cumulative')
    if (not isinstance(all_time, dict) or not isinstance(recent, list) or
            not isinstance(quota_observations, list) or not isinstance(finished_turn_ids, list) or
            not isinstance(previous, dict)):
        return False
    numeric_fields = (
        'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'thinking_tokens', 'total_tokens', 'responses', 'tool_calls',
    )
    def valid_number(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool) and
                math.isfinite(float(value)) and value >= 0)

    for key, stats in all_time.items():
        if not isinstance(key, str) or not isinstance(stats, dict):
            return False
        if any(not valid_number(stats.get(field, 0)) for field in numeric_fields):
            return False
    recent_numeric = ('in', 'cached_in', 'cache_write_in', 'out', 'think', 'tot')
    for event in recent:
        if not isinstance(event, dict) or not isinstance(event.get('model_key'), str):
            return False
        if not isinstance(event.get('task_id'), str):
            return False
        if any(not valid_number(event.get(field, 0)) for field in recent_numeric):
            return False
    for event in quota_observations:
        if (not isinstance(event, dict) or not isinstance(event.get('model_key'), str) or
                not isinstance(event.get('session'), str) or not isinstance(event.get('ts'), str) or
                not isinstance(event.get('resets_at'), str) or not isinstance(event.get('task_id'), str)):
            return False
        if not valid_number(event.get('used_percent', 0)) or not valid_number(event.get('total_tokens', 0)):
            return False
    if any(not isinstance(turn_id, str) for turn_id in finished_turn_ids):
        return False
    return True


def _read_json_file(path, default):
    """Read a JSON snapshot under the persistence lock."""
    with PERSISTENCE_LOCK:
        if not os.path.exists(path):
            return default() if callable(default) else default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default() if callable(default) else default


def _atomic_write_text(path, text):
    """Durably replace a text file without exposing partial contents."""
    with PERSISTENCE_LOCK:
        directory = os.path.dirname(path) or BASE_DIR
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f'.{os.path.basename(path)}.', suffix='.tmp', dir=directory
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass
            raise


def _atomic_write_json(path, data):
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _mutate_json_file(path, default, mutator):
    """Run a complete read-modify-write transaction while holding one lock."""
    with PERSISTENCE_LOCK:
        data = _read_json_file(path, default)
        replacement = mutator(data)
        if replacement is not None:
            data = replacement
        _atomic_write_json(path, data)
        return data


def _write_data_js(data):
    payload = 'const USAGE_DATA = ' + json.dumps(data, ensure_ascii=False, indent=2) + ';'
    _atomic_write_text(os.path.join(BASE_DIR, 'data.js'), payload)


_LIVE_STATIC_SNAPSHOT_WRITTEN = False


def _write_live_static_snapshot_once(data):
    """Keep the offline fallback fresh after the first successful live scan."""
    global _LIVE_STATIC_SNAPSHOT_WRITTEN
    if _LIVE_STATIC_SNAPSHOT_WRITTEN:
        return
    with PERSISTENCE_LOCK:
        if not _LIVE_STATIC_SNAPSHOT_WRITTEN:
            _write_data_js(data)
            _LIVE_STATIC_SNAPSHOT_WRITTEN = True


def _serialize_persistence(func):
    """Serialize a mutating workflow without changing its call signature."""
    def wrapped(*args, **kwargs):
        with PERSISTENCE_LOCK:
            return func(*args, **kwargs)
    wrapped.__name__ = getattr(func, '__name__', 'wrapped')
    wrapped.__doc__ = getattr(func, '__doc__')
    return wrapped

def estimate_tokens(text):
    """Accurate token estimate for mixed languages (~3 chars per token)"""
    if not text:
        return 0
    return max(1, len(text) // 3)

# Local benchmark metadata plus official provider pricing/context metadata.
# Benchmark-like scores are estimates unless a per-entry benchmark_source says otherwise.
BENCHMARK_DATABASE = {
    'Gemini 3.8 Flash (High)': {
        'provider': 'Google',
        'intelligence_index': 58.5,
        'coding_score': 90.5,
        'reasoning_score': 91.0,
        'speed_tps': 365.0,
        'ttft_sec': 0.38,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.380,
        'context_window': '1M tokens',
        'badge': '⚡ Phiên Bản Mới Nhất (High Thinking)',
        'best_for': 'Tác vụ thế hệ mới với tốc độ vượt trội, xử lý reasoning chuyên sâu.'
    },
    'Gemini 3.8 Flash': {
        'provider': 'Google',
        'intelligence_index': 57.0,
        'coding_score': 89.5,
        'reasoning_score': 90.0,
        'speed_tps': 380.0,
        'ttft_sec': 0.35,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.350,
        'context_window': '1M tokens',
        'badge': '⚡ Phiên Bản Mới Nhất (Standard)',
        'best_for': 'Lập trình và xử lý văn bản tốc độ cao thế hệ 3.8.'
    },
    'Gemini 3.7 Flash (High)': {
        'provider': 'Google',
        'intelligence_index': 56.0,
        'coding_score': 88.6,
        'reasoning_score': 89.2,
        'speed_tps': 340.1,
        'ttft_sec': 0.42,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.402,
        'context_window': '1M tokens',
        'badge': '🏆 Tốc Độ Vượt Trội (#1 Speed)',
        'best_for': 'Tác vụ đòi hỏi tốc độ cực nhanh, xử lý codebase lớn với context 1M.'
    },
    'Claude Opus 4.6 (Thinking)': {
        'provider': 'Anthropic',
        'intelligence_index': 63.5,
        'coding_score': 93.8,
        'reasoning_score': 94.5,
        'speed_tps': 61.9,
        'ttft_sec': 1.85,
        'price_in_1m': 15.00,
        'price_out_1m': 75.00,
        'cost_per_task': 2.337,
        'context_window': '200K tokens',
        'badge': '🧠 Trí Tuệ & Code Chuyên Sâu (#1 Intelligence)',
        'best_for': 'Tái cấu trúc kiến trúc phức tạp, bài toán suy luận khó và logic sâu.'
    },
    'Gemini 3.5 Flash (High)': {
        'provider': 'Google',
        'intelligence_index': 52.8,
        'coding_score': 84.1,
        'reasoning_score': 85.0,
        'speed_tps': 245.0,
        'ttft_sec': 0.51,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.350,
        'context_window': '1M tokens',
        'badge': '⚖️ Cân Bằng Hoàn Hảo (High Thinking)',
        'best_for': 'Lập trình hàng ngày với chế độ suy luận cao, chi phí tối ưu.'
    },
    'Gemini 3.5 Flash (Medium)': {
        'provider': 'Google',
        'intelligence_index': 48.5,
        'coding_score': 80.2,
        'reasoning_score': 79.8,
        'speed_tps': 285.0,
        'ttft_sec': 0.45,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.280,
        'context_window': '1M tokens',
        'badge': '💰 Tiết Kiệm & Nhanh Gọn (High Value)',
        'best_for': 'Hỏi đáp nhanh, tìm kiếm, tra cứu tài liệu và tác vụ phụ.'
    },
    'Gemini 2.5 Pro': {
        'provider': 'Google',
        'intelligence_index': 60.0,
        'coding_score': 92.0,
        'reasoning_score': 92.5,
        'speed_tps': 180.0,
        'ttft_sec': 0.65,
        'price_in_1m': 1.25,
        'price_out_1m': 5.00,
        'cost_per_task': 0.650,
        'context_window': '1M tokens',
        'badge': '🧠 Gemini 2.5 Pro (Official CLI)',
        'best_for': 'Lập trình chuyên sâu, suy luận và xử lý tác vụ phức tạp trong Gemini CLI.'
    },
    'Gemini 2.5 Flash': {
        'provider': 'Google',
        'intelligence_index': 54.0,
        'coding_score': 86.0,
        'reasoning_score': 86.5,
        'speed_tps': 320.0,
        'ttft_sec': 0.38,
        'price_in_1m': 0.15,
        'price_out_1m': 0.60,
        'cost_per_task': 0.320,
        'context_window': '1M tokens',
        'badge': '⚡ Gemini 2.5 Flash (Official CLI)',
        'best_for': 'Tốc độ cao và chi phí tối ưu cho chat và code trong Gemini CLI.'
    },
    'Gemini 2.0 Flash': {
        'provider': 'Google',
        'intelligence_index': 53.0,
        'coding_score': 84.5,
        'reasoning_score': 85.0,
        'speed_tps': 300.0,
        'ttft_sec': 0.40,
        'price_in_1m': 0.10,
        'price_out_1m': 0.40,
        'cost_per_task': 0.300,
        'context_window': '1M tokens',
        'badge': '⚡ Gemini 2.0 Flash (Official CLI)',
        'best_for': 'Tốc độ phản hồi cực nhanh cho các tác vụ hàng ngày.'
    },
    # ---- Codex Platform Models ----
    'o3 xhigh': {
        'provider': 'OpenAI (Codex)',
        'intelligence_index': 62.0,
        'coding_score': 92.0,
        'reasoning_score': 93.0,
        'speed_tps': 45.0,
        'ttft_sec': 3.5,
        'price_in_1m': 2.00,
        'price_out_1m': 8.00,
        'cost_per_task': 1.80,
        'context_window': '200K tokens',
        'badge': '🔬 Deep Reasoning (xhigh)',
        'best_for': 'Suy luận cực sâu, bài toán phức tạp, nghiên cứu.'
    },
    'o3 high': {
        'provider': 'OpenAI (Codex)',
        'intelligence_index': 59.0,
        'coding_score': 90.0,
        'reasoning_score': 91.0,
        'speed_tps': 55.0,
        'ttft_sec': 2.5,
        'price_in_1m': 2.00,
        'price_out_1m': 8.00,
        'cost_per_task': 1.20,
        'context_window': '200K tokens',
        'badge': '🧠 Strong Reasoning (high)',
        'best_for': 'Coding phức tạp, kiến trúc, review code.'
    },
    'o4-mini high': {
        'provider': 'OpenAI (Codex)',
        'intelligence_index': 55.0,
        'coding_score': 87.5,
        'reasoning_score': 86.0,
        'speed_tps': 120.0,
        'ttft_sec': 1.2,
        'price_in_1m': 0.40,
        'price_out_1m': 1.60,
        'cost_per_task': 0.45,
        'context_window': '200K tokens',
        'badge': '⚡ Fast & Smart (high)',
        'best_for': 'Coding hàng ngày, refactor, viết test nhanh.'
    },
    'o4-mini xhigh': {
        'provider': 'OpenAI (Codex)',
        'intelligence_index': 57.0,
        'coding_score': 89.0,
        'reasoning_score': 88.0,
        'speed_tps': 90.0,
        'ttft_sec': 1.8,
        'price_in_1m': 0.40,
        'price_out_1m': 1.60,
        'cost_per_task': 0.65,
        'context_window': '200K tokens',
        'badge': '⚡ Fast & Deep (xhigh)',
        'best_for': 'Suy luận sâu nhưng nhanh, tối ưu chi phí.'
    },
    'gpt-4.1 standard': {
        'provider': 'OpenAI (Codex)',
        'intelligence_index': 54.0,
        'coding_score': 86.0,
        'reasoning_score': 84.0,
        'speed_tps': 150.0,
        'ttft_sec': 0.8,
        'price_in_1m': 2.00,
        'price_out_1m': 8.00,
        'cost_per_task': 0.55,
        'context_window': '1M tokens',
        'badge': '📝 General Purpose',
        'best_for': 'Tác vụ tổng quát, soạn thảo, phân tích tài liệu.'
    },
    'claude 4 sonnet standard': {
        'provider': 'Anthropic (Codex)',
        'intelligence_index': 60.0,
        'coding_score': 91.0,
        'reasoning_score': 90.0,
        'speed_tps': 80.0,
        'ttft_sec': 1.5,
        'price_in_1m': 3.00,
        'price_out_1m': 15.00,
        'cost_per_task': 1.50,
        'context_window': '200K tokens',
        'badge': '🎯 Balanced Intelligence',
        'best_for': 'Code quality cao, review kiến trúc, viết tài liệu.'
    },
    '5.6 sol xhigh': {
        'provider': 'Sol / Codex',
        'intelligence_index': 64.0,
        'coding_score': 94.2,
        'reasoning_score': 95.0,
        'speed_tps': 75.0,
        'ttft_sec': 2.0,
        'price_in_1m': 3.00,
        'price_out_1m': 15.00,
        'cost_per_task': 1.65,
        'context_window': '256K tokens',
        'badge': '☀️ Deep Reasoning Sol (xhigh)',
        'best_for': 'Suy luận đỉnh cao, giải quyết các bài toán lập trình và kiến trúc đa tầng.'
    },
    '5.6 sol high': {
        'provider': 'Sol / Codex',
        'intelligence_index': 61.0,
        'coding_score': 92.0,
        'reasoning_score': 92.5,
        'speed_tps': 95.0,
        'ttft_sec': 1.4,
        'price_in_1m': 2.50,
        'price_out_1m': 12.00,
        'cost_per_task': 1.10,
        'context_window': '256K tokens',
        'badge': '☀️ High Sol (high)',
        'best_for': 'Coding tốc độ cao và suy luận chuẩn xác hàng ngày.'
    },
    '5.6 sol standard': {
        'provider': 'Sol / Codex',
        'intelligence_index': 58.0,
        'coding_score': 89.0,
        'reasoning_score': 88.0,
        'speed_tps': 140.0,
        'ttft_sec': 0.9,
        'price_in_1m': 1.50,
        'price_out_1m': 6.00,
        'cost_per_task': 0.60,
        'context_window': '256K tokens',
        'badge': '☀️ Standard Sol',
        'best_for': 'Tác vụ tổng quát, chat nhanh và xử lý tài liệu.'
    }
}

# Official OpenAI metadata fetched from developers.openai.com on 2026-08-20.
# Do not infer benchmark scores from these values: OpenAI Docs currently provides
# model capabilities, context, reasoning efforts and pricing, not the local IQ/speed
# metrics used by this dashboard.
OPENAI_GPT56_OFFICIAL = {
    'sol': {
        'model_id': 'gpt-5.6-sol',
        'price_in_1m': 4.00,
        'price_cached_in_1m': 0.40,
        'price_out_1m': 20.00,
        'badge': '☀️ GPT-5.6 Sol',
        'best_for': 'Frontier model cho công việc chuyên môn phức tạp, reasoning và coding.',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-5.6-sol'
    },
    'terra': {
        'model_id': 'gpt-5.6-terra',
        'price_in_1m': 2.00,
        'price_cached_in_1m': 0.20,
        'price_out_1m': 12.00,
        'badge': '🌍 GPT-5.6 Terra',
        'best_for': 'Cân bằng intelligence và chi phí cho workload coding/reasoning thường xuyên.',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-5.6-terra'
    },
    'luna': {
        'model_id': 'gpt-5.6-luna',
        'price_in_1m': 0.20,
        'price_cached_in_1m': 0.02,
        'price_out_1m': 1.20,
        'badge': '🌙 GPT-5.6 Luna',
        'best_for': 'Workload khối lượng lớn, nhạy chi phí và cần context lớn.',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-5.6-luna'
    }
}

GPT56_REASONING_LABELS_BY_FAMILY = {
    # Codex exposes an additional Ultra effort for Sol and Terra. Artificial
    # Analysis has not published a separate Ultra result, so those rows stay
    # unranked while retaining verified provider pricing.
    'sol': ('none', 'low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
    'terra': ('none', 'low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
    'luna': ('none', 'low', 'standard', 'high', 'xhigh', 'max'),
}

def register_openai_gpt56_models():
    """Merge official GPT-5.6 metadata into every reasoning-effort variant."""
    for family, official in OPENAI_GPT56_OFFICIAL.items():
        for label in GPT56_REASONING_LABELS_BY_FAMILY[family]:
            key = f'5.6 {family} {label}'
            entry = BENCHMARK_DATABASE.get(key)
            if entry is None:
                entry = {
                    'intelligence_index': None,
                    'coding_score': None,
                    'reasoning_score': None,
                    'speed_tps': None,
                    'ttft_sec': None,
                    'cost_per_task': None,
                    'benchmark_source': 'unbenchmarked',
                }
                BENCHMARK_DATABASE[key] = entry
            else:
                entry.setdefault('benchmark_source', 'local_heuristic')

            effort = 'medium' if label == 'standard' else label
            entry.update({
                'provider': 'OpenAI (Codex)',
                'model_id': official['model_id'],
                'reasoning_effort': effort,
                'price_in_1m': official['price_in_1m'],
                'price_cached_in_1m': official['price_cached_in_1m'],
                'price_out_1m': official['price_out_1m'],
                'context_window': '1.05M tokens',
                'max_input_tokens': 922_000,
                'max_output_tokens': 128_000,
                'knowledge_cutoff': '2026-02-16',
                'metadata_source': 'OpenAI Docs',
                'metadata_source_url': official['source_url'],
                'badge': f"{official['badge']} ({label})",
                'best_for': official['best_for'],
            })

register_openai_gpt56_models()

OPENAI_VERIFIED_PRICING = {
    'gpt-6-astra': {
        'price_in_1m': 10.00,
        'price_cached_in_1m': 1.00,
        'price_out_1m': 50.00,
        'reasoning_labels': ('low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
        'default_effort': 'medium',
        'knowledge_cutoff': '2026-04-30',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-6-astra',
    },
    'gpt-6-sol': {
        'price_in_1m': 2.00,
        'price_cached_in_1m': 0.20,
        'price_out_1m': 10.00,
        'reasoning_labels': ('none', 'low', 'standard', 'high', 'xhigh', 'max', 'ultra'),
        'default_effort': 'medium',
        'knowledge_cutoff': '2026-04-20',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-6-sol',
    },
    'gpt-6-luna': {
        'price_in_1m': 0.10,
        'price_cached_in_1m': 0.01,
        'price_out_1m': 0.50,
        'reasoning_labels': ('none', 'low', 'standard', 'high', 'xhigh', 'max'),
        'default_effort': 'medium',
        'knowledge_cutoff': '2026-05-18',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-6-luna',
    },
    'gpt-5.5': {
        'price_in_1m': 5.00,
        'price_cached_in_1m': 0.50,
        'price_out_1m': 30.00,
        'reasoning_labels': ('none', 'low', 'standard', 'high', 'xhigh'),
        'default_effort': 'medium',
        'knowledge_cutoff': '2025-12-01',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-5.5',
    },
    'gpt-5.4': {
        'price_in_1m': 2.50,
        'price_cached_in_1m': 0.25,
        'price_out_1m': 15.00,
        'reasoning_labels': ('none', 'low', 'standard', 'high', 'xhigh'),
        'default_effort': 'none',
        'knowledge_cutoff': '2025-08-31',
        'source_url': 'https://developers.openai.com/api/docs/models/gpt-5.4',
    },
}

# Codex subscription credits are a separate rate card from API dollar prices.
# Published Standard-speed rates per 1M tokens, checked on 2026-09-23.
CODEX_OFFICIAL_CREDIT_RATES = {
    'gpt-6-astra': (250.0, 25.0, 1250.0),
    'gpt-6-sol': (50.0, 5.0, 250.0),
    'gpt-6-luna': (2.5, 0.25, 12.5),
    'gpt-5.6-sol': (100.0, 10.0, 500.0),
    'gpt-5.6-terra': (50.0, 5.0, 300.0),
    'gpt-5.6-luna': (5.0, 0.5, 30.0),
}
CODEX_CREDIT_RATE_SOURCE_URL = 'https://learn.chatgpt.com/docs/pricing'


def register_codex_credit_rates():
    for entry in BENCHMARK_DATABASE.values():
        rates = CODEX_OFFICIAL_CREDIT_RATES.get(entry.get('model_id'))
        if rates:
            entry.update({
                'codex_credit_in_1m': rates[0],
                'codex_credit_cached_in_1m': rates[1],
                'codex_credit_out_1m': rates[2],
                'codex_credit_rate_source_url': CODEX_CREDIT_RATE_SOURCE_URL,
                'codex_credit_rate_as_of': '2026-09-23',
                'codex_credit_rate_speed': 'standard',
            })


GEMINI_36_FLASH_OFFICIAL = {
    'model_id': 'gemini-3.6-flash',
    'price_in_1m': 0.75,
    'price_cached_in_1m': 0.075,
    'price_out_1m': 3.75,
    'pricing_valid_through': '2026-12-31',
    'scheduled_price_in_1m_from_2027': 1.50,
    'scheduled_price_cached_in_1m_from_2027': 0.15,
    'scheduled_price_out_1m_from_2027': 7.50,
    'source_url': 'https://ai.google.dev/gemini-api/docs/pricing',
}

GEMINI_38_FLASH_OFFICIAL = {
    'model_id': 'gemini-3.8-flash',
    'price_in_1m': 0.75,
    'price_cached_in_1m': 0.075,
    'price_out_1m': 3.75,
    'pricing_valid_through': '2026-12-31',
    'scheduled_price_in_1m_from_2027': 1.50,
    'scheduled_price_cached_in_1m_from_2027': 0.15,
    'scheduled_price_out_1m_from_2027': 7.50,
    'max_output_tokens': 64_000,
    'source_url': 'https://ai.google.dev/gemini-api/docs/pricing',
}


def _blank_priced_model_entry():
    return {
        'intelligence_index': None,
        'coding_score': None,
        'reasoning_score': None,
        'speed_tps': None,
        'ttft_sec': None,
        'cost_per_task': None,
        'context_window': 'Unknown',
        'badge': 'Verified pricing',
        'best_for': 'Provider pricing is verified; local benchmark metadata is unavailable.',
        'benchmark_source': 'unbenchmarked',
    }


def register_additional_verified_pricing():
    """Register official pricing without inventing benchmark scores."""
    for model_id, official in OPENAI_VERIFIED_PRICING.items():
        variants = [(model_id, official['default_effort'], None)]
        for label in official['reasoning_labels']:
            effort = 'medium' if label == 'standard' else label
            variants.append((f'{model_id} {label}', effort, label))

        for key, effort, label in variants:
            entry = BENCHMARK_DATABASE.get(key)
            if entry is None:
                entry = _blank_priced_model_entry()
                BENCHMARK_DATABASE[key] = entry
            else:
                entry.setdefault('benchmark_source', 'local_heuristic')
            entry.update({
                'provider': 'OpenAI (Codex)',
                'model_id': model_id,
                'reasoning_effort': effort,
                'price_in_1m': official['price_in_1m'],
                'price_cached_in_1m': official['price_cached_in_1m'],
                'price_out_1m': official['price_out_1m'],
                'context_window': '1.05M tokens',
                'max_output_tokens': 128_000,
                'knowledge_cutoff': official['knowledge_cutoff'],
                'pricing_source': 'official',
                'metadata_source': 'OpenAI API Docs',
                'metadata_source_url': official['source_url'],
                'badge': model_id.upper() + (f' ({label})' if label else ''),
            })

    for key, effort in (
        ('Gemini 3.6 Flash', None),
        ('Gemini 3.6 Flash (High)', 'high'),
        ('gemini-3.6-flash', None),
    ):
        entry = BENCHMARK_DATABASE.get(key)
        if entry is None:
            entry = _blank_priced_model_entry()
            BENCHMARK_DATABASE[key] = entry
        entry.update({
            'provider': 'Google',
            'model_id': GEMINI_36_FLASH_OFFICIAL['model_id'],
            'reasoning_effort': effort,
            'price_in_1m': GEMINI_36_FLASH_OFFICIAL['price_in_1m'],
            'price_cached_in_1m': GEMINI_36_FLASH_OFFICIAL['price_cached_in_1m'],
            'price_out_1m': GEMINI_36_FLASH_OFFICIAL['price_out_1m'],
            'pricing_source': 'official',
            'pricing_valid_through': GEMINI_36_FLASH_OFFICIAL['pricing_valid_through'],
            'scheduled_price_in_1m_from_2027': GEMINI_36_FLASH_OFFICIAL['scheduled_price_in_1m_from_2027'],
            'scheduled_price_cached_in_1m_from_2027': GEMINI_36_FLASH_OFFICIAL['scheduled_price_cached_in_1m_from_2027'],
            'scheduled_price_out_1m_from_2027': GEMINI_36_FLASH_OFFICIAL['scheduled_price_out_1m_from_2027'],
            'metadata_source': 'Google Gemini API Pricing',
            'metadata_source_url': GEMINI_36_FLASH_OFFICIAL['source_url'],
            'badge': 'Gemini 3.6 Flash' + (' (High)' if effort == 'high' else ''),
        })

    for key, effort in (
        ('Gemini 3.8 Flash (Low)', 'low'),
        ('Gemini 3.8 Flash', 'medium'),
        ('Gemini 3.8 Flash (High)', 'high'),
    ):
        entry = BENCHMARK_DATABASE.get(key)
        if entry is None:
            entry = _blank_priced_model_entry()
            BENCHMARK_DATABASE[key] = entry
        entry.update({
            'provider': 'Google',
            'model_id': GEMINI_38_FLASH_OFFICIAL['model_id'],
            'reasoning_effort': effort,
            'price_in_1m': GEMINI_38_FLASH_OFFICIAL['price_in_1m'],
            'price_cached_in_1m': GEMINI_38_FLASH_OFFICIAL['price_cached_in_1m'],
            'price_out_1m': GEMINI_38_FLASH_OFFICIAL['price_out_1m'],
            'pricing_source': 'official',
            'pricing_valid_through': GEMINI_38_FLASH_OFFICIAL['pricing_valid_through'],
            'scheduled_price_in_1m_from_2027': GEMINI_38_FLASH_OFFICIAL['scheduled_price_in_1m_from_2027'],
            'scheduled_price_cached_in_1m_from_2027': GEMINI_38_FLASH_OFFICIAL['scheduled_price_cached_in_1m_from_2027'],
            'scheduled_price_out_1m_from_2027': GEMINI_38_FLASH_OFFICIAL['scheduled_price_out_1m_from_2027'],
            'max_output_tokens': GEMINI_38_FLASH_OFFICIAL['max_output_tokens'],
            'metadata_source': 'Google Gemini API Pricing',
            'metadata_source_url': GEMINI_38_FLASH_OFFICIAL['source_url'],
        })


register_additional_verified_pricing()


# Exact choices exposed by the current Codex desktop runtime on this host.
# Keep availability separate from AA coverage: a selectable model must remain
# visible even when AA has not published a benchmark for that effort level.
CODEX_SELECTABLE_MODELS_AS_OF = '2026-09-23'
CODEX_SELECTABLE_MODEL_EFFORTS = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-terra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.5': ('low', 'medium', 'high', 'xhigh'),
}


def _codex_catalog_key(model_id, effort):
    if model_id.startswith('chatgpt-web/'):
        return model_id if effort == 'medium' else f'{model_id} {effort}'
    label = 'standard' if effort == 'medium' else effort
    if model_id.startswith('gpt-5.6-'):
        return f"5.6 {model_id.removeprefix('gpt-5.6-')} {label}"
    return f'{model_id} {label}'


def _codex_selectable_display_name(model_id, effort):
    family_names = {
        'gpt-6-astra': 'GPT-6 Astra',
        'gpt-6-sol': 'GPT-6 Sol',
        'gpt-6-luna': 'GPT-6 Luna',
        'gpt-5.6-sol': 'GPT-5.6 Sol',
        'gpt-5.6-terra': 'GPT-5.6 Terra',
        'gpt-5.6-luna': 'GPT-5.6 Luna',
        'gpt-5.5': 'GPT-5.5',
    }
    return f"{family_names.get(model_id, model_id)} · {effort.capitalize()}"


def register_codex_selectable_models():
    family_labels = {
        'gpt-6-astra': 'Astra',
        'gpt-6-sol': 'Sol',
        'gpt-6-luna': 'Luna',
        'gpt-5.6-sol': 'Sol',
        'gpt-5.6-terra': 'Terra',
        'gpt-5.6-luna': 'Luna',
        'gpt-5.5': 'GPT-5.5',
    }
    selection_order = 0
    for model_id, efforts in CODEX_SELECTABLE_MODEL_EFFORTS.items():
        for effort in efforts:
            selection_order += 1
            key = _codex_catalog_key(model_id, effort)
            entry = BENCHMARK_DATABASE.get(key)
            if entry is None:
                entry = _blank_priced_model_entry()
                entry.update({
                    'model_id': model_id,
                    'reasoning_effort': effort,
                    'provider': 'OpenAI (Codex)',
                    'metadata_source': 'Codex desktop runtime',
                })
                BENCHMARK_DATABASE[key] = entry
            entry.update({
                'selectable_in_codex': True,
                'availability_source': 'Codex desktop runtime',
                'availability_as_of': CODEX_SELECTABLE_MODELS_AS_OF,
                'selection_order': selection_order,
                'display_name': _codex_selectable_display_name(model_id, effort),
                'family': entry.get('family') or family_labels[model_id],
            })


register_codex_selectable_models()
register_codex_credit_rates()


def refresh_codex_runtime_models(cache_file=None):
    """Add visible Codex picker choices from the local client cache, without guessing prices."""
    path = cache_file or CODEX_RUNTIME_MODELS_CACHE_FILE
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            catalog = json.load(handle)
    except (OSError, ValueError, TypeError):
        return False
    rows = catalog.get('models') if isinstance(catalog, dict) else None
    if not isinstance(rows, list) or not rows:
        return False

    visible = []
    for row in rows:
        if not isinstance(row, dict) or row.get('visibility') != 'list':
            continue
        model_id = row.get('slug')
        if not isinstance(model_id, str) or not (
            re.fullmatch(r'gpt-\d+(?:\.\d+)?(?:-[a-z0-9]+)?', model_id) or
            model_id in ('chatgpt-web/light', 'chatgpt-web/medium', 'chatgpt-web/high', 'chatgpt-web/extra-high')
        ):
            continue
        efforts = row.get('supported_reasoning_levels')
        if not isinstance(efforts, list):
            continue
        valid_efforts = []
        for level in efforts:
            effort = level if isinstance(level, str) else level.get('effort') if isinstance(level, dict) else None
            if effort in ('none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
                valid_efforts.append(effort)
        if valid_efforts:
            visible.append((model_id, row.get('display_name') or model_id, valid_efforts))
    if not visible:
        return False

    # A successful local cache read supersedes the dated built-in fallback.
    for entry in BENCHMARK_DATABASE.values():
        if entry.get('availability_source') in ('Codex desktop runtime', 'Codex local model cache'):
            entry['selectable_in_codex'] = False
    fetched_at = str(catalog.get('fetched_at') or '')[:10] or None
    order = 0
    for model_id, display_name, efforts in visible:
        for effort in efforts:
            order += 1
            key = _codex_catalog_key(model_id, effort)
            entry = BENCHMARK_DATABASE.get(key)
            if entry is None:
                entry = _blank_priced_model_entry()
                entry.update({
                    'model_id': model_id,
                    'reasoning_effort': effort,
                    'provider': 'OpenAI (Codex)',
                    'metadata_source': 'Codex local model cache',
                    'price_in_1m': None,
                    'price_cached_in_1m': None,
                    'price_out_1m': None,
                    'pricing_source': 'unknown',
                    'badge': 'Chưa có bảng giá/benchmark',
                    'best_for': 'Model mới được nhận diện từ Codex; chưa có giá hoặc benchmark đã xác minh.',
                })
                BENCHMARK_DATABASE[key] = entry
            entry.update({
                'selectable_in_codex': True,
                'availability_source': 'Codex local model cache',
                'availability_as_of': fetched_at,
                'selection_order': order,
                'display_name': (display_name if model_id.startswith('chatgpt-web/')
                                 else f'{display_name} · {effort.capitalize()}'),
                'family': entry.get('family') or ('ChatGPT Web' if model_id.startswith('chatgpt-web/')
                                                 else model_id.rsplit('-', 1)[-1].capitalize()),
            })
    register_codex_credit_rates()
    return True

ARTIFICIAL_ANALYSIS_SOURCE = 'Artificial Analysis'
ARTIFICIAL_ANALYSIS_INDEX_VERSION = '4.3.2'
ARTIFICIAL_ANALYSIS_AS_OF = '2026-09-21'
AA_LEADERBOARD_METRICS = (
    'intelligence_index',
    'coding_score',
    'reasoning_score',
    'speed_tps',
    'ttft_sec',
    'cost_per_task',
)

# Artificial Analysis Index v4.3.2 values checked against the linked release/model
# pages on 2026-09-21. Only values shown by AA are copied. Some release pages
# explicitly mark their v4.3.2 scores as estimates pending independent evaluation;
# that status is carried to the UI instead of presenting every row as measured.
ARTIFICIAL_ANALYSIS_BENCHMARKS = {
    # Current frontier family.
    'gpt-6-astra low': {
        'intelligence_index': 46.0, 'speed_tps': 62.0, 'cost_per_task': 0.82,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-6-astra',
        'benchmark_status': 'measured', 'family': 'Astra',
    },
    'gpt-6-astra standard': {
        'intelligence_index': 50.0, 'speed_tps': 66.0, 'cost_per_task': 1.54,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-6-astra',
        'benchmark_status': 'measured', 'family': 'Astra',
    },
    'gpt-6-astra high': {
        'intelligence_index': 51.0, 'speed_tps': 65.0, 'cost_per_task': 1.73,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-6-astra',
        'benchmark_status': 'measured', 'family': 'Astra',
    },
    'gpt-6-astra xhigh': {
        'intelligence_index': 52.0, 'speed_tps': 66.0, 'cost_per_task': 2.31,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-6-astra',
        'benchmark_status': 'measured', 'family': 'Astra',
    },
    'gpt-6-astra max': {
        'intelligence_index': 53.0, 'speed_tps': 71.0, 'cost_per_task': 3.26,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-6-astra',
        'benchmark_status': 'measured', 'family': 'Astra',
        'recommended': True, 'recommendation_order': 1,
        'decision_label': 'Chất lượng cao nhất',
        'decision_note': 'Dùng cho nhiệm vụ khó nhất khi chất lượng quan trọng hơn chi phí và độ trễ.',
    },

    # GPT-5.6 Sol: strongest quality/quota balance in the local measurements.
    '5.6 sol none': {
        'intelligence_index': 28.0, 'speed_tps': 64.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
    },
    '5.6 sol low': {
        'intelligence_index': 33.0, 'speed_tps': 72.0, 'cost_per_task': 0.26,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
    },
    '5.6 sol standard': {
        'intelligence_index': 39.0, 'speed_tps': 68.0, 'cost_per_task': 0.50,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
        'benchmark_note': 'Mapped to the medium reasoning result.',
    },
    '5.6 sol high': {
        'intelligence_index': 42.0, 'speed_tps': 74.0, 'cost_per_task': 0.81,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
        'recommended': True, 'recommendation_order': 2,
        'decision_label': 'Cân bằng chất lượng và số task',
        'decision_note': 'Mặc định cho công việc hằng ngày; chất lượng tốt và quota/task cục bộ hiện thấp hơn XHigh.',
    },
    '5.6 sol xhigh': {
        'intelligence_index': 44.0, 'speed_tps': 74.0, 'cost_per_task': 1.18,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
        'recommended': True, 'recommendation_order': 3,
        'decision_label': 'Việc khó cần reasoning cao',
        'decision_note': 'Điểm AA cao hơn High; dùng khi phần reasoning thêm có thể tránh làm lại, vì quota/task cục bộ hiện cao hơn High.',
    },
    '5.6 sol max': {
        'intelligence_index': 47.0, 'speed_tps': 75.0, 'cost_per_task': 1.99,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-sol',
        'benchmark_status': 'estimate', 'family': 'Sol',
    },

    # GPT-5.6 Terra: middle price tier.
    '5.6 terra none': {
        'intelligence_index': 21.0, 'speed_tps': 88.0, 'cost_per_task': 0.14,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
    },
    '5.6 terra low': {
        'intelligence_index': 27.0, 'speed_tps': 90.0, 'cost_per_task': 0.14,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
    },
    '5.6 terra standard': {
        'intelligence_index': 30.0, 'speed_tps': 96.0, 'cost_per_task': 0.18,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
        'benchmark_note': 'Mapped to the medium reasoning result.',
    },
    '5.6 terra high': {
        'intelligence_index': 34.0, 'speed_tps': 91.0, 'cost_per_task': 0.34,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
    },
    '5.6 terra xhigh': {
        'intelligence_index': 38.0, 'speed_tps': 104.0, 'cost_per_task': 0.63,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
    },
    '5.6 terra max': {
        'intelligence_index': 42.0, 'speed_tps': 106.0, 'cost_per_task': 1.40,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-terra',
        'benchmark_status': 'estimate', 'family': 'Terra',
        'recommended': True, 'recommendation_order': 4,
        'decision_label': 'Cân bằng theo giá API',
        'decision_note': 'Phù hợp khi muốn giảm giá API so với Sol nhưng vẫn giữ reasoning mạnh.',
    },

    # GPT-5.6 Luna: high-volume/value tier.
    '5.6 luna none': {
        'intelligence_index': 16.0, 'speed_tps': 152.0, 'cost_per_task': 0.01,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
    },
    '5.6 luna low': {
        'intelligence_index': 21.0, 'speed_tps': 154.0, 'cost_per_task': 0.01,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
    },
    '5.6 luna standard': {
        'intelligence_index': 25.0, 'speed_tps': 154.0, 'cost_per_task': 0.02,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
        'benchmark_note': 'Mapped to the medium reasoning result.',
    },
    '5.6 luna high': {
        'intelligence_index': 32.0, 'speed_tps': 157.0, 'cost_per_task': 0.04,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
    },
    '5.6 luna xhigh': {
        'intelligence_index': 35.0, 'speed_tps': 165.0, 'cost_per_task': 0.09,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
        'recommended': True, 'recommendation_order': 5,
        'decision_label': 'Giá trị và khối lượng lớn',
        'decision_note': 'Chi phí AA/task thấp nhất trong nhóm khuyến nghị; hợp với việc dễ kiểm chứng và chạy số lượng lớn.',
    },
    '5.6 luna max': {
        'intelligence_index': 37.0, 'speed_tps': 165.0, 'cost_per_task': 0.18,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-6-luna',
        'benchmark_status': 'estimate', 'family': 'Luna',
    },

    # Google Flash models that are selectable or present in local history.
    'Gemini 3.8 Flash (High)': {
        'intelligence_index': 41.0, 'speed_tps': 329.0, 'cost_per_task': 1.24,
        'source_url': 'https://artificialanalysis.ai/models/releases/gemini-3-8-flash',
        'benchmark_status': 'measured', 'family': 'Gemini 3.8',
        'recommended': True, 'recommendation_order': 6,
        'decision_label': 'Tốc độ và đa phương thức',
        'decision_note': 'Nhanh nhất trong nhóm khuyến nghị, phù hợp tác vụ đa phương thức và vòng lặp ngắn.',
    },
    'Gemini 3.8 Flash': {
        'intelligence_index': 40.0, 'cost_per_task': 0.93,
        'source_url': 'https://artificialanalysis.ai/models/releases/gemini-3-8-flash',
        'benchmark_status': 'measured', 'family': 'Gemini 3.8',
        'benchmark_note': 'Mapped to the medium reasoning result.',
    },
    'Gemini 3.8 Flash (Low)': {
        'intelligence_index': 33.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gemini-3-8-flash',
        'benchmark_status': 'measured', 'family': 'Gemini 3.8',
    },
    'Gemini 3.7 Flash (High)': {
        'intelligence_index': 39.0, 'speed_tps': 310.0, 'cost_per_task': 0.93,
        'source_url': 'https://artificialanalysis.ai/models/releases/gemini-3-7-flash',
        'benchmark_status': 'estimate', 'family': 'Gemini 3.7',
        'generation_status': 'previous',
    },
    'Gemini 3.6 Flash (High)': {
        'intelligence_index': 34.0, 'speed_tps': 198.3, 'cost_per_task': 0.93,
        'source_url': 'https://artificialanalysis.ai/models/gemini-3-6-flash',
        'benchmark_status': 'measured', 'family': 'Gemini 3.6',
        'generation_status': 'previous',
    },

    # Older OpenAI models are retained because the local all-time usage contains
    # substantial history for them. They are hidden from the recommended view.
    'gpt-5.5 none': {
        'intelligence_index': 23.0, 'speed_tps': 84.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-5',
        'benchmark_status': 'estimate', 'family': 'GPT-5.5', 'generation_status': 'previous',
    },
    'gpt-5.5 low': {
        'intelligence_index': 31.0, 'speed_tps': 80.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-5',
        'benchmark_status': 'estimate', 'family': 'GPT-5.5', 'generation_status': 'previous',
    },
    'gpt-5.5 standard': {
        'intelligence_index': 34.0, 'speed_tps': 91.0, 'cost_per_task': 0.90,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-5',
        'benchmark_status': 'estimate', 'family': 'GPT-5.5', 'generation_status': 'previous',
        'benchmark_note': 'Mapped to the medium reasoning result.',
    },
    'gpt-5.5 high': {
        'intelligence_index': 37.0, 'speed_tps': 81.0, 'cost_per_task': 1.54,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-5',
        'benchmark_status': 'estimate', 'family': 'GPT-5.5', 'generation_status': 'previous',
    },
    'gpt-5.5 xhigh': {
        'intelligence_index': 39.0, 'speed_tps': 90.0, 'cost_per_task': 2.63,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-5',
        'benchmark_status': 'estimate', 'family': 'GPT-5.5', 'generation_status': 'previous',
    },
    'gpt-5.4 none': {
        'intelligence_index': 18.0, 'speed_tps': 108.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-4',
        'benchmark_status': 'estimate', 'family': 'GPT-5.4', 'generation_status': 'previous',
    },
    'gpt-5.4 low': {
        'intelligence_index': 28.0, 'speed_tps': 109.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-4',
        'benchmark_status': 'estimate', 'family': 'GPT-5.4', 'generation_status': 'previous',
    },
    'gpt-5.4 xhigh': {
        'intelligence_index': 39.0, 'speed_tps': 158.0,
        'source_url': 'https://artificialanalysis.ai/models/releases/gpt-5-4',
        'benchmark_status': 'estimate', 'family': 'GPT-5.4', 'generation_status': 'previous',
    },
}

# Public AA release pages checked on 2026-09-23. Keep these as the offline
# snapshot until an authenticated AA Data API refresh supplies newer values.
for family, variants in {
    'sol': (
        ('none', 28, 109, 0.33), ('low', 34, 129, 0.13),
        ('standard', 40, 114, 0.25), ('high', 43, 119, 0.37),
        ('xhigh', 44, 128, 0.53), ('max', 48, 115, 1.06),
    ),
    'luna': (
        ('none', 18, 136, 0.01), ('low', 21, 176, 0.0045),
        ('standard', 29, 143, 0.02), ('high', 32, 144, 0.03),
        ('xhigh', 34, 153, 0.04), ('max', 37, 154, 0.07),
    ),
}.items():
    for effort, intelligence, speed, cost in variants:
        ARTIFICIAL_ANALYSIS_BENCHMARKS[f'gpt-6-{family} {effort}'] = {
            'intelligence_index': float(intelligence),
            'speed_tps': float(speed),
            'cost_per_task': cost,
            'source_url': f'https://artificialanalysis.ai/models/releases/gpt-6-{family}',
            'benchmark_status': 'measured',
            'benchmark_as_of': '2026-09-23',
            'family': family.capitalize(),
        }


def register_artificial_analysis_benchmarks():
    """Attach only directly verified Artificial Analysis metrics to catalog rows."""
    for key, benchmark in ARTIFICIAL_ANALYSIS_BENCHMARKS.items():
        entry = BENCHMARK_DATABASE.get(key)
        if entry is None:
            continue
        for metric in AA_LEADERBOARD_METRICS:
            entry[metric] = benchmark.get(metric)
        entry.update({
            'benchmark_source': ARTIFICIAL_ANALYSIS_SOURCE,
            'benchmark_source_url': benchmark['source_url'],
            'benchmark_as_of': benchmark.get('benchmark_as_of', ARTIFICIAL_ANALYSIS_AS_OF),
            'benchmark_index_version': ARTIFICIAL_ANALYSIS_INDEX_VERSION,
            'benchmark_status': benchmark.get('benchmark_status', 'measured'),
            'family': benchmark.get('family'),
            'recommended': bool(benchmark.get('recommended')),
            'recommendation_order': benchmark.get('recommendation_order'),
            'decision_label': benchmark.get('decision_label'),
            'decision_note': benchmark.get('decision_note'),
            'generation_status': benchmark.get('generation_status', 'current'),
            'benchmark_note': benchmark.get('benchmark_note'),
        })
        if entry.get('best_for') == 'Provider pricing is verified; local benchmark metadata is unavailable.':
            entry['best_for'] = 'Provider pricing and the current Artificial Analysis benchmark are verified for this variant.'


def get_verified_aa_metrics(entry):
    """Return AA metrics for the AA leaderboard, hiding local heuristic values."""
    if entry.get('benchmark_source') != ARTIFICIAL_ANALYSIS_SOURCE:
        return {metric: None for metric in AA_LEADERBOARD_METRICS}
    return {metric: entry.get(metric) for metric in AA_LEADERBOARD_METRICS}


def aa_value_score(intelligence_index, cost_per_task):
    """IQ per dollar of AA benchmark-task cost, without a hidden price floor."""
    if intelligence_index is None or cost_per_task is None or cost_per_task <= 0:
        return None
    return round(intelligence_index / cost_per_task, 1)


def sort_and_assign_aa_ranks(rows):
    """Sort verified AA scores first and use dense ranks for ties."""
    rows.sort(
        key=lambda x: (
            x['intelligence_index'] is not None,
            x['intelligence_index'] if x['intelligence_index'] is not None else float('-inf')
        ),
        reverse=True
    )
    dense_rank = 0
    previous_score = None
    for item in rows:
        score = item['intelligence_index']
        if score is None:
            item['rank'] = None
            continue
        if previous_score is None or score != previous_score:
            dense_rank += 1
            previous_score = score
        item['rank'] = dense_rank


register_artificial_analysis_benchmarks()

AA_REFRESH_LOCK = threading.Lock()
AA_REFRESH_RUNNING = False
AA_LAST_ERROR = None
AA_RETRY_AFTER = None


def _aa_api_key():
    key = os.environ.get('ARTIFICIAL_ANALYSIS_API_KEY', '').strip()
    if key:
        return key
    try:
        with open(AA_API_KEY_FILE, 'r', encoding='utf-8') as source:
            return source.read().strip()
    except OSError:
        return ''


def _aa_cache_time(snapshot):
    try:
        value = datetime.fromisoformat(str(snapshot.get('fetched_at') or '').replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _apply_aa_cache(snapshot):
    """Apply only validated AA benchmark fields to existing model identities."""
    if not isinstance(snapshot, dict) or _aa_cache_time(snapshot) is None:
        return 0
    rows = snapshot.get('models')
    if not isinstance(rows, dict):
        return 0
    updated = 0
    for key, benchmark in rows.items():
        entry = BENCHMARK_DATABASE.get(key)
        if not isinstance(entry, dict) or not isinstance(benchmark, dict):
            continue
        # Re-validate the disk cache; it is local state, not an authority to
        # assign arbitrary benchmark numbers or source links.
        intelligence = aa_sync._finite_number(benchmark.get('intelligence_index'), 100)
        if intelligence is None:
            continue
        for metric, maximum in (
            ('coding_score', 100), ('speed_tps', 2000),
            ('ttft_sec', 1000), ('cost_per_task', 1000),
        ):
            entry[metric] = aa_sync._finite_number(benchmark.get(metric), maximum)
        entry['intelligence_index'] = intelligence
        entry['reasoning_score'] = None
        source_url = benchmark.get('source_url')
        entry['benchmark_source'] = ARTIFICIAL_ANALYSIS_SOURCE
        entry['benchmark_source_url'] = (
            source_url if isinstance(source_url, str) and
            source_url.startswith('https://artificialanalysis.ai/')
            else aa_sync.AA_DATA_API_DOCS_URL
        )
        entry['benchmark_as_of'] = snapshot['fetched_at'][:10]
        entry['benchmark_index_version'] = str(snapshot.get('index_version') or ARTIFICIAL_ANALYSIS_INDEX_VERSION)
        entry['benchmark_status'] = 'published'
        updated += 1
    return updated


def _aa_refresh_worker(api_key):
    global AA_REFRESH_RUNNING, AA_LAST_ERROR, AA_RETRY_AFTER
    error = None
    try:
        snapshot = aa_sync.fetch_free_models(api_key)
        _atomic_write_json(AA_BENCHMARK_CACHE_FILE, snapshot)
    except urllib.error.HTTPError as exc:
        error = f'HTTP {exc.code}'
    except Exception as exc:
        error = type(exc).__name__
    with AA_REFRESH_LOCK:
        AA_LAST_ERROR = error
        AA_RETRY_AFTER = datetime.now(timezone.utc) + timedelta(hours=1) if error else None
        AA_REFRESH_RUNNING = False


def prepare_aa_benchmarks():
    """Use the last good snapshot and schedule a non-blocking daily refresh."""
    global AA_REFRESH_RUNNING
    register_artificial_analysis_benchmarks()
    snapshot = _read_json_file(AA_BENCHMARK_CACHE_FILE, {})
    cached_count = _apply_aa_cache(snapshot)
    fetched_at = _aa_cache_time(snapshot) if cached_count else None
    now = datetime.now(timezone.utc)
    key = _aa_api_key()
    stale = fetched_at is None or now - fetched_at >= timedelta(hours=24)
    with AA_REFRESH_LOCK:
        if key and stale and not AA_REFRESH_RUNNING and (AA_RETRY_AFTER is None or now >= AA_RETRY_AFTER):
            AA_REFRESH_RUNNING = True
            threading.Thread(target=_aa_refresh_worker, args=(key,), daemon=True, name='aa-benchmark-refresh').start()
        running = AA_REFRESH_RUNNING
        error = AA_LAST_ERROR
    return {
        'state': ('needs_key' if not key else 'refreshing' if running else
                  'error' if error and stale else 'current' if not stale else 'stale'),
        'last_success_at': fetched_at.isoformat() if fetched_at else None,
        'cached_models': cached_count,
        'error': error if key else None,
        'source_url': aa_sync.AA_DATA_API_DOCS_URL,
        'snapshot_as_of': ARTIFICIAL_ANALYSIS_AS_OF,
    }

def normalize_model_lookup_name(model_name):
    """Normalize common Codex/API spellings without changing stored display names."""
    normalized = re.sub(r'\s*\(fast\)\s*$', '', str(model_name or '').strip().lower())
    normalized = re.sub(r'[-_/]+', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    if normalized == 'gpt 5.6':
        return '5.6 sol standard'
    if normalized.startswith('gpt 5.6 '):
        normalized = normalized[4:]
    if normalized in {'5.6 sol', '5.6 terra', '5.6 luna'}:
        normalized += ' standard'
    if normalized.startswith('5.6 '):
        normalized = re.sub(r' (medium|default)$', ' standard', normalized)
    if re.fullmatch(r'gpt \d+(?:\.\d+)? (?:astra|sol|terra|luna)', normalized):
        normalized += ' standard'
    if re.match(r'gpt \d+(?:\.\d+)? (?:astra|sol|terra|luna) ', normalized):
        normalized = re.sub(r' (medium|default)$', ' standard', normalized)
    return normalized


def _codex_variant_lookup_name(model_name):
    """Compare stored model identities without merging Fast into Standard."""
    name = str(model_name or '').strip()
    base = normalize_model_lookup_name(name)
    return base + (' (fast)' if name.lower().endswith('(fast)') else '')

def _codex_service_tier(value):
    """Normalize Codex's older priority spelling and current Fast setting."""
    tier = str(value or '').strip().lower()
    if tier in ('fast', 'priority'):
        return 'fast'
    if tier == 'default':
        return 'default'
    return ''


def _codex_fast_credit_multiplier(model_name):
    """ChatGPT-credit multiplier, not an API Priority price."""
    if not str(model_name or '').strip().lower().endswith('(fast)'):
        return 1.0
    base = normalize_model_lookup_name(model_name)
    if base.startswith('gpt 5.4 ') or base.startswith('5.4 '):
        return 2.0
    if re.match(r'^(?:gpt )?(?:5\.5|5\.6|6)(?: |$)', base):
        return 2.5
    return 1.0


def normalize_codex_model_effort(model_name, effort=None, service_tier=None):
    """Normalize model family and reasoning effort into a canonical compound identity.

    Returns (compound_key, canonical_model_id, effort_value, effort_label).
    Example: ('5.6 sol high', 'gpt-5.6-sol', 'high', 'high')
    """
    raw_m = str(model_name or '').strip().lower()
    fast_suffix = raw_m.endswith('(fast)')
    if fast_suffix:
        raw_m = raw_m[:-len('(fast)')].strip()
    raw_m_clean = re.sub(r'[-_/]+', ' ', raw_m)
    raw_m_clean = re.sub(r'\s+', ' ', raw_m_clean)

    raw_e = str(effort or '').strip().lower() if effort is not None else ''

    extracted_effort = None
    for ef in ('ultra', 'xhigh', 'standard', 'medium', 'default', 'high', 'none', 'low', 'max'):
        if raw_m_clean.endswith(' ' + ef):
            extracted_effort = ef
            raw_m_clean = raw_m_clean[:-len(ef)-1].strip()
            break

    effective_effort = raw_e or extracted_effort or 'medium'
    is_fast = _codex_service_tier(service_tier) == 'fast' or (
        service_tier is None and fast_suffix
    )
    if effective_effort in ('medium', 'default', 'standard', ''):
        effort_label = 'standard'
        effort_value = 'medium'
    else:
        effort_label = effective_effort
        effort_value = effective_effort

    # Match the generation explicitly. A family-only test used to attribute
    # GPT-6 Sol/Luna log events to GPT-5.6 and contaminate both model totals.
    family_match = re.fullmatch(
        r'(?:gpt\s*)?(\d+(?:\.\d+)?)\s+(astra|sol|terra|luna)', raw_m_clean
    )
    if raw_m_clean in ('gpt 5.6', '5.6', 'gpt5.6'):
        family_match = re.fullmatch(r'(5\.6)\s+(sol)', '5.6 sol')
    if family_match:
        generation, family = family_match.groups()
        canonical_model_id = f'gpt-{generation}-{family}'
        compound_key = (
            f'5.6 {family} {effort_label}' if generation == '5.6'
            else f'{canonical_model_id} {effort_label}'
        )
        return compound_key + (' (fast)' if is_fast else ''), canonical_model_id, effort_value, effort_label

    if 'o3' in raw_m_clean:
        canonical_model_id = 'o3'
        compound_key = f'o3 {effort_label}' if effort_label != 'standard' else 'o3 standard'
    elif 'o4 mini' in raw_m_clean or 'o4-mini' in raw_m:
        canonical_model_id = 'o4-mini'
        compound_key = f'o4-mini {effort_label}' if effort_label != 'standard' else 'o4-mini standard'
    elif 'gpt 4.1' in raw_m_clean or 'gpt-4.1' in raw_m:
        canonical_model_id = 'gpt-4.1'
        compound_key = f'gpt-4.1 {effort_label}'
    elif 'claude' in raw_m_clean:
        canonical_model_id = 'claude-4-sonnet'
        compound_key = f'claude 4 sonnet {effort_label}'
    else:
        canonical_model_id = str(model_name or 'unknown').strip()
        compound_key = f'{canonical_model_id} {effort_label}' if effort_label != 'standard' else canonical_model_id

    return compound_key + (' (fast)' if is_fast else ''), canonical_model_id, effort_value, effort_label

def get_benchmark_for_model(model_name):
    if not model_name or model_name == 'Unknown':
        return {
            'provider': 'Other',
            'intelligence_index': None,
            'coding_score': None,
            'reasoning_score': None,
            'speed_tps': None,
            'ttft_sec': None,
            'price_in_1m': None,
            'price_out_1m': None,
            'cost_per_task': None,
            'context_window': 'Unknown',
            'benchmark_source': 'unbenchmarked',
            'pricing_source': 'unknown',
            'badge': 'Khác',
            'best_for': 'Chưa có metadata đáng tin cậy cho model này.'
        }
    if model_name in BENCHMARK_DATABASE:
        return BENCHMARK_DATABASE[model_name]
    normalized_name = normalize_model_lookup_name(model_name)
    normalized_database = {
        normalize_model_lookup_name(key): value
        for key, value in BENCHMARK_DATABASE.items()
    }
    if normalized_name in normalized_database:
        return normalized_database[normalized_name]
    name_lower = normalized_name
    for k, v in BENCHMARK_DATABASE.items():
        normalized_key = normalize_model_lookup_name(k)
        if normalized_key in name_lower or name_lower in normalized_key:
            return v
    return {
        'provider': 'Custom',
        'intelligence_index': None,
        'coding_score': None,
        'reasoning_score': None,
        'speed_tps': None,
        'ttft_sec': None,
        'price_in_1m': None,
        'price_out_1m': None,
        'cost_per_task': None,
        'context_window': 'Unknown',
        'benchmark_source': 'unbenchmarked',
        'pricing_source': 'unknown',
        'badge': 'Custom Model',
        'best_for': 'Model tùy chỉnh; chưa có benchmark hoặc bảng giá đã xác minh.'
    }


CODEX_AUTO_REVIEW_PRICING_BASIS = 'gpt-5.4 low'
CODEX_AUTO_REVIEW_PRICING_NOTE = (
    'OpenAI ChatGPT rate card: Auto review uses GPT-5.4; token pricing follows GPT-5.4.'
)


def _is_codex_auto_review_model(model_name):
    normalized = normalize_model_lookup_name(model_name)
    return normalized == 'codex auto review' or normalized.startswith('codex auto review ')


def model_has_verified_pricing(model_name):
    if str(model_name or '').strip().lower().endswith('(fast)'):
        # The base token rate is verified; its ChatGPT credit uplift is a
        # separate product rule, so it is not a verified API dollar rate.
        return False
    if _is_codex_auto_review_model(model_name):
        return True
    bm = get_benchmark_for_model(model_name)
    return bm.get('price_in_1m') is not None and bm.get('price_out_1m') is not None


CHATGPT_WEB_PRICING_BASIS = '5.6 sol standard'
CHATGPT_WEB_PRICING_NOTE = (
    'ChatGPT Web được hiển thị là GPT-5.6 Sol Thinking. Log cục bộ không cung cấp một mức '
    'reasoning effort API ổn định, nên chi phí được ước tính theo bảng giá token của họ GPT-5.6 Sol; '
    'giá token giống nhau giữa các mức effort.'
)


def _is_chatgpt_web_model(model_name):
    normalized = normalize_model_lookup_name(model_name)
    return normalized == 'chatgpt web' or normalized.startswith('chatgpt web ')


def get_effective_pricing(model_name):
    """Return pricing usable for cost estimation, with provenance metadata.

    Verified/catalog-priced models use their own pricing. ChatGPT Web usage uses
    the GPT-5.6 Sol family token rate as a GPT-5.6 Sol Thinking proxy, while
    remaining marked as estimated rather than verified pricing.
    """
    if str(model_name or '').strip().lower().endswith('(fast)'):
        base_name = str(model_name).strip()[:-len('(fast)')].strip()
        base = get_effective_pricing(base_name)
        multiplier = _codex_fast_credit_multiplier(model_name)
        if base is None or multiplier == 1.0:
            return None
        weighted = dict(base)
        for field in ('price_in_1m', 'price_cached_in_1m', 'price_out_1m'):
            if weighted.get(field) is not None:
                weighted[field] = float(weighted[field]) * multiplier
        weighted.update({
            'pricing_source': 'chatgpt_fast_credit_equivalent',
            'pricing_verified': False,
            'pricing_estimated': True,
            'pricing_basis_model': base_name,
            'pricing_note': (
                f'ChatGPT Fast consumes {multiplier:g}x Standard credits. '
                'Displayed USD is a credit-weighted estimate, not an API charge.'
            ),
            'fast_credit_multiplier': multiplier,
        })
        return weighted

    if _is_codex_auto_review_model(model_name):
        basis = get_benchmark_for_model(CODEX_AUTO_REVIEW_PRICING_BASIS)
        price_in = basis.get('price_in_1m')
        price_out = basis.get('price_out_1m')
        if price_in is None or price_out is None:
            return None
        return {
            'price_in_1m': price_in,
            'price_cached_in_1m': basis.get('price_cached_in_1m'),
            'price_out_1m': price_out,
            'pricing_source': 'official_feature_mapping',
            'pricing_verified': True,
            'pricing_estimated': False,
            'pricing_basis_model': 'GPT-5.4',
            'pricing_note': CODEX_AUTO_REVIEW_PRICING_NOTE,
        }

    if _is_chatgpt_web_model(model_name):
        basis = get_benchmark_for_model(CHATGPT_WEB_PRICING_BASIS)
        price_in = basis.get('price_in_1m')
        price_out = basis.get('price_out_1m')
        if price_in is None or price_out is None:
            return None
        return {
            'price_in_1m': price_in,
            'price_cached_in_1m': basis.get('price_cached_in_1m'),
            'price_out_1m': price_out,
            'pricing_source': 'assumed_equivalent',
            'pricing_verified': False,
            'pricing_estimated': True,
            'pricing_basis_model': 'GPT-5.6 Sol Thinking',
            'pricing_note': CHATGPT_WEB_PRICING_NOTE,
        }

    bm = get_benchmark_for_model(model_name)
    price_in = bm.get('price_in_1m')
    price_out = bm.get('price_out_1m')
    if price_in is None or price_out is None:
        return None
    return {
        'price_in_1m': price_in,
        'price_cached_in_1m': bm.get('price_cached_in_1m'),
        'price_out_1m': price_out,
        'pricing_source': bm.get('pricing_source') or 'catalog',
        'pricing_verified': model_has_verified_pricing(model_name),
        'pricing_estimated': False,
        'pricing_basis_model': bm.get('model_id') or str(model_name or ''),
        'pricing_note': None,
    }


def model_has_cost_estimate(model_name):
    return get_effective_pricing(model_name) is not None


def get_pricing_provenance(model_name):
    pricing = get_effective_pricing(model_name)
    if pricing is None:
        return {
            'pricing_known': False,
            'pricing_verified': False,
            'pricing_estimated': False,
            'pricing_source': 'unknown',
            'pricing_basis_model': None,
            'pricing_note': None,
            'fast_credit_multiplier': None,
        }
    return {
        'pricing_known': True,
        'pricing_verified': bool(pricing.get('pricing_verified')),
        'pricing_estimated': bool(pricing.get('pricing_estimated')),
        'pricing_source': pricing.get('pricing_source'),
        'pricing_basis_model': pricing.get('pricing_basis_model'),
        'pricing_note': pricing.get('pricing_note'),
        'fast_credit_multiplier': pricing.get('fast_credit_multiplier'),
    }


def estimate_model_cost(model_name, input_tokens=0, output_tokens=0,
                        cached_input_tokens=0, cache_write_input_tokens=0):
    """Estimate Standard API-equivalent or Fast credit-weighted cost."""
    pricing = get_effective_pricing(model_name)
    if pricing is None:
        return None

    price_in = float(pricing['price_in_1m'])
    price_out = float(pricing['price_out_1m'])
    price_cached_in = pricing.get('price_cached_in_1m')

    in_toks = max(0, int(input_tokens or 0))
    out_toks = max(0, int(output_tokens or 0))
    cached_toks = max(0, int(cached_input_tokens or 0))
    cache_write_toks = max(0, int(cache_write_input_tokens or 0))

    if price_cached_in is not None and (cached_toks > 0 or cache_write_toks > 0):
        price_cached = float(price_cached_in)
        price_write = price_in * 1.25
        capped_cached = min(in_toks, cached_toks)
        capped_write = min(max(0, in_toks - capped_cached), cache_write_toks)
        uncached_in = max(0, in_toks - capped_cached - capped_write)
        in_cost = (
            (uncached_in / 1_000_000) * price_in +
            (capped_cached / 1_000_000) * price_cached +
            (capped_write / 1_000_000) * price_write
        )
    else:
        in_cost = (in_toks / 1_000_000) * price_in

    out_cost = (out_toks / 1_000_000) * price_out
    return in_cost + out_cost

# ---- Account Management Engine ----
def get_logged_in_account():
    acc = {
        'email': '',
        'name': 'Local user',
        'profile_pic': '',
        'tier': 'Not detected',
        'is_logged_in': False,
        'current_model': 'Unknown'
    }

    if os.path.exists(VSCDB_PATH):
        try:
            conn = sqlite3.connect(VSCDB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key='antigravity.profileUrl'")
            row = cur.fetchone()
            if row:
                val = row[0]
                if isinstance(val, bytes): val = val.decode('utf-8', errors='ignore')
                acc['profile_pic'] = val

            cur.execute("SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.userStatus'")
            row = cur.fetchone()
            if row:
                val = row[0]
                if isinstance(val, bytes): val = val.decode('utf-8', errors='ignore')
                for b in re.findall(r'[A-Za-z0-9+/=]{40,}', val):
                    try:
                        raw = base64.b64decode(b)
                        emails = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', raw.decode('latin-1', errors='ignore'))
                        if emails:
                            acc['email'] = emails[0]
                            acc['is_logged_in'] = True
                        m = re.search(rb'\x1a(.)([^\x00-\x1f:]+):\x16', raw)
                        if m:
                            try: acc['name'] = m.group(2).decode('utf-8')
                            except: pass
                    except: pass
            conn.close()
        except Exception:
            pass

    return acc

@_serialize_persistence
def sync_accounts_db():
    current_acc = get_logged_in_account()
    os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)
    account_key = current_acc['email'] or '__unknown__'

    accounts_data = {
        'active_email': account_key,
        'accounts': {}
    }

    saved_accounts = _read_json_file(ACCOUNTS_FILE, {})
    if isinstance(saved_accounts, dict) and saved_accounts:
        accounts_data = saved_accounts

    email = account_key
    if email not in accounts_data.get('accounts', {}):
        if 'accounts' not in accounts_data: accounts_data['accounts'] = {}
        accounts_data['accounts'][email] = {
            'email': current_acc['email'],
            'name': current_acc['name'],
            'profile_pic': current_acc['profile_pic'],
            'tier': current_acc['tier'],
            'is_logged_in': current_acc.get('is_logged_in', False),
            'first_seen': datetime.now(timezone.utc).isoformat(),
            'last_active': datetime.now(timezone.utc).isoformat(),
            'limits': {
                'gemini_5h_tokens': 500000,
                'external_5h_tokens': 60000,
                'gemini_weekly_tokens': 2500000,
                'external_weekly_tokens': 250000
            }
        }
    else:
        accounts_data['accounts'][email]['last_active'] = datetime.now(timezone.utc).isoformat()
        accounts_data['accounts'][email]['name'] = current_acc['name']
        accounts_data['accounts'][email]['profile_pic'] = current_acc['profile_pic']
        accounts_data['accounts'][email]['email'] = current_acc['email']
        accounts_data['accounts'][email]['tier'] = current_acc['tier']
        accounts_data['accounts'][email]['is_logged_in'] = current_acc.get('is_logged_in', False)

    if not accounts_data.get('active_email'):
        accounts_data['active_email'] = email

    _atomic_write_json(ACCOUNTS_FILE, accounts_data)

    return accounts_data

# ---- Self-Calibrating Quota Estimator ----
QUOTA_BUCKET_DEFAULTS = {
    'gemini_5h': 500000,
    'external_5h': 60000,
    'gemini_weekly': 2500000,
    'external_weekly': 250000,
}

QUOTA_BUCKET_WINDOWS = {
    'gemini_5h': 5 * 60 * 60,
    'external_5h': 5 * 60 * 60,
    'gemini_weekly': 7 * 24 * 60 * 60,
    'external_weekly': 7 * 24 * 60 * 60,
}


def scan_gemini_worker_usage(state_dir=None, now=None, max_files=2000,
                             max_dirs=10000, max_entries=50000):
    """Read exact Antigravity worker token totals from bounded local run reports."""
    root = state_dir if state_dir is not None else globals().get('GEMINI_WORKER_STATE_DIR')
    now_utc = now if isinstance(now, datetime) else (_parse_iso_utc(now) if now else datetime.now(timezone.utc))
    if not now_utc:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    result = {
        'available': False,
        'source': 'gemini_worker_reports',
        'source_label': 'Gemini Worker (Antigravity CLI exact reports)',
        'estimator_id': GEMINI_WORKER_ESTIMATOR_ID,
        'five_hour_tokens': 0,
        'weekly_tokens': 0,
        'five_hour_runs': 0,
        'weekly_runs': 0,
        'events': [],
        'diagnostics': {'files_seen': 0, 'reports_counted': 0, 'duplicates': 0,
                        'malformed': 0, 'truncated': False},
    }
    if not root or not os.path.isdir(root):
        result['diagnostics']['error'] = 'Gemini worker state directory not found'
        return result

    max_files = max(0, int(max_files))
    max_dirs = max(0, int(max_dirs))
    max_entries = max(0, int(max_entries))
    candidates = []
    matching_reports = 0
    visited_dirs = 0
    visited_entries = 0
    try:
        for walk_root, _, files in os.walk(root, followlinks=False):
            visited_dirs += 1
            if visited_dirs > max_dirs:
                result['diagnostics']['truncated'] = True
                break
            for name in files:
                visited_entries += 1
                if visited_entries > max_entries:
                    result['diagnostics']['truncated'] = True
                    break
                if name != 'report.json':
                    continue
                matching_reports += 1
                path = os.path.join(walk_root, name)
                try:
                    if os.path.islink(path) or is_cloud_offline_file(path):
                        continue
                    item = (os.path.getmtime(path), path)
                    if len(candidates) < max_files:
                        heapq.heappush(candidates, item)
                    elif candidates and item > candidates[0]:
                        heapq.heapreplace(candidates, item)
                except Exception:
                    continue
            if result['diagnostics']['truncated']:
                break
    except Exception:
        result['diagnostics']['truncated'] = True

    if matching_reports > len(candidates):
        result['diagnostics']['truncated'] = True

    result['diagnostics']['files_seen'] = len(candidates)
    seen_runs = set()
    events = []
    for _, path in sorted(candidates, reverse=True):
        report = _read_json_file(path, {})
        metadata = report.get('metadata') if isinstance(report, dict) else None
        if not isinstance(metadata, dict) or metadata.get('backend') != 'antigravity_cli':
            continue
        usage = metadata.get('worker_usage')
        run_id = metadata.get('run_id')
        finished = _parse_iso_utc(metadata.get('finished_at'))
        if not isinstance(usage, dict) or not isinstance(run_id, str) or not run_id or not finished:
            result['diagnostics']['malformed'] += 1
            continue
        required_usage_fields = ('total_tokens', 'input_tokens', 'output_tokens')
        if any(not isinstance(usage.get(field), int) or isinstance(usage.get(field), bool) or usage.get(field) < 0
               for field in required_usage_fields):
            result['diagnostics']['malformed'] += 1
            continue
        if ('cache_read_tokens' in usage and
                (not isinstance(usage.get('cache_read_tokens'), int) or
                 isinstance(usage.get('cache_read_tokens'), bool) or
                 usage.get('cache_read_tokens') < 0)):
            result['diagnostics']['malformed'] += 1
            continue
        total = usage['total_tokens']
        if total <= 0:
            result['diagnostics']['malformed'] += 1
            continue
        if run_id in seen_runs:
            result['diagnostics']['duplicates'] += 1
            continue
        seen_runs.add(run_id)
        events.append({
            'run_id': run_id,
            'timestamp': finished.isoformat(),
            'total_tokens': total,
            'input_tokens': usage['input_tokens'],
            'output_tokens': usage['output_tokens'],
            'model': str(metadata.get('model') or 'unknown')[:128],
        })

    events.sort(key=lambda item: item['timestamp'])
    five_cutoff = now_utc - timedelta(hours=5)
    week_cutoff = now_utc - timedelta(days=7)
    for event in events:
        event_dt = _parse_iso_utc(event['timestamp'])
        if not event_dt or event_dt > now_utc:
            continue
        if event_dt > week_cutoff:
            result['weekly_tokens'] += event['total_tokens']
            result['weekly_runs'] += 1
        if event_dt > five_cutoff:
            result['five_hour_tokens'] += event['total_tokens']
            result['five_hour_runs'] += 1

    result['available'] = True
    result['events'] = events
    result['diagnostics']['reports_counted'] = len(events)
    return result


def _worker_tokens_at(events, observed_at, window_seconds):
    at = _parse_iso_utc(observed_at) if not isinstance(observed_at, datetime) else observed_at
    if not at:
        return 0
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    else:
        at = at.astimezone(timezone.utc)
    cutoff = at - timedelta(seconds=max(0, int(window_seconds)))
    total = 0
    for event in events or []:
        event_dt = _parse_iso_utc(event.get('timestamp')) if isinstance(event, dict) else None
        if event_dt and cutoff < event_dt <= at:
            total += _safe_nonnegative_int(event.get('total_tokens'))
    return total


def _worker_usage_between(events, start_at, end_at):
    start = _parse_iso_utc(start_at) if not isinstance(start_at, datetime) else start_at
    end = _parse_iso_utc(end_at) if not isinstance(end_at, datetime) else end_at
    if not start or not end:
        return 0, 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    total = 0
    count = 0
    for event in events or []:
        event_dt = _parse_iso_utc(event.get('timestamp')) if isinstance(event, dict) else None
        if event_dt and start < event_dt <= end:
            total += _safe_nonnegative_int(event.get('total_tokens'))
            count += 1
    return total, count


def load_quota_observations():
    """Load versioned quota observations. Missing/corrupt data falls back to an empty store."""
    try:
        data = _read_json_file(QUOTA_OBSERVATIONS_FILE, {'version': 1, 'accounts': {}})
        if not isinstance(data, dict):
            raise ValueError('quota observation store must be an object')
        data.setdefault('version', 1)
        data.setdefault('accounts', {})
        return data
    except Exception:
        return {'version': 1, 'accounts': {}}


def save_quota_observations(data):
    data = dict(data or {})
    data['version'] = 1
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    data.setdefault('accounts', {})
    _atomic_write_json(QUOTA_OBSERVATIONS_FILE, data)


def record_capacity_calibration_event(store, account_email, bucket, calibration,
                                      worker_calibration=None, cycle_id='default',
                                      timestamp=None):
    """Persist a bounded capacity-state event only when evidence/state changes."""
    if bucket not in ('gemini_5h', 'gemini_weekly'):
        return None
    account_key = account_email or '__unknown__'
    accounts = store.setdefault('accounts', {})
    account = accounts.setdefault(account_key, {})
    events_by_bucket = account.setdefault('capacity_events', {})
    events = events_by_bucket.setdefault(bucket, [])
    calibration = calibration if isinstance(calibration, dict) else {}
    worker_calibration = worker_calibration if isinstance(worker_calibration, dict) else {}
    reasons = list(calibration.get('retained_reasons') or [])
    reasons.extend(worker_calibration.get('retained_reasons') or [])
    event = {
        'timestamp': timestamp or datetime.now(timezone.utc).isoformat(),
        'bucket': bucket,
        'cycle_id': cycle_id or 'default',
        'transcript_capacity_tokens': _safe_nonnegative_int(calibration.get('capacity_tokens')),
        'worker_capacity_tokens': _safe_nonnegative_int(
            worker_calibration.get('capacity_tokens', calibration.get('worker_capacity_tokens'))),
        'mixed_capacity_tokens': calibration.get('mixed_capacity_tokens', worker_calibration.get('mixed_capacity_tokens')),
        'confidence': float(calibration.get('confidence') or worker_calibration.get('confidence') or 0.0),
        'method': calibration.get('method') or worker_calibration.get('method') or 'prior_retained',
        'evidence_count': _safe_nonnegative_int(calibration.get('observations')),
        'pair_count': _safe_nonnegative_int(calibration.get('usable_pairs', worker_calibration.get('pair_count'))),
        'accepted': bool(calibration.get('accepted') or worker_calibration.get('accepted')),
        'recent_cycle_capacity_tokens': _safe_nonnegative_int(calibration.get('recent_cycle_capacity_tokens')),
        'recent_cycle_confidence': float(calibration.get('recent_cycle_confidence') or 0.0),
        'recent_cycle_evidence_count': _safe_nonnegative_int(calibration.get('recent_cycle_evidence_count')),
        'recent_cycle_pair_count': _safe_nonnegative_int(calibration.get('recent_cycle_pair_count')),
        'recent_cycle_id': calibration.get('recent_cycle_id'),
        'recent_cycle_evidence_id': calibration.get('recent_cycle_evidence_id'),
        'recent_cycle_accepted': bool(calibration.get('recent_cycle_accepted')),
        'reason': ';'.join(dict.fromkeys(str(r) for r in reasons if r)) or (
            'accepted_evidence' if calibration.get('accepted') or worker_calibration.get('accepted')
            else 'prior_retained'),
        'identifiability': calibration.get('identifiability') or worker_calibration.get('identifiability') or {},
        'source_mix_dependent': True,
    }
    event['transcript_capacity'] = event['transcript_capacity_tokens']
    event['worker_capacity'] = event['worker_capacity_tokens']
    event['mixed_capacity'] = event['mixed_capacity_tokens']
    # Avoid float noise generating duplicate events on repeated identical saves.
    signature_keys = (
        'bucket', 'cycle_id', 'transcript_capacity_tokens', 'worker_capacity_tokens',
        'mixed_capacity_tokens', 'confidence', 'method', 'evidence_count',
        'pair_count', 'accepted', 'recent_cycle_capacity_tokens', 'recent_cycle_confidence',
        'recent_cycle_evidence_count', 'recent_cycle_pair_count', 'recent_cycle_id',
        'recent_cycle_evidence_id', 'recent_cycle_accepted', 'reason', 'identifiability',
    )
    signature = tuple(json.dumps(event.get(key), sort_keys=True) for key in signature_keys)
    if events:
        previous = events[-1]
        previous_signature = tuple(json.dumps(previous.get(key), sort_keys=True) for key in signature_keys)
        if signature == previous_signature:
            return previous
    events.append(event)
    if len(events) > 200:
        del events[:-200]
    return event


def get_capacity_calibration_history(store, account_email):
    """Return bounded source-separated capacity histories for API/time-series consumers."""
    account = (store or {}).get('accounts', {}).get(account_email or '__unknown__', {})
    raw = account.get('capacity_events', {}) if isinstance(account, dict) else {}
    return {
        'gemini_5h': list(raw.get('gemini_5h') or [])[-200:],
        'gemini_weekly': list(raw.get('gemini_weekly') or [])[-200:],
    }


def _active_account_email():
    try:
        data = _read_json_file(ACCOUNTS_FILE, {})
        if isinstance(data, dict):
            return data.get('active_email')
    except Exception:
        pass
    return None


@_serialize_persistence
def load_real_quota_store(migrate_legacy=True):
    """Load account-scoped quota state, migrating the old single-account file once."""
    raw = _read_json_file(REAL_QUOTAS_FILE, {})

    if not isinstance(raw, dict):
        raw = {}

    if isinstance(raw.get('accounts'), dict):
        raw.setdefault('version', 2)
        return raw

    # Backward compatibility: older builds stored one unscoped quota profile.
    # Attach that legacy profile to the account that is active at migration time
    # so it cannot leak into subsequently selected accounts.
    owner = _active_account_email() or '__unknown__'
    store = {
        'version': 2,
        'accounts': {owner: dict(raw)} if raw else {},
        'updated_at': raw.get('updated_at') if raw else None,
    }
    if migrate_legacy and raw:
        try:
            _atomic_write_json(REAL_QUOTAS_FILE, store)
        except Exception:
            pass
    return store


def load_real_quota_profile(account_email=None):
    account_key = account_email or _active_account_email() or '__unknown__'
    store = load_real_quota_store(migrate_legacy=True)
    profile = store.get('accounts', {}).get(account_key, {})
    return dict(profile) if isinstance(profile, dict) else {}


@_serialize_persistence
def save_real_quota_profile(profile, account_email=None):
    account_key = account_email or _active_account_email() or '__unknown__'
    store = load_real_quota_store(migrate_legacy=True)
    store.setdefault('accounts', {})[account_key] = dict(profile or {})
    store['version'] = 2
    store['updated_at'] = (profile or {}).get('updated_at') or datetime.now(timezone.utc).isoformat()
    _atomic_write_json(REAL_QUOTAS_FILE, store)
    return store


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _local_day_start_utc(now_utc=None):
    """Return the start of the current system-local day expressed in UTC."""
    current = now_utc if isinstance(now_utc, datetime) else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    local_now = current.astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc)


def _weighted_median(samples):
    """Return the weighted median for an iterable of (value, positive_weight)."""
    clean = sorted((float(v), max(0.000001, float(w))) for v, w in samples if v is not None)
    if not clean:
        return None
    threshold = sum(w for _, w in clean) / 2.0
    running = 0.0
    for value, weight in clean:
        running += weight
        if running >= threshold:
            return value
    return clean[-1][0]


def record_quota_observation(store, account_email, bucket, remaining_pct, tracked_used_tokens,
                             cycle_id='default', pct_rounding_half_width=0.5, timestamp=None,
                             token_estimator_id=None, worker_used_tokens=None):
    """Append a manual IDE quota reading without discarding prior calibration evidence."""
    if bucket not in QUOTA_BUCKET_DEFAULTS:
        raise ValueError(f'Unknown quota bucket: {bucket}')
    account_key = account_email or '__unknown__'
    accounts = store.setdefault('accounts', {})
    buckets = accounts.setdefault(account_key, {})
    history = buckets.setdefault(bucket, [])

    pct = max(0.0, min(100.0, float(remaining_pct)))
    tracked = max(0, int(round(float(tracked_used_tokens or 0))))
    obs = {
        'timestamp': timestamp or datetime.now(timezone.utc).isoformat(),
        'remaining_pct': pct,
        'tracked_used_tokens': tracked,
        'cycle_id': cycle_id or 'default',
        'pct_rounding_half_width': max(0.0, float(pct_rounding_half_width)),
        'token_estimator_id': token_estimator_id or TOKEN_ESTIMATOR_ID,
    }
    if bucket.startswith('gemini_') and worker_used_tokens is not None:
        obs['worker_used_tokens'] = max(0, int(round(float(worker_used_tokens or 0))))
        obs['worker_estimator_id'] = GEMINI_WORKER_ESTIMATOR_ID

    if history:
        prev = history[-1]
        same_value = (
            prev.get('cycle_id', 'default') == obs['cycle_id'] and
            abs(float(prev.get('remaining_pct', -999)) - pct) < 1e-9 and
            int(prev.get('tracked_used_tokens', -1)) == tracked and
            (prev.get('token_estimator_id') or LEGACY_TOKEN_ESTIMATOR_ID) == obs['token_estimator_id'] and
            prev.get('worker_used_tokens') == obs.get('worker_used_tokens')
        )
        if same_value:
            # Re-saving an unchanged form should not inflate the evidence count.
            return prev
        prev_dt = _parse_iso_utc(prev.get('timestamp'))
        now_dt = _parse_iso_utc(obs['timestamp'])
        if prev_dt and now_dt and abs((now_dt - prev_dt).total_seconds()) < 30:
            same_reading = (
                prev.get('cycle_id', 'default') == obs['cycle_id'] and
                abs(float(prev.get('remaining_pct', -999)) - pct) < 1e-9 and
                int(prev.get('tracked_used_tokens', -1)) == tracked and
                (prev.get('token_estimator_id') or LEGACY_TOKEN_ESTIMATOR_ID) == obs['token_estimator_id'] and
                prev.get('worker_used_tokens') == obs.get('worker_used_tokens')
            )
            if not same_reading:
                history.append(obs)
                if len(history) > 200:
                    del history[:-200]
                return obs
            history[-1] = obs
            return obs

    history.append(obs)
    if len(history) > 200:
        del history[:-200]
    return obs


def _multisource_observations(observations):
    """Return comparable transcript/worker readings without changing cycle IDs."""
    valid = []
    for raw in observations or []:
        if not isinstance(raw, dict):
            continue
        try:
            pct = float(raw.get('remaining_pct'))
            transcript = int(round(float(raw.get('tracked_used_tokens', 0))))
            worker_raw = raw.get('worker_used_tokens')
            worker = None if worker_raw is None else int(round(float(worker_raw)))
            ts = _parse_iso_utc(raw.get('timestamp'))
            token_id = raw.get('token_estimator_id') or LEGACY_TOKEN_ESTIMATOR_ID
            worker_id = raw.get('worker_estimator_id')
            if (ts and 0.0 <= pct <= 100.0 and transcript >= 0 and
                    token_id == TOKEN_ESTIMATOR_ID and
                    (worker is None or (worker >= 0 and worker_id == GEMINI_WORKER_ESTIMATOR_ID))):
                valid.append({
                    **raw,
                    '_pct': pct,
                    '_transcript': transcript,
                    '_worker': worker,
                    '_dt': ts,
                    '_rounding': max(0.0, float(raw.get('pct_rounding_half_width', 0.5))),
                })
        except Exception:
            continue
    valid.sort(key=lambda item: item['_dt'])
    return valid


def _multisource_pairs(valid, window_seconds=None):
    """Build signed same-cycle pair deltas; reset boundaries never enter a fit."""
    pairs = []
    max_pair_age = (window_seconds * 2.0) if window_seconds else None
    for index, left in enumerate(valid):
        for right in valid[index + 1:]:
            if left.get('cycle_id', 'default') != right.get('cycle_id', 'default'):
                continue
            elapsed = (right['_dt'] - left['_dt']).total_seconds()
            if elapsed <= 0 or (max_pair_age and elapsed > max_pair_age):
                continue
            dt = right['_transcript'] - left['_transcript']
            dw = None
            if left.get('_worker') is not None and right.get('_worker') is not None:
                dw = right['_worker'] - left['_worker']
            drop = left['_pct'] - right['_pct']
            rounding = left['_rounding'] + right['_rounding']
            if abs(drop) <= max(0.15, rounding * 0.75):
                continue
            if dt == 0 and (dw is None or dw == 0):
                continue
            # If both sources move in the same direction, the visible percentage
            # must move in the corresponding direction. Opposing source deltas
            # are retained as signed recovery/expiry evidence for the fit.
            if dw is not None and dt * dw > 0 and drop * dt <= 0:
                continue
            if dw is None and drop * dt <= 0:
                continue
            weight = max(0.1, abs(drop) - rounding * 0.5)
            weight *= min(10.0, max(1.0, (abs(dt) / 10000.0) ** 0.5))
            if dw is not None:
                weight *= min(10.0, max(1.0, (abs(dw) / 10000.0) ** 0.5))
            pairs.append({
                'transcript_delta': dt,
                'worker_delta': dw,
                'pct_drop': drop,
                'rounding': rounding,
                'weight': weight,
                'left_timestamp': left.get('timestamp'),
                'right_timestamp': right.get('timestamp'),
                'cycle_id': right.get('cycle_id', 'default'),
            })
    return pairs


def _fit_nonnegative_source_coefficients(pairs):
    """Robust weighted fit of pct-drop = ct*transcript + cw*worker.

    Coefficients are percentage points per token. The returned rank flag is
    deliberately conservative: a single source movement cannot identify both
    source capacities.
    """
    rows = [p for p in pairs if p.get('worker_delta') is not None]
    if not rows:
        return {'transcript': None, 'worker': None, 'rank_identifiable': False,
                'transcript_identifiable': False, 'worker_identifiable': False,
                'inliers': [], 'residual_pct': None}

    def solve(items, one_source=None):
        if one_source:
            den = sum(float(p[one_source]) ** 2 * p['weight'] for p in items)
            if den <= 0:
                return None
            return sum(float(p[one_source]) * p['pct_drop'] * p['weight'] for p in items) / den
        s_tt = sum(p['transcript_delta'] ** 2 * p['weight'] for p in items)
        s_ww = sum(p['worker_delta'] ** 2 * p['weight'] for p in items)
        s_tw = sum(p['transcript_delta'] * p['worker_delta'] * p['weight'] for p in items)
        s_ty = sum(p['transcript_delta'] * p['pct_drop'] * p['weight'] for p in items)
        s_wy = sum(p['worker_delta'] * p['pct_drop'] * p['weight'] for p in items)
        determinant = s_tt * s_ww - s_tw * s_tw
        if determinant <= max(1.0, s_tt * s_ww) * 1e-8:
            return None
        return ((s_ty * s_ww - s_wy * s_tw) / determinant,
                (s_wy * s_tt - s_ty * s_tw) / determinant)

    all_fit = solve(rows)
    rank_identifiable = all_fit is not None
    if all_fit is None:
        # One-dimensional evidence remains useful for that source only; the
        # other coefficient must be retained from its prior.
        t_fit = solve(rows, 'transcript_delta') if any(p['transcript_delta'] for p in rows) else None
        w_fit = solve(rows, 'worker_delta') if any(p['worker_delta'] for p in rows) else None
        return {
            'transcript': max(0.0, t_fit) if t_fit is not None else None,
            'worker': max(0.0, w_fit) if w_fit is not None else None,
            'rank_identifiable': False,
            'transcript_identifiable': bool(t_fit is not None and not any(p['worker_delta'] for p in rows)),
            'worker_identifiable': bool(w_fit is not None and not any(p['transcript_delta'] for p in rows)),
            'inliers': rows,
            'residual_pct': None,
        }

    ct, cw = all_fit
    if ct <= 0 or cw <= 0:
        return {
            'transcript': max(0.0, ct), 'worker': max(0.0, cw),
            'rank_identifiable': False, 'transcript_identifiable': False,
            'worker_identifiable': False, 'inliers': rows, 'residual_pct': None,
        }

    def residual(row, coefficients=(ct, cw)):
        return row['pct_drop'] - (coefficients[0] * row['transcript_delta'] + coefficients[1] * row['worker_delta'])

    residuals = [abs(residual(p)) for p in rows]
    median_residual = _weighted_median([(v, 1.0) for v in residuals]) or 0.0
    tolerance = max(0.25, median_residual * 2.5, max((p['rounding'] for p in rows), default=0.5) * 1.5)
    inliers = [p for p in rows if abs(residual(p)) <= tolerance]
    refined = solve(inliers) if len(inliers) >= 3 else None
    if refined is not None and refined[0] > 0 and refined[1] > 0:
        ct, cw = refined
    final_residual = _weighted_median([(abs(residual(p, (ct, cw))), 1.0) for p in inliers]) if inliers else None
    return {
        'transcript': ct, 'worker': cw, 'rank_identifiable': True,
        'transcript_identifiable': True, 'worker_identifiable': True,
        'inliers': inliers, 'residual_pct': final_residual,
    }


def _bounded_capacity(value, prior, minimum=1000, maximum=2_000_000_000):
    try:
        candidate = float(value)
    except Exception:
        candidate = 0.0
    if not math.isfinite(candidate) or candidate < minimum or candidate > maximum:
        return max(0, int(round(float(prior or 0))))
    return int(round(candidate))


def _estimate_multisource_capacity(observations, fallback_capacity, worker_fallback_capacity=0,
                                   window_seconds=None):
    """Estimate separate source capacities plus a display-only mixed capacity."""
    fallback = max(0, int(round(float(fallback_capacity or 0))))
    worker_prior = max(0, int(round(float(worker_fallback_capacity or 0))))
    valid = _multisource_observations(observations)
    pairs = _multisource_pairs(valid, window_seconds)
    pairs_with_worker = [p for p in pairs if p.get('worker_delta') is not None]
    fit = _fit_nonnegative_source_coefficients(pairs_with_worker)
    ct = fallback
    cw = worker_prior
    reasons = []
    accepted = False
    if fit.get('transcript_identifiable') and fit.get('transcript', 0) > 0:
        proposed = _bounded_capacity(100.0 / fit['transcript'], fallback)
        if proposed > 0 and proposed != fallback:
            ct, accepted = proposed, True
    elif pairs_with_worker:
        reasons.append('transcript_coefficient_unidentifiable_prior_retained')
    if fit.get('worker_identifiable') and fit.get('worker', 0) > 0:
        proposed = _bounded_capacity(100.0 / fit['worker'], worker_prior, minimum=100_000)
        if proposed > 0 and proposed != worker_prior:
            cw, accepted = proposed, True
    elif pairs_with_worker:
        reasons.append('worker_coefficient_unidentifiable_prior_retained')

    mixed_samples = []
    for pair in pairs:
        denominator = abs(pair['pct_drop'])
        if denominator <= max(0.15, pair['rounding']):
            continue
        total_delta = abs(pair['transcript_delta'] + (pair.get('worker_delta') or 0))
        candidate = 100.0 * total_delta / denominator
        if 1000 <= candidate <= 2_000_000_000:
            mixed_samples.append((candidate, pair['weight']))
    mixed = _weighted_median(mixed_samples)
    mixed = _bounded_capacity(mixed, 0) if mixed is not None else None
    rounding_tolerance = max((float(pair.get('rounding') or 0.0) for pair in pairs), default=0.5)
    if not pairs:
        reasons.append('no_comparable_same_cycle_pairs')
    elif not fit.get('rank_identifiable'):
        reasons.append('source_movement_not_identifiable')

    confidence = 0.0
    if pairs:
        confidence = min(0.98, 0.15 + 0.12 * min(len(pairs), 5))
        if fit.get('rank_identifiable'):
            confidence += 0.20
        if fit.get('inliers'):
            confidence += 0.12 * min(1.0, len(fit['inliers']) / 5.0)
        confidence = min(0.98, confidence)
    status = 'stable' if confidence >= 0.82 else ('calibrated' if confidence >= 0.55 else 'learning')
    method = 'multi_source_robust' if accepted else 'prior_retained'
    if fit.get('rank_identifiable') and accepted:
        method = 'multi_source_robust'
    pct_by_cycle = {}
    for item in valid:
        pct_by_cycle.setdefault(item.get('cycle_id', 'default'), []).append(item['_pct'])
    pct_span = max((max(values) - min(values) for values in pct_by_cycle.values() if values), default=0.0)
    support_pct = (len(fit.get('inliers') or []) / len(pairs_with_worker) * 100.0
                   if pairs_with_worker else 0.0)
    return {
        'capacity_tokens': ct,
        'transcript_capacity_tokens': ct,
        'one_percent_tokens': round(ct / 100.0, 1) if ct else 0,
        'worker_capacity_tokens': cw,
        'mixed_capacity_tokens': mixed,
        'worker_capacity': cw,
        'mixed_capacity': mixed,
        'source_mix_dependent': True,
        'source_capacities': {
            'transcript_tokens': ct,
            'worker_tokens': cw,
            'mixed_tokens': mixed,
        },
        'observations': len(valid),
        'usable_pairs': len(pairs),
        'worker_pairs': len(pairs_with_worker),
        'supported_observations': len(fit.get('inliers') or []),
        'support_pct': round(support_pct, 1),
        'confidence': round(confidence, 4),
        'confidence_pct': round(confidence * 100.0, 1),
        'status': status,
        'method': method,
        'estimator_id': 'gemini_multisource_pair_fit_v1',
        'refinement_method': 'robust_nonnegative_two_source_fit',
        'bootstrap_capacity_tokens': None,
        'observed_pct_span': round(pct_span, 3),
        'dispersion_pct': round(float(fit['residual_pct']), 2) if fit.get('residual_pct') is not None else None,
        'token_estimator_id': TOKEN_ESTIMATOR_ID,
        'worker_estimator_id': GEMINI_WORKER_ESTIMATOR_ID,
        'rounding_tolerance_pct': round(rounding_tolerance, 4),
        'signed_net_deltas': True,
        'same_cycle_only': True,
        'identifiability': {
            'rank_identifiable': bool(fit.get('rank_identifiable')),
            'transcript': bool(fit.get('transcript_identifiable')),
            'worker': bool(fit.get('worker_identifiable')),
        },
        'retained_reasons': reasons,
        'accepted': bool(accepted),
        'residual_pct': round(float(fit['residual_pct']), 4) if fit.get('residual_pct') is not None else None,
        'latest_observation': ({
            'timestamp': valid[-1].get('timestamp'),
            'remaining_pct': valid[-1]['_pct'],
            'tracked_used_tokens': valid[-1]['_transcript'],
            'worker_used_tokens': valid[-1].get('_worker'),
            'cycle_id': valid[-1].get('cycle_id', 'default'),
        } if valid else None),
        'latest_trusted_observation': None,
        'latest_observation_trusted': False,
        'outlier_observations': max(0, len(pairs_with_worker) - len(fit.get('inliers') or [])),
    }


def estimate_quota_capacity(observations, fallback_capacity, window_seconds=None,
                            worker_fallback_capacity=0):
    """Estimate transcript capacity, fitting Gemini sources jointly when available."""
    if any(isinstance(item, dict) and item.get('worker_used_tokens') is not None and
           item.get('worker_estimator_id') == GEMINI_WORKER_ESTIMATOR_ID
           for item in (observations or [])):
        return _estimate_multisource_capacity(
            observations, fallback_capacity, worker_fallback_capacity, window_seconds)
    return _estimate_transcript_only_capacity(observations, fallback_capacity, window_seconds)


def estimate_recent_cycle_capacity_candidate(observations, window_seconds=None,
                                             worker_fallback_capacity=0):
    """Estimate a source-clean transcript-capacity candidate from the latest cycle only.

    This is an early-warning signal. It never replaces the long-horizon estimator and
    never crosses a reset boundary. A candidate is emitted only after the latest cycle
    has enough independent evidence to pass the normal estimator's confidence gates.
    """
    valid = _multisource_observations(observations)
    if not valid:
        return None
    latest_cycle = valid[-1].get('cycle_id', 'default')
    cycle_observations = [item for item in valid if item.get('cycle_id', 'default') == latest_cycle]
    if len(cycle_observations) < 3:
        return None

    result = estimate_quota_capacity(
        cycle_observations, 0, window_seconds,
        worker_fallback_capacity if worker_fallback_capacity else 0)
    capacity = _safe_nonnegative_int(result.get('capacity_tokens'))
    confidence = float(result.get('confidence') or 0.0)
    evidence_count = _safe_nonnegative_int(result.get('observations'))
    pair_count = _safe_nonnegative_int(result.get('usable_pairs'))
    identifiability = result.get('identifiability') if isinstance(result.get('identifiability'), dict) else {}
    accepted = bool(result.get('accepted') or result.get('method') == 'multi_observation_robust')
    if identifiability.get('transcript') is False:
        accepted = False
    if (capacity <= 0 or confidence < CAPACITY_SHIFT_MIN_CONFIDENCE or
            evidence_count < 3 or pair_count < 2 or not accepted):
        return None

    latest = cycle_observations[-1]
    latest_timestamp = latest.get('timestamp')
    evidence_id = f'{latest_cycle}|{latest_timestamp}|{evidence_count}|{pair_count}'
    return {
        'capacity_tokens': capacity,
        'confidence': round(confidence, 4),
        'evidence_count': evidence_count,
        'pair_count': pair_count,
        'cycle_id': latest_cycle,
        'latest_observation_timestamp': latest_timestamp,
        'evidence_id': evidence_id,
        'accepted': True,
        'method': result.get('method'),
    }


def _estimate_transcript_only_capacity(observations, fallback_capacity, window_seconds=None):
    """Estimate capacity from many rolling-token/% readings using a robust pairwise median."""
    fallback = max(0, int(round(float(fallback_capacity or 0))))
    valid = []
    for raw in observations or []:
        try:
            pct = float(raw.get('remaining_pct'))
            tracked = int(round(float(raw.get('tracked_used_tokens', 0))))
            ts = _parse_iso_utc(raw.get('timestamp'))
            estimator_id = raw.get('token_estimator_id') or LEGACY_TOKEN_ESTIMATOR_ID
            if estimator_id != TOKEN_ESTIMATOR_ID:
                continue
            if 0.0 <= pct <= 100.0 and tracked >= 0 and ts:
                item = dict(raw)
                item['_pct'] = pct
                item['_tracked'] = tracked
                item['_dt'] = ts
                item['_rounding'] = max(0.0, float(raw.get('pct_rounding_half_width', 0.5)))
                item['_token_estimator_id'] = estimator_id
                valid.append(item)
        except Exception:
            continue

    # Keep evidence from older cycles: capacity is a property of the quota bucket,
    # while cycle_id only prevents us from taking a slope across a reset boundary.
    valid.sort(key=lambda x: x['_dt'])

    pair_samples = []
    max_pair_age = (window_seconds * 2.0) if window_seconds else None
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            a, b = valid[i], valid[j]
            if a.get('cycle_id', 'default') != b.get('cycle_id', 'default'):
                continue
            elapsed = (b['_dt'] - a['_dt']).total_seconds()
            if elapsed <= 0 or (max_pair_age and elapsed > max_pair_age):
                continue
            delta_tokens = b['_tracked'] - a['_tracked']
            delta_pct = b['_pct'] - a['_pct']
            rounding_noise = a['_rounding'] + b['_rounding']
            if abs(delta_pct) <= max(0.15, rounding_noise * 0.75):
                continue
            if abs(delta_tokens) < 250:
                continue
            # For a clean pair, remaining % and net rolling usage must move oppositely.
            if delta_tokens * delta_pct >= 0:
                continue
            candidate = abs(100.0 * delta_tokens / delta_pct)
            if candidate < 1000 or candidate > 1_000_000_000:
                continue
            signal_pct = max(0.1, abs(delta_pct) - rounding_noise * 0.5)
            token_signal = min(10.0, max(1.0, (abs(delta_tokens) / 10000.0) ** 0.5))
            pair_samples.append((candidate, signal_pct * token_signal))

    method = 'prior'
    refinement_method = None
    dispersion_ratio = None
    supported_observations = 0
    support_ratio = 0.0
    if pair_samples:
        # RANSAC-like consensus: a candidate capacity is good when it makes many
        # observations in the same cycle agree on the same hidden intercept:
        # remaining_pct + 100 * tracked / capacity = cycle_anchor_pct.
        # This is much harder for one bad manual percentage to dominate than
        # weighting pair slopes by their raw percentage span.
        cycle_groups = {}
        for obs in valid:
            cycle_groups.setdefault(obs.get('cycle_id', 'default'), []).append(obs)

        def score_candidate(candidate):
            residuals = []
            support = 0
            eligible = 0
            for group in cycle_groups.values():
                if len(group) < 2:
                    continue
                intercepts = [o['_pct'] + (100.0 * o['_tracked'] / candidate) for o in group]
                center = _weighted_median([(v, 1.0) for v in intercepts])
                for o, intercept in zip(group, intercepts):
                    eligible += 1
                    residual = abs(intercept - center)
                    residuals.append((residual, 1.0))
                    tolerance_pct = max(1.25, o['_rounding'] * 2.0 + 0.25)
                    if residual <= tolerance_pct:
                        support += 1
            median_residual = _weighted_median(residuals) if residuals else 999.0
            return support, eligible, median_residual

        scored = []
        for candidate, weight in pair_samples:
            support, eligible, residual = score_candidate(candidate)
            scored.append((candidate, weight, support, eligible, residual))

        # Prefer maximum observation consensus, then the smallest robust residual.
        best = max(scored, key=lambda x: (x[2], -x[4], x[1]))
        rough = best[0]
        supported_observations = best[2]
        support_ratio = (best[2] / best[3]) if best[3] else 0.0

        # Once a consensus slope is found, use nearby pair estimates to smooth
        # percentage rounding noise without letting remote outliers back in.
        tolerance = max(1000.0, rough * 0.30)
        inliers = [(v, w) for v, w in pair_samples if abs(v - rough) <= tolerance]
        capacity = _weighted_median(inliers) or rough

        # Pairwise ratios are robust, but rounded percentages can bias their
        # median noticeably when each visible step is only ~1 percentage point.
        # After RANSAC has identified a trustworthy neighborhood, refine the
        # common slope with a fixed-effect regression: every cycle gets its own
        # intercept while all cycles share one token/% slope. Centering within
        # each cycle means reset boundaries cannot leak into the slope.
        provisional_trusted = []
        for group in cycle_groups.values():
            if len(group) < 2:
                continue
            intercepts = [o['_pct'] + (100.0 * o['_tracked'] / capacity) for o in group]
            center = _weighted_median([(v, 1.0) for v in intercepts])
            for o, intercept in zip(group, intercepts):
                tolerance_pct = max(1.5, o['_rounding'] * 2.0 + 0.5)
                if abs(intercept - center) <= tolerance_pct:
                    provisional_trusted.append(o)

        regression_num = 0.0
        regression_den = 0.0
        trusted_by_cycle = {}
        for obs in provisional_trusted:
            trusted_by_cycle.setdefault(obs.get('cycle_id', 'default'), []).append(obs)
        for group in trusted_by_cycle.values():
            if len(group) < 2:
                continue
            x_mean = sum(o['_tracked'] for o in group) / len(group)
            y_mean = sum(o['_pct'] for o in group) / len(group)
            for o in group:
                dx = o['_tracked'] - x_mean
                dy = o['_pct'] - y_mean
                regression_num += dx * dy
                regression_den += dx * dx

        if regression_den > 0:
            slope_pct_per_token = regression_num / regression_den
            if slope_pct_per_token < 0:
                refined = -100.0 / slope_pct_per_token
                if 1000 <= refined <= 1_000_000_000 and abs(refined - rough) <= tolerance:
                    refined_support, refined_eligible, refined_residual = score_candidate(refined)
                    refined_support_ratio = (refined_support / refined_eligible) if refined_eligible else 0.0
                    # Never trade away RANSAC consensus just to obtain a smoother
                    # slope. A small residual allowance handles percentage
                    # quantization around otherwise-equivalent fits.
                    if refined_support >= best[2] and refined_residual <= best[4] + 0.20:
                        capacity = refined
                        refinement_method = 'within_cycle_least_squares'
                        supported_observations = refined_support
                        support_ratio = refined_support_ratio

        # Recompute dispersion around the refined capacity so the confidence
        # diagnostics describe the value we actually return.
        final_tolerance = max(1000.0, capacity * 0.30)
        inliers = [(v, w) for v, w in pair_samples if abs(v - capacity) <= final_tolerance]
        rel_devs = [(abs(v - capacity) / max(1.0, capacity), w) for v, w in inliers]
        dispersion_ratio = _weighted_median(rel_devs) or 0.0
        method = 'multi_observation_robust'
        usable_pairs = len(inliers)
    else:
        usable_pairs = 0
        capacity = None

    bootstrap_capacity = None
    if capacity is None and valid:
        latest = valid[-1]
        consumed_fraction = (100.0 - latest['_pct']) / 100.0
        if latest['_tracked'] > 0 and consumed_fraction >= 0.01:
            bootstrap = latest['_tracked'] / consumed_fraction
            if 1000 <= bootstrap <= 1_000_000_000:
                # A single rounded percentage is useful as a hint, but is not
                # trusted enough to move the active capacity by itself.
                bootstrap_capacity = max(0, int(round(bootstrap)))
                capacity = fallback or bootstrap_capacity
                method = 'prior_with_single_observation_hint'

    if capacity is None or capacity <= 0:
        capacity = fallback or 1000
        method = 'prior'

    capacity = max(0, int(round(capacity)))
    # Confidence must only credit percentage movement that occurred inside one
    # cycle. A reset can create a huge 10% -> 100% jump, but that jump contains
    # no slope information and must not make the estimator look more certain.
    pct_span = 0.0
    if valid:
        pct_by_cycle = {}
        for obs in valid:
            pct_by_cycle.setdefault(obs.get('cycle_id', 'default'), []).append(obs['_pct'])
        pct_span = max(
            (max(values) - min(values) for values in pct_by_cycle.values() if values),
            default=0.0,
        )
    if method == 'multi_observation_robust':
        pair_factor = min(1.0, usable_pairs / 6.0)
        obs_factor = min(1.0, len(valid) / 6.0)
        span_factor = min(1.0, pct_span / 10.0)
        dispersion_quality = max(0.0, 1.0 - min(1.0, (dispersion_ratio or 0.0) / 0.5))
        support_quality = max(0.0, min(1.0, support_ratio))
        confidence = min(0.98, 0.12 + 0.28 * pair_factor + 0.14 * obs_factor + 0.16 * span_factor + 0.10 * dispersion_quality + 0.20 * support_quality)
    elif method == 'prior_with_single_observation_hint':
        confidence = 0.12
    else:
        confidence = 0.05 if valid else 0.0

    if confidence >= 0.82 and len(valid) >= 5:
        status = 'stable'
    elif confidence >= 0.55 and len(valid) >= 3:
        status = 'calibrated'
    else:
        status = 'learning'

    latest_out = None
    latest_trusted_out = None
    outlier_observations = 0
    if valid:
        latest = valid[-1]
        latest_cycle_id = latest.get('cycle_id', 'default')
        latest_out = {
            'timestamp': latest.get('timestamp'),
            'remaining_pct': latest['_pct'],
            'tracked_used_tokens': latest['_tracked'],
            'cycle_id': latest_cycle_id,
        }
        if latest.get('worker_used_tokens') is not None:
            latest_out['worker_used_tokens'] = _safe_nonnegative_int(latest.get('worker_used_tokens'))
            latest_out['worker_estimator_id'] = latest.get('worker_estimator_id')
        latest_trusted = latest
        if method == 'multi_observation_robust' and capacity > 0:
            trusted = []
            for cycle_id in {o.get('cycle_id', 'default') for o in valid}:
                group = [o for o in valid if o.get('cycle_id', 'default') == cycle_id]
                if len(group) < 2:
                    trusted.extend(group)
                    continue
                intercepts = [o['_pct'] + (100.0 * o['_tracked'] / capacity) for o in group]
                center = _weighted_median([(v, 1.0) for v in intercepts])
                for o, intercept in zip(group, intercepts):
                    tolerance_pct = max(1.5, o['_rounding'] * 2.0 + 0.5)
                    if abs(intercept - center) <= tolerance_pct:
                        trusted.append(o)
                    else:
                        outlier_observations += 1
            if trusted:
                # Capacity may legitimately learn from older cycles, but a live
                # percentage anchor must never jump back across a reset boundary.
                # Prefer the newest trusted reading from the current cycle. If
                # every current-cycle reading is an outlier, keep the latest raw
                # reading as the anchor instead of resurrecting an older cycle.
                current_cycle_trusted = [
                    o for o in trusted
                    if o.get('cycle_id', 'default') == latest_cycle_id
                ]
                if current_cycle_trusted:
                    latest_trusted = max(current_cycle_trusted, key=lambda o: o['_dt'])

        latest_trusted_out = {
            'timestamp': latest_trusted.get('timestamp'),
            'remaining_pct': latest_trusted['_pct'],
            'tracked_used_tokens': latest_trusted['_tracked'],
            'cycle_id': latest_trusted.get('cycle_id', 'default'),
        }
        if latest_trusted.get('worker_used_tokens') is not None:
            latest_trusted_out['worker_used_tokens'] = _safe_nonnegative_int(latest_trusted.get('worker_used_tokens'))
            latest_trusted_out['worker_estimator_id'] = latest_trusted.get('worker_estimator_id')

    return {
        'capacity_tokens': capacity,
        'one_percent_tokens': round(capacity / 100.0, 1) if capacity else 0,
        'observations': len(valid),
        'usable_pairs': usable_pairs,
        'supported_observations': supported_observations,
        'support_pct': round(support_ratio * 100.0, 1),
        'confidence': round(confidence, 4),
        'confidence_pct': round(confidence * 100.0, 1),
        'status': status,
        'method': method,
        'refinement_method': refinement_method,
        'token_estimator_id': TOKEN_ESTIMATOR_ID,
        'bootstrap_capacity_tokens': bootstrap_capacity,
        'observed_pct_span': round(pct_span, 3),
        'dispersion_pct': round((dispersion_ratio or 0.0) * 100.0, 2) if dispersion_ratio is not None else None,
        'latest_observation': latest_out,
        'latest_trusted_observation': latest_trusted_out,
        'latest_observation_trusted': bool(latest_out and latest_trusted_out and latest_out.get('timestamp') == latest_trusted_out.get('timestamp')),
        'outlier_observations': outlier_observations,
    }


def estimate_worker_quota_capacity(observations, transcript_capacity,
                                   fallback_capacity=0, window_seconds=None):
    """Estimate exact worker capacity without collapsing it into transcript units."""
    fallback = max(0, int(fallback_capacity or 0))
    result = _estimate_multisource_capacity(
        observations, transcript_capacity, fallback, window_seconds)
    worker_capacity = result.get('worker_capacity_tokens') or fallback

    # Preserve the historical single-pair behavior only when no prior exists.
    # This keeps old callers useful while the joint estimator still reports the
    # coefficient as unidentifiable and never uses it as a prediction prior.
    if (not result.get('identifiability', {}).get('worker') and not fallback and
            result.get('worker_pairs') == 1):
        valid = _multisource_observations(observations)
        pairs = _multisource_pairs(valid, window_seconds)
        pair = next((p for p in pairs if p.get('worker_delta') is not None), None)
        if pair:
            transcript_delta_pct = (
                100.0 * pair['transcript_delta'] / max(1, int(transcript_capacity or 0))
                if transcript_capacity else 0.0)
            residual = pair['pct_drop'] - transcript_delta_pct
            if residual * pair['worker_delta'] > 0 and abs(residual) >= 0.2:
                legacy = _bounded_capacity(abs(100.0 * pair['worker_delta'] / residual), 0, minimum=100_000)
                if legacy:
                    worker_capacity = legacy
                    result['retained_reasons'] = list(result.get('retained_reasons') or []) + [
                        'single_pair_legacy_compatibility_only']

    worker_identifiable = bool(result.get('identifiability', {}).get('worker'))
    method = result.get('method') if worker_identifiable else ('worker_observation_pair' if worker_capacity and not fallback else ('prior' if fallback else 'uncalibrated'))
    confidence = result.get('confidence', 0.0) if worker_identifiable else (0.0 if not worker_capacity else 0.12)
    return {
        'capacity_tokens': worker_capacity,
        'method': method,
        'confidence': round(confidence, 3),
        'confidence_pct': round(confidence * 100.0, 1),
        'pair_count': result.get('worker_pairs', 0),
        'observation_count': result.get('observations', 0),
        'worker_estimator_id': GEMINI_WORKER_ESTIMATOR_ID,
        'token_estimator_id': TOKEN_ESTIMATOR_ID,
        'source_mix_dependent': True,
        'identifiability': result.get('identifiability', {}),
        'retained_reasons': result.get('retained_reasons', []),
        'mixed_capacity_tokens': result.get('mixed_capacity_tokens'),
        'accepted': bool(worker_identifiable),
    }


def _transcript_capacity_history_without_worker_movement(observations):
    """Deprecated compatibility helper; worker movement is now fit jointly."""
    return [dict(raw) for raw in (observations or []) if isinstance(raw, dict)]


def predict_quota_from_anchor(tracked_used_tokens, capacity_tokens, calibration, legacy_pct=None,
                              legacy_tracked_tokens=None, worker_used_tokens=0,
                              worker_calibration=None, legacy_worker_tokens=None):
    """Project current quota from the latest valid % anchor plus net rolling-token movement."""
    capacity = max(0, int(capacity_tokens or 0))
    tracked = max(0, int(tracked_used_tokens or 0))
    worker_tracked = max(0, int(worker_used_tokens or 0))
    worker_capacity = max(0, int((worker_calibration or {}).get('capacity_tokens') or 0))
    worker_applied = False
    worker_delta = 0

    latest_obs = (calibration or {}).get('latest_observation')
    latest_trusted = (calibration or {}).get('latest_trusted_observation')

    latest = None
    if latest_obs and latest_trusted:
        dt_obs = _parse_iso_utc(latest_obs.get('timestamp'))
        dt_trusted = _parse_iso_utc(latest_trusted.get('timestamp'))
        if dt_obs and dt_trusted:
            latest = latest_obs if dt_obs >= dt_trusted else latest_trusted
        else:
            latest = latest_obs or latest_trusted
    else:
        latest = latest_obs or latest_trusted

    if capacity > 0 and latest:
        anchor_pct = float(latest.get('remaining_pct', 100.0))
        anchor_tracked = int(latest.get('tracked_used_tokens', 0))
        pct = anchor_pct - (100.0 * (tracked - anchor_tracked) / capacity)
        if worker_capacity > 0 and latest.get('worker_used_tokens') is not None:
            worker_delta = worker_tracked - int(latest.get('worker_used_tokens') or 0)
            pct -= 100.0 * worker_delta / worker_capacity
            worker_applied = True
        source = 'observation_anchor_delta'
    elif capacity > 0 and legacy_pct is not None and legacy_tracked_tokens is not None:
        pct = float(legacy_pct) - (100.0 * (tracked - int(legacy_tracked_tokens or 0)) / capacity)
        if worker_capacity > 0 and legacy_worker_tokens is not None:
            worker_delta = worker_tracked - int(legacy_worker_tokens or 0)
            pct -= 100.0 * worker_delta / worker_capacity
            worker_applied = True
        source = 'legacy_anchor_delta'
    elif capacity > 0:
        pct = 100.0 * (1.0 - tracked / capacity)
        source = 'tracked_tokens_only'
    else:
        pct = 0.0
        source = 'no_capacity'

    pct = max(0.0, min(100.0, pct))
    remaining = max(0, int(round(capacity * pct / 100.0))) if capacity > 0 else 0
    estimated_used = max(0, capacity - remaining) if capacity > 0 else tracked
    inferred_untracked = max(0, estimated_used - tracked)
    return {
        'percentage_remaining': round(pct, 1),
        'remaining_tokens': remaining,
        'estimated_used_tokens': estimated_used,
        'tracked_used_tokens': tracked,
        'inferred_untracked_tokens': inferred_untracked,
        'prediction_source': source,
        'worker_used_tokens': worker_tracked,
        'worker_capacity_tokens': worker_capacity,
        'worker_delta_tokens': worker_delta,
        'worker_prediction_applied': worker_applied,
        'worker_calibration': worker_calibration or {
            'capacity_tokens': 0, 'method': 'uncalibrated', 'confidence': 0.0,
            'worker_estimator_id': GEMINI_WORKER_ESTIMATOR_ID,
        },
    }

# ---- Quota & Limit Calculation Engine ----
def calculate_quotas(rolling_data, limits_config=None, active_email=None):
    now_utc = datetime.now(timezone.utc)
    active_email = active_email or _active_account_email()

    # Load only the active account's real quota baseline / reset anchors.
    real_override = load_real_quota_profile(active_email)

    # Resolve prior capacities first, then let the multi-observation estimator improve them.
    fallback_g5_lim = (limits_config or {}).get('gemini_5h_tokens') or real_override.get('gemini_5h_tokens_calculated') or 500000
    fallback_e5_lim = (limits_config or {}).get('external_5h_tokens') or real_override.get('external_5h_tokens_calculated') or 60000
    fallback_gw_lim = (limits_config or {}).get('gemini_weekly_tokens') or real_override.get('gemini_weekly_tokens_calculated') or 2500000
    fallback_ew_lim = (limits_config or {}).get('external_weekly_tokens') or real_override.get('external_weekly_tokens_calculated') or 250000

    observation_store = load_quota_observations()
    account_history = observation_store.get('accounts', {}).get(active_email or '__unknown__', {})
    worker_events = rolling_data.get('gem_worker_events') or []

    def worker_enriched_history(bucket):
        window = QUOTA_BUCKET_WINDOWS[bucket]
        enriched = []
        for raw in account_history.get(bucket, []) or []:
            item = dict(raw) if isinstance(raw, dict) else {}
            if item and item.get('worker_used_tokens') is None:
                item['worker_used_tokens'] = _worker_tokens_at(worker_events, item.get('timestamp'), window)
                item['worker_estimator_id'] = GEMINI_WORKER_ESTIMATOR_ID
            enriched.append(item)
        return enriched

    g5_history = worker_enriched_history('gemini_5h')
    gw_history = worker_enriched_history('gemini_weekly')
    g5_cal = estimate_quota_capacity(
        g5_history, fallback_g5_lim, QUOTA_BUCKET_WINDOWS['gemini_5h'],
        real_override.get('gemini_worker_5h_capacity_tokens', 0))
    e5_cal = estimate_quota_capacity(account_history.get('external_5h', []), fallback_e5_lim, QUOTA_BUCKET_WINDOWS['external_5h'])
    gw_cal = estimate_quota_capacity(
        gw_history, fallback_gw_lim, QUOTA_BUCKET_WINDOWS['gemini_weekly'],
        real_override.get('gemini_worker_weekly_capacity_tokens', 0))
    ew_cal = estimate_quota_capacity(account_history.get('external_weekly', []), fallback_ew_lim, QUOTA_BUCKET_WINDOWS['external_weekly'])

    g5_lim = g5_cal['capacity_tokens']
    e5_lim = e5_cal['capacity_tokens']
    gw_lim = gw_cal['capacity_tokens']
    ew_lim = ew_cal['capacity_tokens']
    g5_worker_cal = estimate_worker_quota_capacity(
        g5_history, g5_lim, real_override.get('gemini_worker_5h_capacity_tokens', 0),
        QUOTA_BUCKET_WINDOWS['gemini_5h'])
    gw_worker_cal = estimate_worker_quota_capacity(
        gw_history, gw_lim, real_override.get('gemini_worker_weekly_capacity_tokens', 0),
        QUOTA_BUCKET_WINDOWS['gemini_weekly'])

    gem_5h_used = rolling_data.get('gem_5h_used', 0)
    gem_5h_reqs = rolling_data.get('gem_5h_reqs', 0)
    ext_5h_used = rolling_data.get('ext_5h_used', 0)
    ext_5h_reqs = rolling_data.get('ext_5h_reqs', 0)
    oldest_gem_5h_ts = rolling_data.get('oldest_gem_5h_ts')
    newest_gem_5h_ts = rolling_data.get('newest_gem_5h_ts')
    oldest_ext_5h_ts = rolling_data.get('oldest_ext_5h_ts')
    newest_ext_5h_ts = rolling_data.get('newest_ext_5h_ts')

    gem_wk_used = rolling_data.get('gem_wk_used', 0)
    gem_wk_reqs = rolling_data.get('gem_wk_reqs', 0)
    ext_wk_used = rolling_data.get('ext_wk_used', 0)
    ext_wk_reqs = rolling_data.get('ext_wk_reqs', 0)
    oldest_gem_wk_ts = rolling_data.get('oldest_gem_wk_ts')
    newest_gem_wk_ts = rolling_data.get('newest_gem_wk_ts')
    oldest_ext_wk_ts = rolling_data.get('oldest_ext_wk_ts')
    newest_ext_wk_ts = rolling_data.get('newest_ext_wk_ts')
    gem_worker_5h_used = _safe_nonnegative_int(rolling_data.get('gem_worker_5h_used'))
    gem_worker_wk_used = _safe_nonnegative_int(rolling_data.get('gem_worker_wk_used'))
    gem_worker_5h_runs = _safe_nonnegative_int(rolling_data.get('gem_worker_5h_runs'))
    gem_worker_wk_runs = _safe_nonnegative_int(rolling_data.get('gem_worker_wk_runs'))

    # 1. 5-Hour Resets Calculation (Full Reset vs Next Batch Recovery)
    gem_5h_reset_min = 0
    gem_5h_reset_sec = 0
    gem_5h_reset_at = None
    gem_5h_next_batch_at = None

    custom_gem_reset = real_override.get('gemini_5h_reset_at')
    if custom_gem_reset:
        try:
            c_dt = datetime.fromisoformat(custom_gem_reset.replace('Z', '+00:00'))
            if c_dt > now_utc:
                gem_5h_reset_at = c_dt.isoformat()
                diff_sec = max(0, int((c_dt - now_utc).total_seconds()))
                gem_5h_reset_sec = diff_sec
                gem_5h_reset_min = max(1, round(diff_sec / 60))
        except Exception:
            pass

    if gem_5h_used > 0:
        if oldest_gem_5h_ts:
            batch_dt = oldest_gem_5h_ts + timedelta(hours=5)
            if batch_dt > now_utc:
                gem_5h_next_batch_at = batch_dt.isoformat()
        if not gem_5h_reset_at and newest_gem_5h_ts:
            # Full 100% reset occurs when the NEWEST prompt in the window passes 5 hours
            target_dt = newest_gem_5h_ts + timedelta(hours=5)
            if target_dt > now_utc:
                gem_5h_reset_at = target_dt.isoformat()
                diff_sec = max(0, int((target_dt - now_utc).total_seconds()))
                gem_5h_reset_sec = diff_sec
                gem_5h_reset_min = max(1, round(diff_sec / 60))

    ext_5h_reset_min = 0
    ext_5h_reset_sec = 0
    ext_5h_reset_at = None
    ext_5h_next_batch_at = None

    custom_ext_reset = real_override.get('external_5h_reset_at')
    if custom_ext_reset:
        try:
            c_dt = datetime.fromisoformat(custom_ext_reset.replace('Z', '+00:00'))
            if c_dt > now_utc:
                ext_5h_reset_at = c_dt.isoformat()
                diff_sec = max(0, int((c_dt - now_utc).total_seconds()))
                ext_5h_reset_sec = diff_sec
                ext_5h_reset_min = max(1, round(diff_sec / 60))
        except Exception:
            pass

    if ext_5h_used > 0:
        if oldest_ext_5h_ts:
            batch_dt = oldest_ext_5h_ts + timedelta(hours=5)
            if batch_dt > now_utc:
                ext_5h_next_batch_at = batch_dt.isoformat()
        if not ext_5h_reset_at and newest_ext_5h_ts:
            target_dt = newest_ext_5h_ts + timedelta(hours=5)
            if target_dt > now_utc:
                ext_5h_reset_at = target_dt.isoformat()
                diff_sec = max(0, int((target_dt - now_utc).total_seconds()))
                ext_5h_reset_sec = diff_sec
                ext_5h_reset_min = max(1, round(diff_sec / 60))

    # 2. Weekly (7-Day) Resets Calculation
    gem_wk_reset_at = None
    gem_wk_reset_sec = 0
    gem_wk_reset_hours = 0
    custom_gem_wk_reset = real_override.get('gemini_weekly_reset_at')
    if custom_gem_wk_reset:
        try:
            c_dt = datetime.fromisoformat(custom_gem_wk_reset.replace('Z', '+00:00'))
            if c_dt > now_utc:
                gem_wk_reset_at = c_dt.isoformat()
                diff_sec = max(0, int((c_dt - now_utc).total_seconds()))
                gem_wk_reset_sec = diff_sec
                gem_wk_reset_hours = round(diff_sec / 3600, 1)
        except Exception:
            pass

    if not gem_wk_reset_at and gem_wk_used > 0 and oldest_gem_wk_ts:
        target_dt = oldest_gem_wk_ts + timedelta(days=7)
        if target_dt > now_utc:
            gem_wk_reset_at = target_dt.isoformat()
            diff_sec = max(0, int((target_dt - now_utc).total_seconds()))
            gem_wk_reset_sec = diff_sec
            gem_wk_reset_hours = round(diff_sec / 3600, 1)

    ext_wk_reset_at = None
    ext_wk_reset_sec = 0
    ext_wk_reset_hours = 0
    custom_ext_wk_reset = real_override.get('external_weekly_reset_at')
    if custom_ext_wk_reset:
        try:
            c_dt = datetime.fromisoformat(custom_ext_wk_reset.replace('Z', '+00:00'))
            if c_dt > now_utc:
                ext_wk_reset_at = c_dt.isoformat()
                diff_sec = max(0, int((c_dt - now_utc).total_seconds()))
                ext_wk_reset_sec = diff_sec
                ext_wk_reset_hours = round(diff_sec / 3600, 1)
        except Exception:
            pass

    if not ext_wk_reset_at and ext_wk_used > 0 and oldest_ext_wk_ts:
        target_dt = oldest_ext_wk_ts + timedelta(days=7)
        if target_dt > now_utc:
            ext_wk_reset_at = target_dt.isoformat()
            diff_sec = max(0, int((target_dt - now_utc).total_seconds()))
            ext_wk_reset_sec = diff_sec
            ext_wk_reset_hours = round(diff_sec / 3600, 1)

    # 1. Predict each quota from the latest trusted manual % anchor plus the
    # net movement of rolling tracked tokens. This also preserves usage that
    # happened before this tracker started observing the account.
    gw_pred = predict_quota_from_anchor(
        gem_wk_used, gw_lim, gw_cal,
        real_override.get('gemini_weekly_pct'), real_override.get('cur_gw_used'),
        gem_worker_wk_used, gw_worker_cal, real_override.get('cur_gw_worker_used')
    )
    ew_pred = predict_quota_from_anchor(
        ext_wk_used, ew_lim, ew_cal,
        real_override.get('external_weekly_pct'), real_override.get('cur_ew_used')
    )
    g5_pred = predict_quota_from_anchor(
        gem_5h_used, g5_lim, g5_cal,
        real_override.get('gemini_5h_pct'), real_override.get('cur_g5_used'),
        gem_worker_5h_used, g5_worker_cal, real_override.get('cur_g5_worker_used')
    )
    e5_pred = predict_quota_from_anchor(
        ext_5h_used, e5_lim, e5_cal,
        real_override.get('external_5h_pct'), real_override.get('cur_e5_used')
    )

    gw_rem = gw_pred['remaining_tokens']
    gw_pct = gw_pred['percentage_remaining']
    ew_rem = ew_pred['remaining_tokens']
    ew_pct = ew_pred['percentage_remaining']
    g5_rem = g5_pred['remaining_tokens']
    g5_pct = g5_pred['percentage_remaining']
    e5_rem = e5_pred['remaining_tokens']
    e5_pct = e5_pred['percentage_remaining']

    # HIERARCHICAL CAP ENFORCEMENT:
    # If weekly quota is completely depleted (0.0%), the 5-hour effective quota cannot be used (locked to 0.0%)
    ext_weekly_capped = (ew_pct <= 0.0 or ew_rem <= 0)
    if ext_weekly_capped:
        e5_rem = 0
        e5_pct = 0.0

    gem_weekly_capped = (gw_pct <= 0.0 or gw_rem <= 0)
    if gem_weekly_capped:
        g5_rem = 0
        g5_pct = 0.0

    # Dynamic percentage values (never locked statically):
    g5_real_pct = g5_pct
    e5_real_pct = e5_pct
    gw_real_pct = gw_pct
    ew_real_pct = ew_pct

    # Status determination:
    g5_status_str = 'Locked (Weekly Capped)' if gem_weekly_capped else ('Optimal' if g5_pct >= 99 else ('Normal' if g5_pct > 25 else ('Depleted' if g5_pct <= 0 else 'Near Limit')))
    e5_status_str = 'Locked (Weekly Capped)' if ext_weekly_capped else ('Optimal' if e5_pct >= 99 else ('Normal' if e5_pct > 25 else ('Depleted' if e5_pct <= 0 else 'Near Limit')))
    gw_status_str = 'Depleted' if gw_pct <= 0.0 else ('Optimal' if gw_pct >= 99 else ('Normal' if gw_pct > 25 else 'Near Limit'))
    ew_status_str = 'Depleted' if ew_pct <= 0.0 else ('Optimal' if ew_pct >= 99 else ('Normal' if ew_pct > 25 else 'Near Limit'))

    return {
        'five_hour_window': {
            'resets_in_minutes': max(gem_5h_reset_min, ext_5h_reset_min),
            'resets_in_seconds': max(gem_5h_reset_sec, ext_5h_reset_sec),
            'gemini': {
                'limit_tokens': g5_lim,
                'used_tokens': g5_pred['estimated_used_tokens'],
                'estimated_used_tokens': g5_pred['estimated_used_tokens'],
                'tracked_used_tokens': g5_pred['tracked_used_tokens'],
                'inferred_untracked_tokens': g5_pred['inferred_untracked_tokens'],
                'remaining_tokens': g5_rem,
                'percentage_remaining': g5_pct,
                'percentage_real': g5_real_pct,
                'prediction_source': g5_pred['prediction_source'],
                'calibration': g5_cal,
                'worker_used_tokens': g5_pred['worker_used_tokens'],
                'worker_runs_count': gem_worker_5h_runs,
                'worker_prediction_applied': g5_pred['worker_prediction_applied'],
                'worker_calibration': g5_pred['worker_calibration'],
                'requests_count': gem_5h_reqs,
                'resets_in_minutes': gem_5h_reset_min,
                'resets_in_seconds': gem_5h_reset_sec,
                'resets_at': gem_5h_reset_at,
                'next_batch_resets_at': gem_5h_next_batch_at,
                'first_request_at': oldest_gem_5h_ts.isoformat() if oldest_gem_5h_ts else None,
                'status': g5_status_str,
                'is_weekly_capped': gem_weekly_capped,
                'data_source': 'IDE/CLI transcripts + exact Gemini worker reports'
            },
            'external': {
                'limit_tokens': e5_lim,
                'used_tokens': e5_pred['estimated_used_tokens'],
                'estimated_used_tokens': e5_pred['estimated_used_tokens'],
                'tracked_used_tokens': e5_pred['tracked_used_tokens'],
                'inferred_untracked_tokens': e5_pred['inferred_untracked_tokens'],
                'remaining_tokens': e5_rem,
                'percentage_remaining': e5_pct,
                'percentage_real': e5_real_pct,
                'prediction_source': e5_pred['prediction_source'],
                'calibration': e5_cal,
                'requests_count': ext_5h_reqs,
                'resets_in_minutes': ext_5h_reset_min,
                'resets_in_seconds': ext_5h_reset_sec,
                'resets_at': ext_5h_reset_at,
                'next_batch_resets_at': ext_5h_next_batch_at,
                'first_request_at': oldest_ext_5h_ts.isoformat() if oldest_ext_5h_ts else None,
                'status': e5_status_str,
                'is_weekly_capped': ext_weekly_capped,
                'data_source': 'IDE Settings & Empirical Sliding Window'
            }
        },
        'weekly_window': {
            'gemini': {
                'limit_tokens': gw_lim,
                'used_tokens': gw_pred['estimated_used_tokens'],
                'estimated_used_tokens': gw_pred['estimated_used_tokens'],
                'tracked_used_tokens': gw_pred['tracked_used_tokens'],
                'inferred_untracked_tokens': gw_pred['inferred_untracked_tokens'],
                'remaining_tokens': gw_rem,
                'percentage_remaining': gw_pct,
                'percentage_real': gw_real_pct,
                'prediction_source': gw_pred['prediction_source'],
                'calibration': gw_cal,
                'worker_used_tokens': gw_pred['worker_used_tokens'],
                'worker_runs_count': gem_worker_wk_runs,
                'worker_prediction_applied': gw_pred['worker_prediction_applied'],
                'worker_calibration': gw_pred['worker_calibration'],
                'requests_count': gem_wk_reqs,
                'resets_at': gem_wk_reset_at,
                'resets_in_hours': gem_wk_reset_hours,
                'resets_in_seconds': gem_wk_reset_sec,
                'first_request_at': oldest_gem_wk_ts.isoformat() if oldest_gem_wk_ts else None,
                'status': gw_status_str,
                'data_source': 'IDE/CLI transcripts + exact Gemini worker reports'
            },
            'external': {
                'limit_tokens': ew_lim,
                'used_tokens': ew_pred['estimated_used_tokens'],
                'estimated_used_tokens': ew_pred['estimated_used_tokens'],
                'tracked_used_tokens': ew_pred['tracked_used_tokens'],
                'inferred_untracked_tokens': ew_pred['inferred_untracked_tokens'],
                'remaining_tokens': ew_rem,
                'percentage_remaining': ew_pct,
                'percentage_real': ew_real_pct,
                'prediction_source': ew_pred['prediction_source'],
                'calibration': ew_cal,
                'requests_count': ext_wk_reqs,
                'resets_at': ext_wk_reset_at,
                'resets_in_hours': ext_wk_reset_hours,
                'resets_in_seconds': ext_wk_reset_sec,
                'first_request_at': oldest_ext_wk_ts.isoformat() if oldest_ext_wk_ts else None,
                'status': ew_status_str,
                'data_source': 'IDE Settings & Empirical Sliding Window'
            }
        },
        'has_real_data': bool(real_override),
        'real_updated_at': real_override.get('updated_at')
    }

# ---- Codex Usage Data Engine ----
def load_codex_usage():
    """Load manually-entered Codex usage data"""
    default = {'models': {}, 'weekly_limit_tokens': 0, 'platform': 'Codex', 'updated_at': None}
    data = _read_json_file(CODEX_USAGE_FILE, default)
    return data if isinstance(data, dict) else dict(default)

def save_codex_usage(data):
    """Save Codex usage data"""
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(CODEX_USAGE_FILE, data)


def _nonnegative_int(value, field_name):
    value = int(value or 0)
    if value < 0:
        raise ValueError(f'{field_name} must be >= 0')
    return value


def _nonnegative_float(value, field_name):
    value = float(value or 0.0)
    if value < 0:
        raise ValueError(f'{field_name} must be >= 0')
    return value


def apply_codex_usage_update(codex_data, body):
    """Apply one Codex usage API mutation with explicit all-time/weekly semantics."""
    codex_data = dict(codex_data or {})
    codex_data.setdefault('models', {})
    action = body.get('action', 'upsert_model')

    if action == 'upsert_model':
        model_name = str(body.get('model_name', '')).strip()
        if not model_name:
            raise ValueError('model_name is required')

        mode = body.get('mode', 'add')
        if mode not in ('add', 'overwrite'):
            raise ValueError("mode must be 'add' or 'overwrite'")

        existing = codex_data['models'].get(model_name, {})
        in_toks = _nonnegative_int(body.get('input_tokens', 0), 'input_tokens')
        out_toks = _nonnegative_int(body.get('output_tokens', 0), 'output_tokens')
        think_toks = _nonnegative_int(body.get('thinking_tokens', 0), 'thinking_tokens')
        tot_toks = _nonnegative_int(body.get('total_tokens', 0), 'total_tokens')
        if tot_toks == 0 and (in_toks > 0 or out_toks > 0):
            tot_toks = in_toks + out_toks

        weekly_supplied = 'weekly_tokens' in body and body.get('weekly_tokens') not in (None, '')
        weekly_toks = (
            _nonnegative_int(body.get('weekly_tokens'), 'weekly_tokens')
            if weekly_supplied else tot_toks
        )

        cost_val = _nonnegative_float(body.get('cost_usd', 0.0), 'cost_usd')
        explicit_cost = cost_val > 0.0
        if cost_val == 0.0 and (in_toks > 0 or out_toks > 0):
            estimated_cost = estimate_model_cost(model_name, in_toks, out_toks)
            if estimated_cost is not None:
                cost_val = round(estimated_cost, 4)
        new_cost_known = bool(
            tot_toks == 0 or explicit_cost or model_has_cost_estimate(model_name)
        )

        weekly_cost_supplied = 'weekly_cost_usd' in body and body.get('weekly_cost_usd') not in (None, '')
        if weekly_cost_supplied:
            weekly_cost = _nonnegative_float(body.get('weekly_cost_usd'), 'weekly_cost_usd')
            new_weekly_cost_known = True
        elif weekly_toks == 0:
            weekly_cost = 0.0
            new_weekly_cost_known = True
        elif weekly_toks == tot_toks:
            weekly_cost = cost_val
            new_weekly_cost_known = new_cost_known
        else:
            # We cannot recover a 7-day cost from an all-time cost without the
            # weekly input/output split. Keep it explicitly unknown instead of
            # fabricating a proportional dollar amount.
            weekly_cost = 0.0
            new_weekly_cost_known = False

        sess_val = _nonnegative_int(body.get('sessions', 0), 'sessions')
        resp_val = _nonnegative_int(body.get('responses', 0), 'responses')
        tool_val = _nonnegative_int(body.get('tool_calls', 0), 'tool_calls')

        existing_total = _nonnegative_int(existing.get('total_tokens', 0), 'existing.total_tokens')
        existing_weekly = _nonnegative_int(existing.get('weekly_tokens', existing_total), 'existing.weekly_tokens')
        existing_cost = _nonnegative_float(existing.get('cost_usd', 0.0), 'existing.cost_usd')
        existing_weekly_cost = _nonnegative_float(existing.get('weekly_cost_usd', existing_cost), 'existing.weekly_cost_usd')
        existing_cost_known = existing.get('cost_known')
        if existing_cost_known is None:
            existing_cost_known = bool(
                existing_total == 0 or existing_cost > 0 or model_has_cost_estimate(model_name)
            )
        existing_weekly_cost_known = existing.get('weekly_cost_known')
        if existing_weekly_cost_known is None:
            existing_weekly_cost_known = bool(
                existing_weekly == 0 or existing_weekly_cost > 0 or model_has_cost_estimate(model_name)
            )

        if mode == 'add' and existing:
            final_in = _nonnegative_int(existing.get('input_tokens', 0), 'existing.input_tokens') + in_toks
            final_out = _nonnegative_int(existing.get('output_tokens', 0), 'existing.output_tokens') + out_toks
            final_think = _nonnegative_int(existing.get('thinking_tokens', 0), 'existing.thinking_tokens') + think_toks
            final_tot = existing_total + tot_toks
            final_weekly = existing_weekly + weekly_toks
            final_cost = round(existing_cost + cost_val, 4)
            final_weekly_cost = round(existing_weekly_cost + weekly_cost, 4)
            final_cost_known = bool(existing_cost_known and new_cost_known)
            final_weekly_cost_known = bool(existing_weekly_cost_known and new_weekly_cost_known)
            final_sess = _nonnegative_int(existing.get('sessions', 0), 'existing.sessions') + sess_val
            final_resp = _nonnegative_int(existing.get('responses', 0), 'existing.responses') + resp_val
            final_tools = _nonnegative_int(existing.get('tool_calls', 0), 'existing.tool_calls') + tool_val
        else:
            # Overwrite means overwrite: zero is a valid value and must not fall
            # back to the old record.
            final_in = in_toks
            final_out = out_toks
            final_think = think_toks
            final_tot = tot_toks
            final_weekly = weekly_toks
            final_cost = round(cost_val, 4)
            final_weekly_cost = round(weekly_cost, 4)
            final_cost_known = new_cost_known
            final_weekly_cost_known = new_weekly_cost_known
            final_sess = sess_val
            final_resp = resp_val
            final_tools = tool_val

        codex_data['models'][model_name] = {
            'total_tokens': final_tot,
            'weekly_tokens': final_weekly,
            'input_tokens': final_in,
            'output_tokens': final_out,
            'thinking_tokens': final_think,
            'cost_usd': final_cost,
            'weekly_cost_usd': final_weekly_cost,
            'cost_known': final_cost_known,
            'weekly_cost_known': final_weekly_cost_known,
            'sessions': final_sess,
            'responses': final_resp,
            'tool_calls': final_tools,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }
        if 'weekly_limit_tokens' in body:
            weekly_limit = _nonnegative_int(body.get('weekly_limit_tokens'), 'weekly_limit_tokens')
            if weekly_limit > 0:
                codex_data['weekly_limit_tokens'] = weekly_limit

    elif action == 'delete_model':
        model_name = str(body.get('model_name', '')).strip()
        if model_name:
            codex_data['models'].pop(model_name, None)

    elif action == 'set_weekly_limit':
        codex_data['weekly_limit_tokens'] = _nonnegative_int(
            body.get('weekly_limit_tokens', 0), 'weekly_limit_tokens'
        )
    else:
        raise ValueError(f'Unsupported Codex usage action: {action}')

    return codex_data


def _sanitize_credits(raw_credits):
    if not isinstance(raw_credits, dict):
        return None
    sanitized = {}
    if 'has_credits' in raw_credits and isinstance(raw_credits['has_credits'], bool):
        sanitized['has_credits'] = raw_credits['has_credits']
    if 'unlimited' in raw_credits and isinstance(raw_credits['unlimited'], bool):
        sanitized['unlimited'] = raw_credits['unlimited']
    for num_field in ('remaining', 'used', 'limit', 'balance'):
        val = raw_credits.get(num_field)
        if val is not None and not isinstance(val, bool):
            try:
                f_val = float(val)
                if math.isfinite(f_val):
                    sanitized[num_field] = int(val) if isinstance(val, int) or (isinstance(val, str) and val.isdigit()) else (int(f_val) if f_val.is_integer() else f_val)
            except Exception:
                pass
    if 'currency' in raw_credits and isinstance(raw_credits['currency'], str) and len(raw_credits['currency']) <= 8:
        sanitized['currency'] = raw_credits['currency']
    return sanitized if sanitized else None


def normalize_codex_rate_limits(rate_limits, event_timestamp=None, now_utc=None):
    """Normalize raw Codex rate_limits payload across schema variations by duration."""
    if not isinstance(rate_limits, dict):
        return None

    now_utc = now_utc or datetime.now(timezone.utc)

    plan_type = rate_limits.get('plan_type') if isinstance(rate_limits.get('plan_type'), str) else None
    credits = _sanitize_credits(rate_limits.get('credits'))

    raw_windows = []
    if isinstance(rate_limits.get('windows'), list):
        raw_windows.extend([w for w in rate_limits['windows'] if isinstance(w, dict)])

    for key in ('primary', 'secondary'):
        w = rate_limits.get(key)
        if isinstance(w, dict):
            raw_windows.append(w)

    for k, v in rate_limits.items():
        if k not in ('primary', 'secondary', 'windows', 'plan_type', 'credits') and isinstance(v, dict):
            if 'window_minutes' in v or 'used_percent' in v or 'remaining_percent' in v:
                raw_windows.append(v)

    if not raw_windows and ('window_minutes' in rate_limits or 'used_percent' in rate_limits or 'remaining_percent' in rate_limits):
        raw_windows.append(rate_limits)

    normalized_windows = []
    seen_durations = set()

    for w in raw_windows:
        win_mins = w.get('window_minutes')
        if win_mins is None and 'window_seconds' in w:
            try:
                sec_val = float(w['window_seconds'])
                if math.isfinite(sec_val):
                    win_mins = int(sec_val) // 60
            except Exception:
                pass
        if win_mins is None:
            continue
        try:
            f_mins = float(win_mins)
            if not math.isfinite(f_mins):
                continue
            win_mins = int(f_mins)
        except Exception:
            continue
        if win_mins <= 0:
            continue

        used_pct = None
        rem_pct = None
        if 'used_percent' in w and w['used_percent'] is not None and not isinstance(w['used_percent'], bool):
            try:
                val = float(w['used_percent'])
                if math.isfinite(val):
                    used_pct = val
            except Exception:
                pass
        if 'remaining_percent' in w and w['remaining_percent'] is not None and not isinstance(w['remaining_percent'], bool):
            try:
                val = float(w['remaining_percent'])
                if math.isfinite(val):
                    rem_pct = val
            except Exception:
                pass

        if used_pct is None and rem_pct is not None:
            used_pct = 100.0 - rem_pct
        elif rem_pct is None and used_pct is not None:
            rem_pct = 100.0 - used_pct
        elif used_pct is None and rem_pct is None:
            used_toks = w.get('used_tokens')
            lim_toks = w.get('limit_tokens')
            if used_toks is not None and lim_toks is not None and not isinstance(used_toks, bool) and not isinstance(lim_toks, bool):
                try:
                    f_used = float(used_toks)
                    f_lim = float(lim_toks)
                    if math.isfinite(f_used) and math.isfinite(f_lim) and f_lim > 0:
                        calc_pct = (f_used / f_lim) * 100.0
                        if math.isfinite(calc_pct):
                            used_pct = calc_pct
                            rem_pct = 100.0 - used_pct
                except Exception:
                    pass

        if used_pct is None or not math.isfinite(used_pct) or rem_pct is None or not math.isfinite(rem_pct):
            continue

        used_pct = max(0.0, min(100.0, round(used_pct, 2)))
        rem_pct = max(0.0, min(100.0, round(rem_pct, 2)))

        raw_reset = w.get('resets_at') or w.get('reset_at') or w.get('reset_time') or w.get('resets_in_seconds')
        resets_at_iso = None
        resets_in_sec = None

        if isinstance(raw_reset, (int, float)) and not isinstance(raw_reset, bool):
            try:
                f_reset = float(raw_reset)
                if math.isfinite(f_reset):
                    if f_reset > 1_000_000_000:
                        sec_val = (f_reset / 1000.0) if f_reset > 1e11 else f_reset
                        dt_reset = datetime.fromtimestamp(sec_val, tz=timezone.utc)
                        resets_at_iso = dt_reset.isoformat()
                    else:
                        dt_reset = now_utc + timedelta(seconds=f_reset)
                        resets_at_iso = dt_reset.isoformat()
            except Exception:
                pass
        elif isinstance(raw_reset, str) and raw_reset.strip():
            try:
                dt_reset = _parse_iso_utc(raw_reset)
                if dt_reset:
                    resets_at_iso = dt_reset.isoformat()
                else:
                    f_reset = float(raw_reset)
                    if math.isfinite(f_reset):
                        if f_reset > 1_000_000_000:
                            sec_val = (f_reset / 1000.0) if f_reset > 1e11 else f_reset
                            dt_reset = datetime.fromtimestamp(sec_val, tz=timezone.utc)
                            resets_at_iso = dt_reset.isoformat()
                        else:
                            dt_reset = now_utc + timedelta(seconds=f_reset)
                            resets_at_iso = dt_reset.isoformat()
            except Exception:
                pass

        if resets_at_iso:
            try:
                dt_r = _parse_iso_utc(resets_at_iso)
                if dt_r:
                    resets_in_sec = max(0, int((dt_r - now_utc).total_seconds()))
            except Exception:
                pass
        elif 'resets_in_seconds' in w and w['resets_in_seconds'] is not None and not isinstance(w['resets_in_seconds'], bool):
            try:
                f_sec = float(w['resets_in_seconds'])
                if math.isfinite(f_sec) and f_sec >= 0:
                    resets_in_sec = int(f_sec)
                    resets_at_iso = (now_utc + timedelta(seconds=resets_in_sec)).isoformat()
            except Exception:
                pass

        if rem_pct <= 0.0:
            status = 'Depleted'
        elif rem_pct < 20.0:
            status = 'Near Limit'
        elif rem_pct >= 99.0:
            status = 'Optimal'
        else:
            status = 'Normal'

        if win_mins == 300:
            label = 'Khung 5 Giờ (5-Hour)'
        elif win_mins == 10080:
            label = 'Khung Tuần (7-Day Weekly)'
        elif win_mins == 1440:
            label = 'Khung Ngày (24-Hour)'
        elif win_mins % 60 == 0:
            label = f'Khung {win_mins // 60} Giờ'
        else:
            label = f'Khung {win_mins} Phút'

        used_tokens_val = None
        if 'used_tokens' in w and w['used_tokens'] is not None and not isinstance(w['used_tokens'], bool):
            try:
                f_tok = float(w['used_tokens'])
                if math.isfinite(f_tok):
                    used_tokens_val = int(w['used_tokens']) if isinstance(w['used_tokens'], int) else (int(f_tok) if f_tok.is_integer() else f_tok)
            except Exception:
                pass

        limit_tokens_val = None
        if 'limit_tokens' in w and w['limit_tokens'] is not None and not isinstance(w['limit_tokens'], bool):
            try:
                f_tok = float(w['limit_tokens'])
                if math.isfinite(f_tok):
                    limit_tokens_val = int(w['limit_tokens']) if isinstance(w['limit_tokens'], int) else (int(f_tok) if f_tok.is_integer() else f_tok)
            except Exception:
                pass

        if win_mins in seen_durations:
            continue
        seen_durations.add(win_mins)
        normalized_windows.append({
            'window_minutes': win_mins,
            'label': label,
            'used_percent': used_pct,
            'remaining_percent': rem_pct,
            'resets_at': resets_at_iso,
            'resets_in_seconds': resets_in_sec,
            'used_tokens': used_tokens_val,
            'limit_tokens': limit_tokens_val,
            'status': status
        })

    normalized_windows.sort(key=lambda x: x['window_minutes'])

    obs_ts_iso = None
    if event_timestamp:
        if isinstance(event_timestamp, str):
            dt_obs = _parse_iso_utc(event_timestamp)
            obs_ts_iso = dt_obs.isoformat() if dt_obs else event_timestamp
        elif isinstance(event_timestamp, datetime):
            obs_ts_iso = event_timestamp.astimezone(timezone.utc).isoformat()

    return {
        'available': bool(normalized_windows),
        'source': 'local_session_logs',
        'source_label': 'Local Codex Session Logs',
        'observed_at': obs_ts_iso or now_utc.isoformat(),
        'plan_type': plan_type,
        'credits': credits,
        'windows': normalized_windows
    }


_CODEX_APP_SERVER_RATE_CACHE = {'expires_at': 0.0, 'value': None}


def find_codex_executable():
    """Resolve Codex even when a desktop-launched tracker has no Codex on PATH."""
    import shutil
    from pathlib import Path

    executable = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
    if executable:
        return executable
    if os.name == 'nt':
        local_app_data = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        install_dir = Path(local_app_data) / 'OpenAI' / 'Codex' / 'bin'
        candidates = []
        for candidate in install_dir.glob('*/codex.exe'):
            try:
                if candidate.is_file():
                    candidates.append((candidate.stat().st_mtime_ns, str(candidate)))
            except OSError:
                continue
        if candidates:
            return max(candidates)[1]
        # Keep support for standalone CLI installations (including npm shims).
        return shutil.which('codex')
    return None


def normalize_codex_app_server_rate_limits(response, now_utc=None):
    """Normalize Codex app-server account/rateLimits/read into the tracker schema."""
    if not isinstance(response, dict):
        return None

    now_utc = now_utc or datetime.now(timezone.utc)
    snapshots = response.get('rateLimitsByLimitId')
    snapshot = snapshots.get('codex') if isinstance(snapshots, dict) else None

    def _has_windows(value):
        return isinstance(value, dict) and (
            isinstance(value.get('primary'), dict) or isinstance(value.get('secondary'), dict)
        )

    if not _has_windows(snapshot):
        legacy_snapshot = response.get('rateLimits')
        if _has_windows(legacy_snapshot):
            snapshot = legacy_snapshot
        elif isinstance(snapshots, dict):
            snapshot = next((v for v in snapshots.values() if _has_windows(v)), snapshot)

    if not isinstance(snapshot, dict):
        return None

    def _window(raw):
        if not isinstance(raw, dict):
            return None
        return {
            'window_minutes': raw.get('windowDurationMins'),
            'used_percent': raw.get('usedPercent'),
            'resets_at': raw.get('resetsAt'),
        }

    raw_rate_limits = {
        'plan_type': snapshot.get('planType'),
        'credits': snapshot.get('credits'),
        'primary': _window(snapshot.get('primary')),
        'secondary': _window(snapshot.get('secondary')),
    }
    normalized = normalize_codex_rate_limits(
        raw_rate_limits,
        event_timestamp=now_utc,
        now_utc=now_utc,
    )
    if not normalized or not normalized.get('available'):
        return None

    normalized['source'] = 'codex_app_server'
    normalized['source_label'] = 'Codex App Server (Live)'
    normalized['account_id'] = response.get('accountId')
    normalized['limit_id'] = snapshot.get('limitId')
    normalized['limit_name'] = snapshot.get('limitName')
    return normalized


def read_codex_rate_limits_app_server(timeout_seconds=5.0, cache_seconds=10.0):
    """Read live Codex quota through the local app-server RPC without touching auth files."""
    import queue
    import subprocess
    import threading
    import time

    now_mono = time.monotonic()
    cached = _CODEX_APP_SERVER_RATE_CACHE.get('value')
    if cached is not None and now_mono < _CODEX_APP_SERVER_RATE_CACHE.get('expires_at', 0.0):
        return cached

    codex_exe = find_codex_executable()
    if not codex_exe:
        return None

    proc = None
    reader_thread = None
    output_queue = queue.Queue()
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        proc = subprocess.Popen(
            [codex_exe, 'app-server', '--stdio'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=creationflags,
        )

        def _reader():
            try:
                for line in proc.stdout:
                    output_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                output_queue.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        def _send(payload):
            proc.stdin.write(json.dumps(payload, separators=(',', ':')) + '\n')
            proc.stdin.flush()

        _send({
            'id': 1,
            'method': 'initialize',
            'params': {'clientInfo': {'name': 'usage-tracker', 'version': '1.0'}},
        })

        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        sent_read = False
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = output_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            try:
                message = json.loads(line)
            except Exception:
                continue
            if not isinstance(message, dict):
                continue
            if message.get('id') == 1 and not sent_read:
                if 'error' in message:
                    break
                _send({'id': 2, 'method': 'account/rateLimits/read', 'params': None})
                sent_read = True
                continue
            if message.get('id') == 2:
                if 'error' in message:
                    break
                normalized = normalize_codex_app_server_rate_limits(message.get('result'))
                if normalized and normalized.get('available'):
                    _CODEX_APP_SERVER_RATE_CACHE['value'] = normalized
                    _CODEX_APP_SERVER_RATE_CACHE['expires_at'] = time.monotonic() + max(0.0, float(cache_seconds))
                    return normalized
                break
    except Exception:
        return None
    finally:
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
    return None


def get_codex_rate_limits():
    """Prefer current app-server quota; keep session logs as backward-compatible fallback."""
    live = read_codex_rate_limits_app_server()
    if live and live.get('available'):
        record_codex_quota_identity(live)
        return live
    fallback = scan_codex_rate_limits()
    if isinstance(fallback, dict):
        fallback['fallback_reason'] = 'Codex app-server rate-limit RPC unavailable'
    return fallback


def _codex_quota_identity_snapshot(rate_limits):
    """Keep only the opaque account ID and quota fields needed for attribution."""
    if (not isinstance(rate_limits, dict) or
            rate_limits.get('source') != 'codex_app_server'):
        return None
    account_id = rate_limits.get('account_id')
    observed_at = _parse_iso_utc(rate_limits.get('observed_at'))
    if not isinstance(account_id, str) or not 1 <= len(account_id) <= 128 or observed_at is None:
        return None
    windows = {}
    for raw in rate_limits.get('windows') or []:
        if not isinstance(raw, dict):
            continue
        try:
            duration = int(raw.get('window_minutes'))
            pct = float(raw.get('used_percent'))
        except (TypeError, ValueError, OverflowError):
            continue
        reset = _parse_iso_utc(raw.get('resets_at'))
        if duration in (300, 10080) and math.isfinite(pct) and 0 <= pct <= 100 and reset:
            windows['five_hour' if duration == 300 else 'weekly'] = {
                'used_percent': round(pct, 2), 'resets_at': reset.isoformat(),
            }
    if 'weekly' not in windows:
        return None
    return {
        'observed_at': observed_at.isoformat(),
        'account_id': account_id,
        'limit_id': str(rate_limits.get('limit_id') or ''),
        'plan_type': str(rate_limits.get('plan_type') or ''),
        'five_hour': windows.get('five_hour'),
        'weekly': windows['weekly'],
        'source': 'codex_app_server',
    }


def load_codex_quota_identity():
    raw = _read_json_file(CODEX_QUOTA_IDENTITY_FILE, {'version': 1, 'snapshots': []})
    return list(raw.get('snapshots') or []) if isinstance(raw, dict) else []


def record_codex_quota_identity(rate_limits):
    """Persist prospective evidence; never assign the current ID to old logs."""
    snapshot = _codex_quota_identity_snapshot(rate_limits)
    if snapshot is None:
        return False
    with PERSISTENCE_LOCK:
        rows = load_codex_quota_identity()
        cutoff = _parse_iso_utc(snapshot['observed_at']) - timedelta(days=367)
        rows = [row for row in rows if isinstance(row, dict) and
                (_parse_iso_utc(row.get('observed_at')) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        last = rows[-1] if rows else None
        if last:
            last_at = _parse_iso_utc(last.get('observed_at'))
            same_state = all(last.get(key) == snapshot.get(key) for key in
                             ('account_id', 'limit_id', 'plan_type', 'five_hour', 'weekly'))
            if last_at and (snapshot['observed_at'] == last['observed_at'] or
                            (same_state and 0 <= (_parse_iso_utc(snapshot['observed_at']) - last_at).total_seconds() < 240)):
                return False
        rows.append(snapshot)
        _atomic_write_json(CODEX_QUOTA_IDENTITY_FILE, {'version': 1, 'snapshots': rows})
    return True


def build_codex_quota_identity_events(snapshots):
    """Classify observable boundaries, not an unobservable provider cause."""
    rows = sorted((row for row in snapshots or [] if isinstance(row, dict) and
                   _parse_iso_utc(row.get('observed_at')) and row.get('account_id')),
                  key=lambda row: row['observed_at'])
    events = []
    last_by_account = {}
    previous = None
    for after in rows:
        when = _parse_iso_utc(after['observed_at'])
        if previous and after['account_id'] != previous['account_id']:
            events.append({
                'observed_at': after['observed_at'], 'kind': 'account_switch_observed',
                'account_id': after['account_id'],
                'previous_account_id': previous['account_id'],
                'weekly_before': previous.get('weekly'), 'weekly_after': after.get('weekly'),
                'cause_confirmed': True,
            })
        before = last_by_account.get(after['account_id'])
        if before:
            prior_when = _parse_iso_utc(before['observed_at'])
            old_week = before.get('weekly') or {}
            new_week = after.get('weekly') or {}
            old_reset = _parse_iso_utc(old_week.get('resets_at'))
            new_reset = _parse_iso_utc(new_week.get('resets_at'))
            if old_reset and new_reset:
                try:
                    used_dropped = float(new_week.get('used_percent')) < float(old_week.get('used_percent')) - 0.5
                except (TypeError, ValueError):
                    used_dropped = False
                reset_moved = abs((new_reset - old_reset).total_seconds()) > 180
                if used_dropped or reset_moved:
                    if (when - prior_when).total_seconds() > 6 * 3600:
                        kind = 'cycle_change_after_gap'
                    elif old_reset <= when <= old_reset + timedelta(hours=2) and new_reset > old_reset:
                        kind = 'scheduled_weekly_reset_observed'
                    elif when < old_reset - timedelta(minutes=5) and used_dropped and new_reset > old_reset:
                        kind = 'early_weekly_reset_unverified'
                    else:
                        kind = 'weekly_state_change_unverified'
                    events.append({
                        'observed_at': after['observed_at'], 'kind': kind,
                        'account_id': after['account_id'], 'previous_account_id': None,
                        'weekly_before': old_week, 'weekly_after': new_week,
                        'cause_confirmed': False,
                    })
        last_by_account[after['account_id']] = after
        previous = after
    return events


def attribute_codex_quota_observations(observations, snapshots, max_gap_seconds=180):
    """Attach an ID only when a nearby live read matches both quota windows."""
    usable = []
    for row in snapshots or []:
        if not isinstance(row, dict) or not row.get('account_id'):
            continue
        ts = _parse_iso_utc(row.get('observed_at'))
        weekly = _parse_iso_utc((row.get('weekly') or {}).get('resets_at'))
        five_hour = _parse_iso_utc((row.get('five_hour') or {}).get('resets_at'))
        try:
            week_pct = float((row.get('weekly') or {}).get('used_percent'))
            five_pct = float((row.get('five_hour') or {}).get('used_percent'))
        except (TypeError, ValueError):
            continue
        if ts and weekly and five_hour and math.isfinite(week_pct) and math.isfinite(five_pct):
            usable.append((ts, weekly, five_hour, row['account_id'], week_pct, five_pct))
    usable.sort(key=lambda item: item[0])
    times = [item[0] for item in usable]
    attributed = []
    for raw in observations or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if row.get('account_id'):
            attributed.append(row)
            continue
        ts = _parse_iso_utc(row.get('ts'))
        weekly = _parse_iso_utc(row.get('weekly_resets_at'))
        five_hour = _parse_iso_utc(row.get('resets_at'))
        if ts and weekly and five_hour:
            nearby = usable[
                bisect.bisect_left(times, ts - timedelta(seconds=max_gap_seconds)):
                bisect.bisect_right(times, ts + timedelta(seconds=max_gap_seconds))
            ]
            matches = set()
            for snap_ts, snap_week, snap_five, account_id, snap_week_pct, snap_five_pct in nearby:
                if (abs((snap_ts - ts).total_seconds()) > max_gap_seconds or
                        abs((snap_week - weekly).total_seconds()) > 180 or
                        abs((snap_five - five_hour).total_seconds()) > 180):
                    continue
                try:
                    if (abs(float(row.get('weekly_used_percent')) - snap_week_pct) > 5 or
                            abs(float(row.get('used_percent')) - snap_five_pct) > 10):
                        continue
                except (TypeError, ValueError):
                    continue
                matches.add(account_id)
            if len(matches) == 1:
                row['account_id'] = matches.pop()
                row['account_attribution'] = 'matched_live_quota'
        attributed.append(row)
    return attributed


def _read_codex_tail_lines(file_path, max_bytes=65536, max_lines=100):
    """Read bounded trailing lines from a session log file without loading the entire file."""
    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
            raw = f.read()
        text = raw.decode('utf-8', errors='ignore')
        lines = text.splitlines()
        if file_size > max_bytes and lines:
            lines = lines[1:]  # discard the first potentially truncated line
        return lines[-max_lines:]
    except Exception:
        return []


def scan_codex_rate_limits(sessions_dir=None, max_files=20, max_tail_bytes=65536,
                           max_tail_lines=100, max_dirs=500, max_entries=5000):
    """Scan local Codex session logs for the newest token_count rate_limits observation using bounded tail reads."""
    target_dir = sessions_dir if sessions_dir is not None else globals().get('CODEX_SESSIONS_DIR') or CODEX_SESSIONS_DIR

    if not target_dir or not os.path.exists(target_dir):
        if sessions_dir is None:
            fallback = os.path.expanduser(os.path.join('~', '.codex'))
            if os.path.exists(fallback):
                target_dir = fallback
            else:
                return {
                    'available': False,
                    'source': 'local_session_logs',
                    'source_label': 'Local Codex Session Logs',
                    'observed_at': None,
                    'plan_type': None,
                    'credits': None,
                    'windows': [],
                    'error': 'Codex session logs directory not found'
                }
        else:
            return {
                'available': False,
                'source': 'local_session_logs',
                'source_label': 'Local Codex Session Logs',
                'observed_at': None,
                'plan_type': None,
                'credits': None,
                'windows': [],
                'error': 'Codex session logs directory not found'
            }

    candidate_heap = []
    visited_dirs = 0
    visited_entries = 0
    stop_walk = False
    try:
        if os.path.isdir(target_dir):
            for root, _, files in os.walk(target_dir):
                visited_dirs += 1
                if visited_dirs > max_dirs:
                    break
                for f in files:
                    visited_entries += 1
                    if visited_entries > max_entries:
                        stop_walk = True
                        break
                    if f.endswith('.jsonl'):
                        p = os.path.join(root, f)
                        if is_cloud_offline_file(p):
                            continue
                        try:
                            mtime = os.path.getmtime(p)
                            candidate = (mtime, p)
                            if len(candidate_heap) < max_files:
                                heapq.heappush(candidate_heap, candidate)
                            elif candidate > candidate_heap[0]:
                                heapq.heapreplace(candidate_heap, candidate)
                        except Exception:
                            pass
                if stop_walk:
                    break
        elif os.path.isfile(target_dir) and target_dir.endswith('.jsonl'):
            candidate_heap.append((os.path.getmtime(target_dir), target_dir))
    except Exception:
        pass

    if not candidate_heap:
        return {
            'available': False,
            'source': 'local_session_logs',
            'source_label': 'Local Codex Session Logs',
            'observed_at': None,
            'plan_type': None,
            'credits': None,
            'windows': [],
            'error': 'No session log files found'
        }

    candidate_files = [(p, mtime) for mtime, p in sorted(candidate_heap, reverse=True)]

    valid_events = []
    candidate_count = 0
    malformed_candidates = 0
    skipped_reasons = []

    for file_path, mtime in candidate_files:
        file_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)

        tail_lines = _read_codex_tail_lines(file_path, max_bytes=max_tail_bytes, max_lines=max_tail_lines)
        for line in reversed(tail_lines):
            line = line.strip()
            if not line or 'rate_limits' not in line:
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    continue
                # Invariant: Only item.type=event_msg records may supply rate_limits
                if item.get('type') != 'event_msg':
                    continue

                payload = item.get('payload')
                if not isinstance(payload, dict):
                    continue
                # Invariant: Only payload.type=token_count events may supply rate_limits
                if payload.get('type') != 'token_count':
                    continue

                rate_limits = payload.get('rate_limits')
                if not isinstance(rate_limits, dict) or not rate_limits:
                    continue
                candidate_count += 1
                ts = item.get('timestamp') or payload.get('timestamp')
                ev_dt = _parse_iso_utc(ts) if ts else None
                if not ev_dt:
                    ev_dt = file_dt
                normalized = normalize_codex_rate_limits(rate_limits, event_timestamp=ts or ev_dt)
                if not normalized or not normalized.get('available') or not normalized.get('windows'):
                    malformed_candidates += 1
                    if len(skipped_reasons) < 5:
                        skipped_reasons.append('malformed_or_null_windows')
                    continue
                valid_events.append((ev_dt, normalized))
            except Exception:
                malformed_candidates += 1
                if len(skipped_reasons) < 5:
                    skipped_reasons.append('invalid_event_record')
                continue
    if not valid_events:
        return {
            'available': False,
            'source': 'local_session_logs',
            'source_label': 'Local Codex Session Logs',
            'observed_at': None,
            'plan_type': None,
            'credits': None,
            'windows': [],
            'error': 'No valid rate limit events found in local Codex session logs',
            'diagnostics': {
                'files_scanned': len(candidate_files),
                'candidate_events': min(candidate_count, 1000),
                'valid_events': 0,
                'malformed_candidates': min(malformed_candidates, 1000),
                'skipped_reasons': skipped_reasons,
            }
        }

    # Select the newest valid observation independently for each duration. A
    # newer payload containing only a null/malformed window cannot hide an older
    # valid 5-hour or weekly window.
    newest_event_dt, newest_normalized = max(valid_events, key=lambda item: item[0])
    windows_by_duration = {}
    for event_dt, normalized_event in valid_events:
        for window in normalized_event.get('windows', []):
            duration = window.get('window_minutes')
            if duration is None:
                continue
            prior = windows_by_duration.get(duration)
            if prior is None or event_dt >= prior[0]:
                windows_by_duration[duration] = (event_dt, window)
    normalized = dict(newest_normalized)
    normalized['windows'] = [windows_by_duration[key][1] for key in sorted(windows_by_duration)]
    normalized['observed_at'] = newest_normalized.get('observed_at') or newest_event_dt.isoformat()
    normalized['diagnostics'] = {
        'files_scanned': len(candidate_files),
        'candidate_events': min(candidate_count, 1000),
        'valid_events': min(len(valid_events), 1000),
        'malformed_candidates': min(malformed_candidates, 1000),
        'skipped_reasons': skipped_reasons,
        'windows_selected': len(normalized['windows']),
        'last_valid_observed_at': normalized.get('observed_at'),
    }
    return normalized


def _extract_codex_5h_quota_observation(rate_limits, ts_str, current_model, current_effort,
                                        cumulative_state, session_key, task_id=None,
                                        service_tier=None):
    """Return one model-attributed 5h quota observation from a token_count event."""
    if not isinstance(rate_limits, dict) or not rate_limits or not isinstance(cumulative_state, dict):
        return None
    ev_dt = _parse_iso_utc(ts_str)
    if ev_dt is None:
        return None
    normalized = normalize_codex_rate_limits(rate_limits, event_timestamp=ts_str, now_utc=ev_dt)
    if not isinstance(normalized, dict):
        return None
    five_hour_window = None
    weekly_window = None
    for window in normalized.get('windows') or []:
        if not isinstance(window, dict):
            continue
        try:
            duration = int(float(window.get('window_minutes')))
        except (TypeError, ValueError, OverflowError):
            continue
        if duration == 300:
            five_hour_window = window
        elif duration == 10080:
            weekly_window = window
    if not five_hour_window and not weekly_window:
        return None
    used_pct = None
    reset_dt = None
    if five_hour_window:
        try:
            used_pct = float(five_hour_window.get('used_percent'))
        except (TypeError, ValueError, OverflowError):
            pass
        reset_dt = _parse_iso_utc(five_hour_window.get('resets_at'))
    if used_pct is None or not math.isfinite(used_pct) or reset_dt is None:
        used_pct = None
        reset_dt = None

    total_tokens = _safe_nonnegative_int(cumulative_state.get('total_tokens'))
    if total_tokens <= 0:
        return None
    model_key, canon_model, effort_value, effort_label = normalize_codex_model_effort(
        current_model, current_effort, service_tier
    )
    weekly_used_pct = None
    weekly_reset_dt = None
    if weekly_window:
        try:
            weekly_used_pct = float(weekly_window.get('used_percent'))
        except (TypeError, ValueError, OverflowError):
            pass
        weekly_reset_dt = _parse_iso_utc(weekly_window.get('resets_at'))
    if weekly_used_pct is None or not math.isfinite(weekly_used_pct) or weekly_reset_dt is None:
        weekly_used_pct = None
        weekly_reset_dt = None
    if used_pct is None and weekly_used_pct is None:
        return None
    return {
        'ts': ts_str,
        'session': str(session_key or ''),
        'task_id': str(task_id or ''),
        'model_key': model_key,
        'model_id': canon_model,
        'reasoning_effort': effort_value,
        'service_tier': _codex_service_tier(service_tier) or 'default',
        'effort_label': effort_label,
        'used_percent': round(max(0.0, min(100.0, used_pct)), 2) if used_pct is not None else None,
        'resets_at': reset_dt.isoformat() if reset_dt is not None else None,
        'weekly_used_percent': round(max(0.0, min(100.0, weekly_used_pct)), 2) if weekly_used_pct is not None else None,
        'weekly_resets_at': weekly_reset_dt.isoformat() if weekly_reset_dt is not None else None,
        'total_tokens': total_tokens,
        'input_tokens': _safe_nonnegative_int(cumulative_state.get('input_tokens')),
        'cached_input_tokens': _safe_nonnegative_int(cumulative_state.get('cached_input_tokens')),
        'cache_write_input_tokens': _safe_nonnegative_int(cumulative_state.get('cache_write_input_tokens')),
        'output_tokens': _safe_nonnegative_int(cumulative_state.get('output_tokens')),
        'thinking_tokens': _safe_nonnegative_int(cumulative_state.get('reasoning_output_tokens')),
    }


def _median_numeric(values):
    clean = sorted(
        float(v) for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))
    )
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2.0


def _codex_ratio_bridge_graph(context_values):
    """Build pairwise model-ratio edges from contexts where models overlap.

    ``context_values`` maps a context key (task category, local day, ...) to
    positive model metrics.  Each context contributes one ratio observation per
    model pair; edge values are robust medians across overlapping contexts.
    """
    pair_samples = {}
    for context_key, raw_values in (context_values or {}).items():
        if not isinstance(raw_values, dict):
            continue
        values = {}
        for model_key, raw_value in raw_values.items():
            if not isinstance(model_key, str) or not model_key:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value) and value > 0:
                values[model_key] = value
        model_keys = sorted(values)
        for index, left in enumerate(model_keys):
            for right in model_keys[index + 1:]:
                ratio = values[left] / values[right]
                if not math.isfinite(ratio) or ratio <= 0:
                    continue
                pair_samples.setdefault((left, right), []).append({
                    'ratio': ratio,
                    'context': str(context_key),
                })

    graph = {}
    for (left, right), samples in pair_samples.items():
        ratio = _median_numeric(sample.get('ratio') for sample in samples)
        if not ratio or ratio <= 0:
            continue
        contexts = [sample.get('context') for sample in samples if sample.get('context')]
        edge = {
            'ratio': float(ratio),
            'evidence_count': len(samples),
            'contexts': contexts,
        }
        graph.setdefault(left, {})[right] = edge
        graph.setdefault(right, {})[left] = {
            'ratio': 1.0 / float(ratio),
            'evidence_count': len(samples),
            'contexts': contexts,
        }
    return graph


def _codex_find_bridge_ratio(graph, source_model, target_model, max_hops=4):
    """Return the strongest shortest ratio path from source to target."""
    if not source_model or not target_model:
        return None
    if source_model == target_model:
        return {
            'ratio': 1.0,
            'path': [source_model],
            'hops': 0,
            'support': 0,
            'edge_evidence_total': 0,
        }
    if source_model not in (graph or {}) or target_model not in (graph or {}):
        return None
    try:
        hop_limit = max(1, min(8, int(max_hops)))
    except (TypeError, ValueError, OverflowError):
        hop_limit = 4

    queue = [(source_model, 1.0, [source_model], None, 0)]
    candidates = []
    shortest_hops = None
    while queue:
        current, ratio_so_far, path, support, evidence_total = queue.pop(0)
        hops = len(path) - 1
        if hops >= hop_limit or (shortest_hops is not None and hops >= shortest_hops):
            continue
        neighbors = (graph or {}).get(current) or {}
        ordered = sorted(
            neighbors.items(),
            key=lambda item: (-int((item[1] or {}).get('evidence_count') or 0), item[0]),
        )
        for neighbor, edge in ordered:
            if neighbor in path:
                continue
            try:
                edge_ratio = float((edge or {}).get('ratio'))
                edge_evidence = int((edge or {}).get('evidence_count') or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(edge_ratio) or edge_ratio <= 0:
                continue
            next_path = path + [neighbor]
            next_ratio = ratio_so_far * edge_ratio
            next_support = edge_evidence if support is None else min(support, edge_evidence)
            next_total = evidence_total + edge_evidence
            next_hops = len(next_path) - 1
            if neighbor == target_model:
                if shortest_hops is None:
                    shortest_hops = next_hops
                if next_hops == shortest_hops:
                    candidates.append({
                        'ratio': next_ratio,
                        'path': next_path,
                        'hops': next_hops,
                        'support': next_support or 0,
                        'edge_evidence_total': next_total,
                    })
                continue
            if shortest_hops is None or next_hops < shortest_hops:
                queue.append((neighbor, next_ratio, next_path, next_support, next_total))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (
        -int(item.get('support') or 0),
        -int(item.get('edge_evidence_total') or 0),
        tuple(item.get('path') or []),
    ))
    return candidates[0]


def _codex_bridge_confidence(bridge):
    """Estimated ratios are intentionally capped below direct high confidence."""
    if not isinstance(bridge, dict):
        return 'low'
    hops = int(bridge.get('hops') or 0)
    support = int(bridge.get('support') or 0)
    if hops <= 2 and support >= 2:
        return 'medium'
    return 'low'


def _codex_estimate_baseline_from_anchors(anchor_values, graph, baseline_model,
                                           prefer_excluding=None, max_hops=4):
    """Estimate a missing baseline from measured anchors connected by ratio bridges."""
    candidates = []
    for anchor_model, raw_value in (anchor_values or {}).items():
        if anchor_model == baseline_model:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        bridge = _codex_find_bridge_ratio(
            graph, anchor_model, baseline_model, max_hops=max_hops
        )
        if not bridge:
            continue
        bridge_ratio = float(bridge.get('ratio') or 0)
        if not math.isfinite(bridge_ratio) or bridge_ratio <= 0:
            continue
        estimated_value = value / bridge_ratio
        if not math.isfinite(estimated_value) or estimated_value <= 0:
            continue
        candidates.append({
            'anchor_model': anchor_model,
            'anchor_value': value,
            'estimated_baseline': estimated_value,
            'bridge': bridge,
        })

    if prefer_excluding:
        alternatives = [
            candidate for candidate in candidates
            if candidate.get('anchor_model') != prefer_excluding
        ]
        if alternatives:
            candidates = alternatives
    if not candidates:
        return None

    estimate = _median_numeric(candidate.get('estimated_baseline') for candidate in candidates)
    if not estimate or estimate <= 0:
        return None
    representative = min(
        candidates,
        key=lambda candidate: (
            abs(float(candidate.get('estimated_baseline') or 0) - estimate),
            int((candidate.get('bridge') or {}).get('hops') or 99),
            -int((candidate.get('bridge') or {}).get('support') or 0),
            candidate.get('anchor_model') or '',
        ),
    )
    bridge_confidence = _codex_bridge_confidence(representative.get('bridge'))
    if len(candidates) >= 2 and bridge_confidence == 'low':
        bridge_confidence = 'medium'
    return {
        'value': float(estimate),
        'candidate_count': len(candidates),
        'anchors': [candidate.get('anchor_model') for candidate in candidates],
        'representative': representative,
        'confidence': bridge_confidence,
    }


def _same_codex_quota_cycle(a, b, tolerance_seconds=180):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    a_reset = _parse_iso_utc(a.get('resets_at'))
    b_reset = _parse_iso_utc(b.get('resets_at'))
    if a_reset is None or b_reset is None:
        return False
    return abs((a_reset - b_reset).total_seconds()) <= tolerance_seconds


def _is_codex_task_boundary(payload_type):
    return payload_type in ('task_started', 'task_complete')


def _build_codex_quota_intervals(quota_observations, min_quota_delta=2.0):
    """Return clean, non-overlapping same-model intervals within one 5h cycle."""
    try:
        min_delta = max(0.1, float(min_quota_delta))
    except (TypeError, ValueError, OverflowError):
        min_delta = 2.0

    by_session = {}
    for raw in quota_observations or []:
        if not isinstance(raw, dict):
            continue
        session = str(raw.get('session') or '')
        ts = _parse_iso_utc(raw.get('ts'))
        model_key = raw.get('model_key')
        reset_dt = _parse_iso_utc(raw.get('resets_at'))
        try:
            used_pct = float(raw.get('used_percent'))
            total_tokens = int(raw.get('total_tokens'))
        except (TypeError, ValueError, OverflowError):
            continue
        if (not session or ts is None or reset_dt is None or not isinstance(model_key, str) or
                not model_key or not math.isfinite(used_pct) or total_tokens <= 0):
            continue
        item = dict(raw)
        item['_ts_dt'] = ts
        item['_used_pct'] = used_pct
        item['_total_tokens'] = total_tokens
        by_session.setdefault(session, []).append(item)

    intervals = []
    component_fields = (
        'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'thinking_tokens',
    )
    for session, observations in by_session.items():
        observations.sort(key=lambda item: item['_ts_dt'])
        anchor = None
        for current in observations:
            if anchor is None:
                anchor = current
                continue
            if (current.get('model_key') != anchor.get('model_key') or
                    not _same_codex_quota_cycle(anchor, current)):
                anchor = current
                continue
            quota_delta = current['_used_pct'] - anchor['_used_pct']
            token_delta = current['_total_tokens'] - anchor['_total_tokens']
            if quota_delta < 0 or token_delta <= 0:
                anchor = current
                continue
            if quota_delta < min_delta:
                continue
            interval = {
                'session': session,
                'model_key': current.get('model_key'),
                'model_id': current.get('model_id'),
                'reasoning_effort': current.get('reasoning_effort'),
                'start_ts': anchor.get('ts'),
                'end_ts': current.get('ts'),
                'resets_at': current.get('resets_at'),
                'quota_delta_pct': quota_delta,
                'token_delta': token_delta,
                'tokens_per_quota_pct': token_delta / quota_delta,
            }
            for field in component_fields:
                try:
                    delta = int(current.get(field, 0)) - int(anchor.get(field, 0))
                except (TypeError, ValueError, OverflowError):
                    delta = 0
                interval[field + '_delta'] = max(0, delta)
            intervals.append(interval)
            anchor = current

    return intervals, sum(len(v) for v in by_session.values()), min_delta


def _codex_quota_interval_confidence(samples):
    """Grade quota evidence from interval count, independent sessions and quota span."""
    valid_samples = [sample for sample in (samples or []) if isinstance(sample, dict)]
    sample_count = len(valid_samples)
    session_count = len({
        sample.get('session') for sample in valid_samples if sample.get('session')
    })
    quota_span = sum(float(sample.get('quota_delta_pct') or 0) for sample in valid_samples)
    if sample_count >= 5 and session_count >= 3 and quota_span >= 30:
        return 'high'
    if sample_count >= 3 and session_count >= 2 and quota_span >= 10:
        return 'medium'
    return 'low'


def build_codex_weekly_capacity_history(quota_observations, now=None, range_days=367,
                                        min_quota_delta=1.0):
    """Infer API-USD full-week equivalents from individual local quota changes.

    Each measured interval remains visible. Recent time-bounded medians can
    react to a policy change while the cumulative median shows convergence.
    Neither series is an official subscription allowance.
    """
    now_utc = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    now_utc = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc)
    days = max(7, min(367, int(range_days)))
    cutoff = now_utc - timedelta(days=days)
    by_session = {}
    for raw in quota_observations or []:
        if not isinstance(raw, dict):
            continue
        ts = _parse_iso_utc(raw.get('ts'))
        reset = _parse_iso_utc(raw.get('weekly_resets_at'))
        try:
            pct = float(raw.get('weekly_used_percent'))
            tokens = int(raw.get('total_tokens'))
        except (TypeError, ValueError, OverflowError):
            continue
        session = str(raw.get('session') or '')
        model = str(raw.get('model_key') or '')
        if (not session or not model or ts is None or reset is None or
                ts < cutoff or ts > now_utc or not math.isfinite(pct) or
                not 0 <= pct <= 100 or tokens <= 0):
            continue
        by_session.setdefault(session, []).append((ts, reset, pct, tokens, model, raw))

    samples = []
    unpriced_count = 0
    component_fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')
    for session, observations in by_session.items():
        observations.sort(key=lambda row: row[0])
        anchor = None
        for current in observations:
            if anchor is None:
                anchor = current
                continue
            same_cycle = abs((current[1] - anchor[1]).total_seconds()) <= 180
            elapsed = (current[0] - anchor[0]).total_seconds()
            current_account = str(current[5].get('account_id') or '')
            anchor_account = str(anchor[5].get('account_id') or '')
            if (current[4] != anchor[4] or not same_cycle or elapsed <= 0 or
                    elapsed > 6 * 3600 or current_account != anchor_account):
                anchor = current
                continue
            pct_delta = current[2] - anchor[2]
            token_delta = current[3] - anchor[3]
            if pct_delta < 0 or token_delta <= 0:
                anchor = current
                continue
            if pct_delta < min_quota_delta:
                continue
            deltas = {}
            for field in component_fields:
                deltas[field] = max(0, _safe_nonnegative_int(current[5].get(field)) -
                                    _safe_nonnegative_int(anchor[5].get(field)))
            cost = estimate_model_cost(
                current[4], input_tokens=deltas['input_tokens'],
                cached_input_tokens=deltas['cached_input_tokens'],
                cache_write_input_tokens=deltas['cache_write_input_tokens'],
                output_tokens=deltas['output_tokens'],
            )
            if cost is None or not math.isfinite(cost) or cost <= 0:
                unpriced_count += 1
                anchor = current
                continue
            samples.append({
                'observed_at': current[0].isoformat(),
                'session': session,
                'model_key': current[4],
                'account_id': current_account or None,
                'account_attribution': current[5].get('account_attribution') if current_account else None,
                'quota_delta_pct': round(pct_delta, 2),
                'token_delta': token_delta,
                'api_cost_delta_usd': round(cost, 6),
                'full_week_usd': round(cost * 100.0 / pct_delta, 2),
            })
            anchor = current

    samples.sort(key=lambda row: (row['observed_at'], row['session']))
    by_account = {}
    for sample in samples:
        by_account.setdefault(sample['account_id'] or '__unknown__', []).append(sample)
    for account_samples in by_account.values():
        sample_dates = [_parse_iso_utc(sample['observed_at']) for sample in account_samples]
        low_half = []
        high_half = []
        window_starts = {7: 0, 14: 0, 30: 0}
        for index, sample in enumerate(account_samples):
            observed_at = sample_dates[index]
            value = sample['full_week_usd']
            if not low_half or value <= -low_half[0]:
                heapq.heappush(low_half, -value)
            else:
                heapq.heappush(high_half, value)
            if len(low_half) > len(high_half) + 1:
                heapq.heappush(high_half, -heapq.heappop(low_half))
            elif len(high_half) > len(low_half):
                heapq.heappush(low_half, -heapq.heappop(high_half))
            sample['cumulative_median_usd'] = round(
                -low_half[0] if len(low_half) > len(high_half) else (-low_half[0] + high_half[0]) / 2, 2
            )
            sample['recent'] = {}
            for window_days in (7, 14, 30):
                window_start = observed_at - timedelta(days=window_days)
                first = window_starts[window_days]
                while first < index and sample_dates[first] <= window_start:
                    first += 1
                window_starts[window_days] = first
                nearby = account_samples[first:index + 1]
                sample['recent'][str(window_days)] = {
                    'median_usd': round(_median_numeric(
                        [entry['full_week_usd'] for entry in nearby]
                    ), 2),
                    'sample_count': len(nearby),
                    'session_count': len({entry['session'] for entry in nearby}),
                    'quota_span_pct': round(sum(entry['quota_delta_pct'] for entry in nearby), 2),
                    'confidence': _codex_quota_interval_confidence(nearby),
                }
    return {
        'available': bool(samples),
        'source': 'local_codex_session_logs',
        'basis': 'api_cost_delta_over_weekly_quota_delta',
        'range_days': days,
        'observation_count': sum(len(rows) for rows in by_session.values()),
        'sample_count': len(samples),
        'account_sample_counts': {key: len(rows) for key, rows in by_account.items()},
        'unpriced_interval_count': unpriced_count,
        'points': samples,
    }


def build_codex_quota_efficiency(quota_observations, min_quota_delta=2.0):
    """Build all-time empirical 5h quota efficiency by model/effort."""
    intervals, observation_count, min_delta = _build_codex_quota_intervals(
        quota_observations, min_quota_delta=min_quota_delta
    )
    component_fields = (
        'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'thinking_tokens',
    )

    grouped = {}
    for interval in intervals:
        grouped.setdefault(interval['model_key'], []).append(interval)

    models = []
    for model_key, samples in grouped.items():
        tokens_per_pct = _median_numeric(s['tokens_per_quota_pct'] for s in samples)
        if not tokens_per_pct or tokens_per_pct <= 0:
            continue
        quota_span = sum(float(s['quota_delta_pct']) for s in samples)
        token_span = sum(int(s['token_delta']) for s in samples)
        sessions = {s['session'] for s in samples}
        sample_count = len(samples)
        confidence = _codex_quota_interval_confidence(samples)
        first = samples[0]
        model_row = {
            'model_key': model_key,
            'model_id': first.get('model_id') or model_key,
            'reasoning_effort': first.get('reasoning_effort') or 'medium',
            'tokens_per_quota_pct': round(tokens_per_pct, 2),
            'quota_pct_per_1m_tokens': round(1_000_000.0 / tokens_per_pct, 4),
            'sample_count': sample_count,
            'quota_span_pct': round(quota_span, 2),
            'token_span': token_span,
            'session_count': len(sessions),
            'confidence': confidence,
            'relative_quota_burn_vs_sol_high': None,
        }
        for field in component_fields:
            rates = []
            for sample in samples:
                qd = float(sample.get('quota_delta_pct') or 0)
                if qd > 0:
                    rates.append(float(sample.get(field + '_delta') or 0) / qd)
            model_row[field + '_per_quota_pct'] = round(_median_numeric(rates) or 0.0, 2)
        model_row['uncached_input_tokens_per_quota_pct'] = round(
            max(0.0, model_row['input_tokens_per_quota_pct'] -
                model_row['cached_input_tokens_per_quota_pct']), 2
        )
        models.append(model_row)

    sol_high = next((m for m in models if m.get('model_key') == '5.6 sol high'), None)
    if sol_high and sol_high.get('tokens_per_quota_pct', 0) > 0:
        baseline = float(sol_high['tokens_per_quota_pct'])
        for model in models:
            tpp = float(model.get('tokens_per_quota_pct') or 0)
            if tpp > 0:
                model['relative_quota_burn_vs_sol_high'] = round(baseline / tpp, 3)

    models.sort(key=lambda row: (
        row.get('relative_quota_burn_vs_sol_high') is None,
        -(row.get('relative_quota_burn_vs_sol_high') or 0),
        row.get('model_key') or '',
    ))
    return {
        'available': bool(models),
        'method': 'empirical_same_session_same_5h_cycle_median',
        'min_quota_delta_pct': min_delta,
        'baseline_model_key': '5.6 sol high',
        'models': models,
        'interval_count': len(intervals),
        'observation_count': observation_count,
        'note': (
            'Empirical local measurement from Codex token_count rate-limit snapshots; '
            'not an official OpenAI quota-weight formula.'
        ),
    }


def _codex_quota_bridge_graph(dated_intervals, cutoff_dt):
    """Learn model-to-model quota-efficiency ratios from historical same-day overlap."""
    by_day_model = {}
    for item in dated_intervals or []:
        end_dt = item.get('_end_dt') if isinstance(item, dict) else None
        if not isinstance(end_dt, datetime) or end_dt > cutoff_dt:
            continue
        model_key = item.get('model_key')
        if not isinstance(model_key, str) or not model_key:
            continue
        try:
            value = float(item.get('tokens_per_quota_pct'))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        day_key = end_dt.astimezone().date().isoformat()
        by_day_model.setdefault(day_key, {}).setdefault(model_key, []).append(value)

    contexts = {}
    for day_key, model_values in by_day_model.items():
        context = {}
        for model_key, values in model_values.items():
            median_value = _median_numeric(values)
            if median_value and median_value > 0:
                context[model_key] = median_value
        if len(context) >= 2:
            contexts[day_key] = context
    return _codex_ratio_bridge_graph(contexts)


def build_codex_quota_efficiency_timeline(quota_observations, now=None, range_days=90,
                                          window_days_options=(1, 3, 7),
                                          min_quota_delta=1.0):
    """Build local rolling quota-efficiency medians for detecting model changes over time.

    Direct comparisons use Sol High observations inside the same trailing window.  When
    Sol High is absent, the baseline may be estimated through historical same-day model
    overlaps.  Model-local points are retained even when no bridge is available.
    """
    now_utc = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    try:
        days = max(7, min(367, int(range_days)))
    except (TypeError, ValueError, OverflowError):
        days = 90

    intervals, observation_count, min_delta = _build_codex_quota_intervals(
        quota_observations, min_quota_delta=min_quota_delta
    )
    dated_intervals = []
    for interval in intervals:
        end_dt = _parse_iso_utc(interval.get('end_ts'))
        if end_dt is None or end_dt > now_utc:
            continue
        item = dict(interval)
        item['_end_dt'] = end_dt
        dated_intervals.append(item)

    today_start = _local_day_start_utc(now_utc)
    day_starts = [today_start - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    date_keys = [day.astimezone().date().isoformat() for day in day_starts]
    baseline_key = '5.6 sol high'
    windows = {}

    for raw_window_days in window_days_options or (1, 3, 7):
        try:
            window_days = max(1, min(30, int(raw_window_days)))
        except (TypeError, ValueError, OverflowError):
            continue
        points_by_model = {}
        model_meta = {}
        for day_start, date_key in zip(day_starts, date_keys):
            day_end = min(day_start + timedelta(days=1), now_utc)
            if day_end <= day_start:
                continue
            window_start = day_end - timedelta(days=window_days)
            nearby = [
                item for item in dated_intervals
                if window_start < item['_end_dt'] <= day_end
            ]
            grouped = {}
            for item in nearby:
                grouped.setdefault(item.get('model_key'), []).append(item)
            grouped_values = {}
            for model_key, samples in grouped.items():
                tokens_per_pct = _median_numeric(
                    sample.get('tokens_per_quota_pct') for sample in samples
                )
                if isinstance(model_key, str) and model_key and tokens_per_pct and tokens_per_pct > 0:
                    grouped_values[model_key] = tokens_per_pct
            baseline_samples = grouped.get(baseline_key) or []
            baseline_tpp = _median_numeric(
                sample.get('tokens_per_quota_pct') for sample in baseline_samples
            )
            baseline_source = 'direct' if baseline_tpp and baseline_tpp > 0 else 'unavailable'
            baseline_bridge = None
            if baseline_source != 'direct' and grouped_values:
                bridge_graph = _codex_quota_bridge_graph(dated_intervals, day_end)
                baseline_bridge = _codex_estimate_baseline_from_anchors(
                    grouped_values, bridge_graph, baseline_key
                )
                if baseline_bridge and baseline_bridge.get('value', 0) > 0:
                    baseline_tpp = float(baseline_bridge['value'])
                    baseline_source = 'bridge'
            if baseline_source == 'direct':
                baseline_confidence = _codex_quota_interval_confidence(baseline_samples)
            elif baseline_source == 'bridge':
                baseline_confidence = baseline_bridge.get('confidence') or 'low'
            else:
                baseline_confidence = None
            baseline_quota_span = sum(
                float(sample.get('quota_delta_pct') or 0) for sample in baseline_samples
            )
            baseline_sessions = {
                sample.get('session') for sample in baseline_samples if sample.get('session')
            }
            representative_bridge = (baseline_bridge or {}).get('representative') or {}
            representative_path = representative_bridge.get('bridge') or {}

            for model_key, tokens_per_pct in grouped_values.items():
                samples = grouped.get(model_key) or []
                model_confidence = _codex_quota_interval_confidence(samples)
                confidence_rank = {'low': 0, 'medium': 1, 'high': 2}
                confidence = model_confidence
                if baseline_confidence:
                    confidence = min(
                        (model_confidence, baseline_confidence),
                        key=lambda value: confidence_rank.get(value, 0),
                    )
                last_sample = max(samples, key=lambda sample: sample['_end_dt'])
                model_meta.setdefault(model_key, {
                    'model_key': model_key,
                    'model_id': last_sample.get('model_id') or model_key,
                    'reasoning_effort': last_sample.get('reasoning_effort') or 'medium',
                })
                relative_burn = None
                if baseline_tpp and baseline_tpp > 0:
                    relative_burn = round(baseline_tpp / tokens_per_pct, 3)
                points_by_model.setdefault(model_key, []).append({
                    'date': date_key,
                    'relative_quota_burn_vs_sol_high': relative_burn,
                    'tokens_per_quota_pct': round(tokens_per_pct, 2),
                    'baseline_tokens_per_quota_pct': round(baseline_tpp, 2) if baseline_tpp else None,
                    'baseline_source': baseline_source,
                    'comparison_estimated': baseline_source == 'bridge',
                    'sample_count': len(samples),
                    'baseline_sample_count': len(baseline_samples),
                    'baseline_estimate_anchor_count': int((baseline_bridge or {}).get('candidate_count') or 0),
                    'bridge_anchor_model': representative_bridge.get('anchor_model'),
                    'bridge_path': representative_path.get('path') or [],
                    'bridge_hops': int(representative_path.get('hops') or 0),
                    'bridge_support': int(representative_path.get('support') or 0),
                    'quota_span_pct': round(sum(float(s.get('quota_delta_pct') or 0) for s in samples), 2),
                    'session_count': len({s.get('session') for s in samples if s.get('session')}),
                    'baseline_quota_span_pct': round(baseline_quota_span, 2),
                    'baseline_session_count': len(baseline_sessions),
                    'model_confidence': model_confidence,
                    'baseline_confidence': baseline_confidence,
                    'confidence': confidence,
                    'last_observed_at': last_sample.get('end_ts'),
                })

        model_rows = []
        for model_key, points in points_by_model.items():
            if not points:
                continue
            latest = points[-1]
            latest_dt = datetime.fromisoformat(latest['date'])
            previous_target = latest_dt - timedelta(days=window_days)
            previous = None
            for point in reversed(points[:-1]):
                if datetime.fromisoformat(point['date']) <= previous_target:
                    previous = point
                    break
            if previous is None and len(points) > 1:
                previous = points[-2]
            latest_value = float(latest.get('relative_quota_burn_vs_sol_high') or 0)
            previous_value = float(previous.get('relative_quota_burn_vs_sol_high') or 0) if previous else 0.0
            change_pct = None
            if latest_value > 0 and previous_value > 0:
                change_pct = round((latest_value / previous_value - 1.0) * 100.0, 1)
            row = dict(model_meta[model_key])
            row.update({
                'points': points,
                'latest': latest,
                'previous': previous,
                'change_pct': change_pct,
            })
            model_rows.append(row)
        model_rows.sort(key=lambda row: (
            -float((row.get('latest') or {}).get('relative_quota_burn_vs_sol_high') or 0),
            row.get('model_key') or '',
        ))
        windows[str(window_days)] = {
            'available': bool(model_rows),
            'window_days': window_days,
            'models': model_rows,
        }

    return {
        'available': any(value.get('available') for value in windows.values()),
        'method': 'daily_trailing_window_same_period_sol_high_median',
        'range_days': days,
        'window_days_options': sorted(int(key) for key in windows),
        'min_quota_delta_pct': min_delta,
        'baseline_model_key': baseline_key,
        'dates': date_keys,
        'windows': windows,
        'interval_count': len(dated_intervals),
        'observation_count': observation_count,
        'generated_at': now_utc.isoformat(),
        'note': (
            'Daily trailing-window medians from local Codex rate-limit snapshots. Same-window '
            'Sol High samples are preferred; when absent, the baseline may be estimated through '
            'historical overlap bridges. Model-local observations remain visible even when no '
            'comparison bridge exists. Confidence is capped by the weaker side. This is not an '
            'official OpenAI weight.'
        ),
    }


def _build_codex_quota_task_samples(quota_observations, usage_events,
                                    finished_turn_ids=None, min_quota_delta=0.5,
                                    min_token_coverage=0.25):
    """Return one validated quota sample per completed, single-model Codex task."""
    try:
        min_delta = max(0.1, float(min_quota_delta))
    except (TypeError, ValueError, OverflowError):
        min_delta = 0.5
    try:
        min_coverage = max(0.05, min(1.0, float(min_token_coverage)))
    except (TypeError, ValueError, OverflowError):
        min_coverage = 0.25

    task_tokens = {}
    task_event_models = {}
    for event in usage_events or []:
        if not isinstance(event, dict):
            continue
        task_id = str(event.get('task_id') or '')
        model_key = event.get('model_key')
        try:
            tokens = int(event.get('tot') or 0)
        except (TypeError, ValueError, OverflowError):
            tokens = 0
        if not task_id or not isinstance(model_key, str) or not model_key:
            continue
        task_tokens[task_id] = task_tokens.get(task_id, 0) + max(0, tokens)
        task_event_models.setdefault(task_id, set()).add(model_key)

    finished = {
        str(turn_id) for turn_id in (finished_turn_ids or [])
        if isinstance(turn_id, str) and turn_id
    }
    by_task = {}
    for raw in quota_observations or []:
        if not isinstance(raw, dict):
            continue
        session = str(raw.get('session') or '')
        task_id = str(raw.get('task_id') or '')
        model_key = raw.get('model_key')
        ts = _parse_iso_utc(raw.get('ts'))
        reset_dt = _parse_iso_utc(raw.get('resets_at'))
        try:
            used_pct = float(raw.get('used_percent'))
            total_tokens = int(raw.get('total_tokens'))
        except (TypeError, ValueError, OverflowError):
            continue
        if (not session or not task_id or task_id not in finished or
                not isinstance(model_key, str) or not model_key or
                ts is None or reset_dt is None or not math.isfinite(used_pct) or total_tokens <= 0):
            continue
        item = dict(raw)
        item['_ts_dt'] = ts
        item['_used_pct'] = used_pct
        item['_total_tokens'] = total_tokens
        by_task.setdefault((session, task_id), []).append(item)

    samples = []
    excluded_mixed = 0
    excluded_cross_cycle = 0
    excluded_insufficient = 0
    excluded_low_coverage = 0
    for task_key, observations in by_task.items():
        observations.sort(key=lambda item: item['_ts_dt'])
        if len(observations) < 2:
            excluded_insufficient += 1
            continue
        first = observations[0]
        if any(not _same_codex_quota_cycle(first, item) for item in observations[1:]):
            excluded_cross_cycle += 1
            continue
        model_keys = {item.get('model_key') for item in observations if item.get('model_key')}
        model_keys.update(task_event_models.get(task_key[1], set()))
        if len(model_keys) != 1:
            excluded_mixed += 1
            continue
        last = observations[-1]
        quota_delta = last['_used_pct'] - first['_used_pct']
        token_delta = last['_total_tokens'] - first['_total_tokens']
        if quota_delta < min_delta or token_delta <= 0:
            excluded_insufficient += 1
            continue
        full_task_tokens = max(int(task_tokens.get(task_key[1]) or 0), token_delta)
        if full_task_tokens <= 0:
            excluded_insufficient += 1
            continue
        coverage = min(1.0, token_delta / full_task_tokens)
        if coverage < min_coverage:
            excluded_low_coverage += 1
            continue
        estimated_quota = quota_delta / coverage
        if not math.isfinite(estimated_quota) or estimated_quota <= 0 or estimated_quota > 100:
            excluded_insufficient += 1
            continue
        samples.append({
            'session': task_key[0],
            'task_id': task_key[1],
            'model_key': next(iter(model_keys)),
            'model_id': first.get('model_id') or next(iter(model_keys)),
            'reasoning_effort': first.get('reasoning_effort') or 'medium',
            'observed_quota_delta_pct': quota_delta,
            'estimated_quota_delta_pct': estimated_quota,
            'observed_token_delta': token_delta,
            'task_tokens': full_task_tokens,
            'token_coverage': coverage,
            'observation_count': len(observations),
            'start_ts': first.get('ts'),
            'end_ts': last.get('ts'),
        })

    return samples, {
        'min_quota_delta_pct': min_delta,
        'min_token_coverage': min_coverage,
        'completed_task_count': len(by_task),
        'observation_count': sum(len(items) for items in by_task.values()),
        'excluded_mixed_model_tasks': excluded_mixed,
        'excluded_cross_cycle_tasks': excluded_cross_cycle,
        'excluded_insufficient_tasks': excluded_insufficient,
        'excluded_low_coverage_tasks': excluded_low_coverage,
    }


def build_codex_quota_per_task(quota_observations, usage_events, finished_turn_ids=None,
                               min_quota_delta=0.5, min_token_coverage=0.25):
    """Estimate 5h quota burn per completed Codex task from local task boundaries."""
    samples, diagnostics = _build_codex_quota_task_samples(
        quota_observations,
        usage_events,
        finished_turn_ids,
        min_quota_delta=min_quota_delta,
        min_token_coverage=min_token_coverage,
    )

    grouped = {}
    for sample in samples:
        grouped.setdefault(sample['model_key'], []).append(sample)

    models = []
    for model_key, model_samples in grouped.items():
        estimated = _median_numeric(s['estimated_quota_delta_pct'] for s in model_samples)
        observed = _median_numeric(s['observed_quota_delta_pct'] for s in model_samples)
        tokens = _median_numeric(s['task_tokens'] for s in model_samples)
        coverage = _median_numeric(s['token_coverage'] for s in model_samples)
        if not estimated or estimated <= 0:
            continue
        sessions = {s['session'] for s in model_samples}
        quota_span = sum(float(s['observed_quota_delta_pct']) for s in model_samples)
        sample_count = len(model_samples)
        if sample_count >= 7 and len(sessions) >= 3 and quota_span >= 30 and (coverage or 0) >= 0.5:
            confidence = 'high'
        elif sample_count >= 3 and len(sessions) >= 2 and quota_span >= 10 and (coverage or 0) >= 0.35:
            confidence = 'medium'
        else:
            confidence = 'low'
        first_sample = model_samples[0]
        models.append({
            'model_key': model_key,
            'model_id': first_sample.get('model_id') or model_key,
            'reasoning_effort': first_sample.get('reasoning_effort') or 'medium',
            'estimated_quota_pct_per_task': round(estimated, 3),
            'observed_quota_pct_per_task': round(observed or 0.0, 3),
            'tokens_per_task': round(tokens or 0.0),
            'median_token_coverage': round(coverage or 0.0, 4),
            'task_count': sample_count,
            'session_count': len(sessions),
            'quota_span_pct': round(quota_span, 2),
            'confidence': confidence,
            'relative_task_quota_burn_vs_sol_high': None,
        })

    sol_high = next((row for row in models if row.get('model_key') == '5.6 sol high'), None)
    if sol_high and sol_high.get('estimated_quota_pct_per_task', 0) > 0:
        baseline = float(sol_high['estimated_quota_pct_per_task'])
        for row in models:
            value = float(row.get('estimated_quota_pct_per_task') or 0)
            if value > 0:
                row['relative_task_quota_burn_vs_sol_high'] = round(value / baseline, 3)

    models.sort(key=lambda row: (
        row.get('relative_task_quota_burn_vs_sol_high') is None,
        -(row.get('relative_task_quota_burn_vs_sol_high') or 0),
        row.get('model_key') or '',
    ))
    return {
        'available': bool(models),
        'method': 'empirical_completed_task_same_5h_cycle_extrapolated_median',
        'min_quota_delta_pct': diagnostics['min_quota_delta_pct'],
        'min_token_coverage': diagnostics['min_token_coverage'],
        'baseline_model_key': '5.6 sol high',
        'models': models,
        'task_sample_count': len(samples),
        **diagnostics,
        'note': (
            'Estimated from completed local Codex task boundaries. Quota between the first and last '
            'task snapshots is extrapolated to full task tokens; mixed-model and reset-crossing tasks '
            'are excluded. This is empirical, not an official OpenAI quota-weight formula.'
        ),
    }


def build_codex_quota_per_task_timeline(quota_observations, usage_events,
                                        finished_turn_ids=None, now=None,
                                        range_days=180,
                                        window_days_options=(7, 14, 30),
                                        min_tasks_per_model=5,
                                        min_quota_delta=0.5,
                                        min_token_coverage=0.25):
    """Build rolling completed-task quota medians against same-window Sol High."""
    now_utc = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    try:
        days = max(30, min(367, int(range_days)))
    except (TypeError, ValueError, OverflowError):
        days = 180
    try:
        minimum_tasks = max(2, min(50, int(min_tasks_per_model)))
    except (TypeError, ValueError, OverflowError):
        minimum_tasks = 5

    samples, diagnostics = _build_codex_quota_task_samples(
        quota_observations,
        usage_events,
        finished_turn_ids,
        min_quota_delta=min_quota_delta,
        min_token_coverage=min_token_coverage,
    )
    dated_samples = []
    for sample in samples:
        end_dt = _parse_iso_utc(sample.get('end_ts'))
        if end_dt is None or end_dt > now_utc:
            continue
        item = dict(sample)
        item['_end_dt'] = end_dt
        dated_samples.append(item)

    today_start = _local_day_start_utc(now_utc)
    day_starts = [today_start - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    date_keys = [day.astimezone().date().isoformat() for day in day_starts]
    baseline_key = '5.6 sol high'
    windows = {}

    for raw_window_days in window_days_options or (7, 14, 30):
        try:
            window_days = max(1, min(90, int(raw_window_days)))
        except (TypeError, ValueError, OverflowError):
            continue
        points_by_model = {}
        model_meta = {}
        for day_start, date_key in zip(day_starts, date_keys):
            day_end = min(day_start + timedelta(days=1), now_utc)
            if day_end <= day_start:
                continue
            window_start = day_end - timedelta(days=window_days)
            nearby = [
                sample for sample in dated_samples
                if window_start < sample['_end_dt'] <= day_end
            ]
            grouped = {}
            for sample in nearby:
                grouped.setdefault(sample.get('model_key'), []).append(sample)
            baseline_samples = grouped.get(baseline_key) or []
            if len(baseline_samples) < minimum_tasks:
                continue
            baseline_quota = _median_numeric(
                sample.get('estimated_quota_delta_pct') for sample in baseline_samples
            )
            if not baseline_quota or baseline_quota <= 0:
                continue

            for model_key, model_samples in grouped.items():
                if (not isinstance(model_key, str) or not model_key or
                        len(model_samples) < minimum_tasks):
                    continue
                estimated = _median_numeric(
                    sample.get('estimated_quota_delta_pct') for sample in model_samples
                )
                if not estimated or estimated <= 0:
                    continue
                observed = _median_numeric(
                    sample.get('observed_quota_delta_pct') for sample in model_samples
                )
                tokens = _median_numeric(sample.get('task_tokens') for sample in model_samples)
                coverage = _median_numeric(
                    sample.get('token_coverage') for sample in model_samples
                )
                sessions = {
                    sample.get('session') for sample in model_samples if sample.get('session')
                }
                baseline_sessions = {
                    sample.get('session') for sample in baseline_samples if sample.get('session')
                }
                if (len(model_samples) >= 10 and len(baseline_samples) >= 10 and
                        len(sessions) >= 3 and len(baseline_sessions) >= 3 and
                        (coverage or 0) >= 0.5):
                    confidence = 'high'
                elif (len(model_samples) >= minimum_tasks and
                      len(baseline_samples) >= minimum_tasks and
                      len(sessions) >= 2 and len(baseline_sessions) >= 2 and
                      (coverage or 0) >= 0.35):
                    confidence = 'medium'
                else:
                    confidence = 'low'
                last_sample = max(model_samples, key=lambda sample: sample['_end_dt'])
                model_meta.setdefault(model_key, {
                    'model_key': model_key,
                    'model_id': last_sample.get('model_id') or model_key,
                    'reasoning_effort': last_sample.get('reasoning_effort') or 'medium',
                })
                points_by_model.setdefault(model_key, []).append({
                    'date': date_key,
                    'estimated_quota_pct_per_task': round(estimated, 3),
                    'observed_quota_pct_per_task': round(observed or 0.0, 3),
                    'relative_task_quota_burn_vs_sol_high': round(estimated / baseline_quota, 3),
                    'tokens_per_task': round(tokens or 0.0),
                    'median_token_coverage': round(coverage or 0.0, 4),
                    'task_count': len(model_samples),
                    'baseline_task_count': len(baseline_samples),
                    'session_count': len(sessions),
                    'baseline_session_count': len(baseline_sessions),
                    'confidence': confidence,
                    'last_observed_at': last_sample.get('end_ts'),
                })

        model_rows = []
        for model_key, points in points_by_model.items():
            if not points:
                continue
            latest = points[-1]
            latest_dt = datetime.fromisoformat(latest['date'])
            previous_target = latest_dt - timedelta(days=window_days)
            previous = None
            for point in reversed(points[:-1]):
                if datetime.fromisoformat(point['date']) <= previous_target:
                    previous = point
                    break
            if previous is None and len(points) > 1:
                previous = points[-2]
            latest_value = float(latest.get('relative_task_quota_burn_vs_sol_high') or 0)
            previous_value = float(previous.get('relative_task_quota_burn_vs_sol_high') or 0) if previous else 0.0
            change_pct = None
            if latest_value > 0 and previous_value > 0:
                change_pct = round((latest_value / previous_value - 1.0) * 100.0, 1)
            row = dict(model_meta[model_key])
            row.update({
                'points': points,
                'latest': latest,
                'previous': previous,
                'change_pct': change_pct,
            })
            model_rows.append(row)
        model_rows.sort(key=lambda row: (
            -float((row.get('latest') or {}).get('relative_task_quota_burn_vs_sol_high') or 0),
            row.get('model_key') or '',
        ))
        windows[str(window_days)] = {
            'available': bool(model_rows),
            'window_days': window_days,
            'models': model_rows,
        }

    return {
        'available': any(value.get('available') for value in windows.values()),
        'method': 'daily_trailing_window_completed_task_same_period_sol_high_median',
        'range_days': days,
        'window_days_options': sorted(int(key) for key in windows),
        'min_tasks_per_model': minimum_tasks,
        'baseline_model_key': baseline_key,
        'dates': date_keys,
        'windows': windows,
        'task_sample_count': len(dated_samples),
        'generated_at': now_utc.isoformat(),
        **diagnostics,
        'note': (
            'Daily trailing-window medians from completed local Codex tasks. Each point requires '
            'the model and Sol High to meet the same-window task minimum; mixed-model, reset-crossing '
            'and low-coverage tasks are excluded. This is empirical, not an official OpenAI weight.'
        ),
    }


CODEX_TASK_CATEGORIES = (
    {'key': 'trading_setup', 'label': 'Học setup giao dịch'},
    {'key': 'web', 'label': 'Làm web'},
    {'key': 'simulation_3d', 'label': 'Mô phỏng 3D'},
    {'key': 'software_debugging', 'label': 'Sửa lỗi phần mềm'},
    {'key': 'research', 'label': 'Nghiên cứu & kiểm chứng'},
    {'key': 'documents', 'label': 'Xử lý tài liệu'},
    {'key': 'system_diagnostics', 'label': 'Chẩn đoán hệ thống'},
    {'key': 'other', 'label': 'Công việc khác'},
)
CODEX_TASK_CATEGORY_KEYS = {item['key'] for item in CODEX_TASK_CATEGORIES}
CODEX_MISSION_OUTCOME_OVERRIDES = {
    'accepted', 'inferred', 'unresolved', 'abandoned', 'user_repaired', 'excluded',
}


def _codex_plain_text(value):
    text = unicodedata.normalize('NFD', str(value or '').lower())
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    text = text.replace('đ', 'd')
    return re.sub(r'\s+', ' ', text).strip()


def _codex_timestamp_iso(value):
    if value is None or value == '':
        return ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 100000000000:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ''
    text = str(value).strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', text):
        try:
            return _codex_timestamp_iso(float(text))
        except ValueError:
            return ''
    parsed = _parse_iso_utc(text)
    return parsed.isoformat() if parsed is not None else text


def _codex_message_text(payload):
    if not isinstance(payload, dict):
        return ''
    content = payload.get('content')
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ''
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get('text')
            if isinstance(text, str) and text:
                parts.append(text)
    return '\n'.join(parts).strip()


def _is_codex_injected_user_context(text):
    clean = str(text or '').strip()
    if not clean:
        return True
    injected_prefixes = (
        '<environment_context>', '<recommended_plugins>', '# AGENTS.md instructions',
        '<skills_instructions>', '<permissions instructions>', '<collaboration_mode>',
        '<apps_instructions>', '<plugins_instructions>', '<app-context>',
        '<in-app-browser-context',
    )
    return any(clean.startswith(prefix) for prefix in injected_prefixes)


def _empty_codex_mission_turn(turn_id, timestamp=None):
    return {
        'turn_id': str(turn_id or ''),
        'root_turn_id': '',
        'thread_id': '',
        'thread_source': '',
        'parent_thread_id': '',
        'is_subagent': False,
        'started_at': str(timestamp or ''),
        'completed_at': '',
        'completed': False,
        'model_key': '',
        'model_id': '',
        'reasoning_effort': '',
        'service_tier': '',
        'cwd': '',
        'user_text': '',
        'user_messages': [],
        'user_chars': 0,
        'user_tokens_est': 0,
        'user_message_count': 0,
        'assistant_message_count': 0,
        'assistant_text': '',
        'tool_call_count': 0,
        'tool_success_count': 0,
        'tool_failure_count': 0,
        'task_error': '',
    }


def _codex_delegation_source_thread_id(text):
    """Return the parent thread recorded by a Codex delegation envelope."""
    match = re.search(
        r'<codex_delegation>.*?<source_thread_id>\s*([A-Za-z0-9_-]{1,128})\s*</source_thread_id>',
        str(text or ''),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ''


def _expand_codex_mission_turn(raw_turn):
    """Split one outer Codex task into stable logical turns per human message."""
    if not isinstance(raw_turn, dict):
        return []
    outer = dict(raw_turn)
    outer_turn_id = str(outer.get('turn_id') or '')
    if not outer_turn_id:
        return []
    raw_messages = outer.get('user_messages') if isinstance(outer.get('user_messages'), list) else []
    messages = [item for item in raw_messages if isinstance(item, dict) and str(item.get('text') or '').strip()]
    if not messages:
        text = str(outer.get('user_text') or '').strip()
        if not text:
            return []
        messages = [{
            'text': text,
            'started_at': outer.get('started_at') or '',
            'user_chars': outer.get('user_chars') or len(text),
            'user_tokens_est': outer.get('user_tokens_est') or estimate_tokens(text),
            'assistant_message_count': outer.get('assistant_message_count') or 0,
            'assistant_text': outer.get('assistant_text') or '',
            'tool_call_count': outer.get('tool_call_count') or 0,
            'tool_success_count': outer.get('tool_success_count') or 0,
            'tool_failure_count': outer.get('tool_failure_count') or 0,
            'task_error': outer.get('task_error') or '',
            'model_key': outer.get('model_key') or '',
            'model_id': outer.get('model_id') or '',
            'reasoning_effort': outer.get('reasoning_effort') or '',
            'cwd': outer.get('cwd') or '',
        }]

    logical_turns = []
    for index, message in enumerate(messages):
        text = str(message.get('text') or '').strip()
        started_at = _codex_timestamp_iso(message.get('started_at') or outer.get('started_at'))
        next_started_at = ''
        if index + 1 < len(messages):
            next_started_at = _codex_timestamp_iso(messages[index + 1].get('started_at'))
        logical_id = outer_turn_id if index == 0 else f'{outer_turn_id}__u{index + 1}'
        logical = dict(outer)
        logical.update({
            'turn_id': logical_id,
            'outer_turn_id': outer_turn_id,
            'usage_task_id': outer_turn_id,
            'logical_user_index': index,
            'started_at': started_at,
            'completed_at': next_started_at or _codex_timestamp_iso(outer.get('completed_at')),
            'completed': bool(next_started_at) or bool(outer.get('completed')),
            'model_key': str(message.get('model_key') or outer.get('model_key') or ''),
            'model_id': str(message.get('model_id') or outer.get('model_id') or ''),
            'reasoning_effort': str(message.get('reasoning_effort') or outer.get('reasoning_effort') or ''),
            'cwd': str(message.get('cwd') or outer.get('cwd') or ''),
            'user_text': text,
            'user_chars': _safe_nonnegative_int(message.get('user_chars')) or len(text),
            'user_tokens_est': _safe_nonnegative_int(message.get('user_tokens_est')) or estimate_tokens(text),
            'user_message_count': 1,
            'assistant_message_count': _safe_nonnegative_int(message.get('assistant_message_count')),
            'assistant_text': str(message.get('assistant_text') or outer.get('assistant_text') or '')[-4000:],
            'tool_call_count': _safe_nonnegative_int(message.get('tool_call_count')),
            'tool_success_count': _safe_nonnegative_int(message.get('tool_success_count')),
            'tool_failure_count': _safe_nonnegative_int(message.get('tool_failure_count')),
            'task_error': str(message.get('task_error') or outer.get('task_error') or '')[-2000:],
        })
        delegation_parent = _codex_delegation_source_thread_id(text)
        if delegation_parent:
            logical['parent_thread_id'] = delegation_parent
            logical['thread_source'] = 'subagent'
            logical['is_subagent'] = True
        logical.pop('user_messages', None)
        logical_turns.append(logical)
    return logical_turns


def _valid_codex_mission_turns_cache_entry(entry):
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get('turns'), dict):
        return False
    offset = entry.get('offset')
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return False
    return all(isinstance(turn_id, str) and isinstance(turn, dict)
               for turn_id, turn in entry['turns'].items())


def _codex_tool_output_failed(value):
    """Classify only explicit tool failures; unknown output remains successful evidence."""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    else:
        text = str(value or '')
    clean = _codex_plain_text(text)
    if not clean:
        return False
    if re.search(r'process exited with code\s+[1-9]\d*', clean):
        return True
    if re.search(r'exit[_ ]code[\\\"\']*\s*[:=]\s*[1-9]\d*', clean):
        return True
    return any(marker in clean for marker in (
        '"iserror": true', '"status": "failed"', 'blocked by policy',
        'command rejected', 'tool call failed', 'script failed',
    ))


def _codex_mission_scan_roots(sessions_dir=None):
    default_scan = sessions_dir is None
    target = sessions_dir if sessions_dir is not None else (
        globals().get('CODEX_SESSIONS_DIR') or CODEX_SESSIONS_DIR
    )
    roots = []
    if target and os.path.exists(target):
        roots.append(target)
    if default_scan and roots and os.path.basename(os.path.normpath(target)).lower() == 'sessions':
        archived = os.path.join(os.path.dirname(os.path.normpath(target)), 'archived_sessions')
        if os.path.isdir(archived):
            roots.append(archived)
    return roots, default_scan


def scan_codex_mission_turns(sessions_dir=None, cache_file=None, now=None,
                             max_files=500, max_dirs=1200, max_entries=25000,
                             max_file_size=500*1024*1024, max_total_bytes=4*1024*1024*1024,
                             max_line_bytes=10*1024*1024):
    """Incrementally index user-visible Codex turns without duplicating token accounting."""
    roots, default_scan = _codex_mission_scan_roots(sessions_dir)
    now_utc = now if isinstance(now, datetime) else (_parse_iso_utc(now) if now else datetime.now(timezone.utc))
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    if not roots:
        return {
            'available': False, 'turns': [],
            'diagnostics': {'files_discovered': 0, 'files_cached': 0, 'files_updated': 0,
                            'bytes_read': 0, 'coverage_complete': True,
                            'error': 'Codex session logs directory not found'},
        }

    cache_path = cache_file if cache_file is not None else (
        globals().get('CODEX_MISSION_TURNS_CACHE_FILE') or CODEX_MISSION_TURNS_CACHE_FILE
    )
    cache_store = _read_json_file(cache_path, {'version': CODEX_MISSION_TURNS_CACHE_VERSION, 'files': {}})
    if (not isinstance(cache_store, dict) or cache_store.get('version') != CODEX_MISSION_TURNS_CACHE_VERSION or
            not isinstance(cache_store.get('files'), dict)):
        cache_store = {'version': CODEX_MISSION_TURNS_CACHE_VERSION, 'files': {}}
    else:
        cache_store['version'] = CODEX_MISSION_TURNS_CACHE_VERSION
    cache_files = cache_store['files']

    candidates_by_key = {}
    visited_dirs = 0
    visited_entries = 0
    discovered = 0
    discovery_truncated = False
    for scan_root in roots:
        real_root = os.path.realpath(scan_root)
        try:
            walker = os.walk(scan_root, followlinks=False) if os.path.isdir(scan_root) else []
            for root, _, files in walker:
                visited_dirs += 1
                if visited_dirs > max_dirs:
                    discovery_truncated = True
                    break
                for name in files:
                    visited_entries += 1
                    if visited_entries > max_entries:
                        discovery_truncated = True
                        break
                    if not name.endswith('.jsonl'):
                        continue
                    path = os.path.join(root, name)
                    try:
                        if os.path.islink(path) or is_cloud_offline_file(path):
                            continue
                        real_path = os.path.realpath(path)
                        if os.path.commonpath([real_root, real_path]) != real_root:
                            continue
                        size = os.path.getsize(path)
                        mtime = os.path.getmtime(path)
                    except Exception:
                        continue
                    discovered += 1
                    if size > max_file_size:
                        discovery_truncated = True
                        continue
                    key = os.path.basename(path) if default_scan else os.path.relpath(path, scan_root)
                    candidate = (mtime, size, path, key)
                    existing = candidates_by_key.get(key)
                    if existing is None or candidate[:2] > existing[:2]:
                        candidates_by_key[key] = candidate
                if discovery_truncated:
                    break
        except Exception:
            discovery_truncated = True
        if discovery_truncated:
            break

    candidates = sorted(candidates_by_key.values(), reverse=True)
    if len(candidates) > max_files:
        candidates = candidates[:max_files]
        discovery_truncated = True

    files_cached = 0
    files_updated = 0
    files_rebuilt = 0
    bytes_read = 0
    cache_modified = False
    active_keys = set()

    for mtime, size, file_path, key in candidates:
        if bytes_read >= max_total_bytes:
            discovery_truncated = True
            break
        active_keys.add(key)
        cached = cache_files.get(key)
        if (_valid_codex_mission_turns_cache_entry(cached) and
                cached.get('attribution_version') == 3 and
                cached.get('mtime') == mtime and cached.get('size') == size and
                cached.get('offset') == size):
            files_cached += 1
            continue

        append_mode = (
            _valid_codex_mission_turns_cache_entry(cached) and
            cached.get('attribution_version') == 3 and
            cached.get('offset', 0) <= size and cached.get('size', 0) <= size
        )
        if append_mode:
            start_offset = cached.get('offset', 0)
            turns = {str(k): dict(v) for k, v in cached.get('turns', {}).items()}
            session_id = str(cached.get('session_id') or '')
            thread_source = str(cached.get('thread_source') or '')
            parent_thread_id = str(cached.get('parent_thread_id') or '')
            current_task_id = str(cached.get('current_task_id') or '')
            current_model = cached.get('current_model')
            current_effort = cached.get('current_effort')
            pending_service_tier = cached.get('pending_service_tier') or 'default'
            current_service_tier = cached.get('current_service_tier') or pending_service_tier
            current_cwd = str(cached.get('current_cwd') or '')
        else:
            start_offset = 0
            turns = {}
            session_id = ''
            thread_source = ''
            parent_thread_id = ''
            current_task_id = ''
            current_model = None
            current_effort = None
            pending_service_tier = 'default'
            current_service_tier = 'default'
            current_cwd = ''
            files_rebuilt += 1

        def ensure_turn(turn_id, timestamp=None):
            nonlocal turns
            turn_key = str(turn_id or '')
            if not turn_key:
                return None
            turn = turns.get(turn_key)
            if not isinstance(turn, dict):
                turn = _empty_codex_mission_turn(turn_key, timestamp)
                turns[turn_key] = turn
            elif timestamp and not turn.get('started_at'):
                turn['started_at'] = str(timestamp)
            if session_id:
                turn['thread_id'] = session_id
            turn['thread_source'] = thread_source
            turn['parent_thread_id'] = parent_thread_id
            turn['is_subagent'] = bool(thread_source == 'subagent' or parent_thread_id)
            return turn

        last_complete_offset = start_offset
        try:
            with open(file_path, 'rb') as handle:
                handle.seek(start_offset)
                while True:
                    if bytes_read >= max_total_bytes:
                        discovery_truncated = True
                        break
                    line_start = handle.tell()
                    raw = handle.readline(max_line_bytes + 1)
                    if not raw:
                        break
                    bytes_read += len(raw)
                    if len(raw) > max_line_bytes:
                        if not raw.endswith((b'\n', b'\r')):
                            while raw and not raw.endswith((b'\n', b'\r')):
                                raw = handle.readline(65536)
                                bytes_read += len(raw)
                        last_complete_offset = handle.tell()
                        continue
                    if not raw.endswith((b'\n', b'\r')) and handle.tell() >= size:
                        handle.seek(line_start)
                        break
                    last_complete_offset = handle.tell()
                    if (b'session_meta' not in raw and b'task_started' not in raw and
                            b'task_complete' not in raw and b'turn_context' not in raw and
                            b'response_item' not in raw and b'thread_settings_applied' not in raw):
                        continue
                    try:
                        item = json.loads(raw.decode('utf-8', errors='ignore').strip())
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get('type')
                    payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
                    payload_type = payload.get('type')
                    timestamp = item.get('timestamp') or payload.get('timestamp') or ''

                    if item_type == 'session_meta':
                        session_id = str(payload.get('id') or payload.get('session_id') or session_id or key)
                        thread_source = str(payload.get('thread_source') or thread_source or '')
                        source_meta = payload.get('source') if isinstance(payload.get('source'), dict) else {}
                        subagent_meta = source_meta.get('subagent') if isinstance(source_meta.get('subagent'), dict) else {}
                        spawn_meta = subagent_meta.get('thread_spawn') if isinstance(subagent_meta.get('thread_spawn'), dict) else {}
                        parent_thread_id = str(spawn_meta.get('parent_thread_id') or parent_thread_id or '')
                        current_cwd = str(payload.get('cwd') or current_cwd or '')
                        for turn in turns.values():
                            turn['thread_id'] = session_id
                            turn['thread_source'] = thread_source
                            turn['parent_thread_id'] = parent_thread_id
                            turn['is_subagent'] = bool(thread_source == 'subagent' or parent_thread_id)
                            if current_cwd and not turn.get('cwd'):
                                turn['cwd'] = current_cwd
                        continue

                    if item_type == 'event_msg' and payload_type == 'thread_settings_applied':
                        settings = payload.get('thread_settings')
                        if isinstance(settings, dict):
                            pending_service_tier = _codex_service_tier(settings.get('service_tier')) or pending_service_tier
                        continue

                    if item_type == 'event_msg' and payload_type == 'task_started':
                        current_task_id = str(payload.get('turn_id') or '')
                        current_service_tier = pending_service_tier
                        turn = ensure_turn(current_task_id, _codex_timestamp_iso(payload.get('started_at') or timestamp))
                        if turn and current_cwd and not turn.get('cwd'):
                            turn['cwd'] = current_cwd
                        if turn:
                            turn['service_tier'] = current_service_tier
                        continue

                    if item_type == 'turn_context' or payload_type == 'turn_context':
                        turn_id = str(payload.get('turn_id') or current_task_id or '')
                        if turn_id and turn_id != current_task_id:
                            current_service_tier = pending_service_tier
                        if turn_id:
                            current_task_id = turn_id
                        context_tier = _codex_service_tier(payload.get('service_tier'))
                        if context_tier:
                            current_service_tier = context_tier
                        current_cwd = str(payload.get('cwd') or current_cwd or '')
                        raw_model = payload.get('model') or item.get('model')
                        raw_effort = (payload.get('effort') or payload.get('reasoning_effort') or
                                      item.get('effort') or item.get('reasoning_effort'))
                        if raw_model:
                            _, current_model, current_effort, _ = normalize_codex_model_effort(raw_model, raw_effort)
                        turn = ensure_turn(current_task_id, timestamp)
                        if turn:
                            turn['root_turn_id'] = str(payload.get('root_turn_id') or turn.get('root_turn_id') or '')
                            turn['cwd'] = current_cwd or turn.get('cwd') or ''
                            if current_model:
                                compound, canon, effort, _ = normalize_codex_model_effort(
                                    current_model, current_effort, current_service_tier
                                )
                                turn['model_key'] = compound
                                turn['model_id'] = canon
                                turn['reasoning_effort'] = effort
                                turn['service_tier'] = current_service_tier
                        continue

                    if item_type == 'event_msg' and payload_type == 'task_complete':
                        turn_id = str(payload.get('turn_id') or current_task_id or '')
                        turn = ensure_turn(turn_id, timestamp)
                        if turn:
                            turn['completed'] = not bool(payload.get('error'))
                            turn['completed_at'] = _codex_timestamp_iso(payload.get('completed_at') or timestamp)
                            if payload.get('error'):
                                error = payload.get('error')
                                if isinstance(error, dict):
                                    error = error.get('message') or error.get('additionalDetails') or error
                                turn['task_error'] = str(error or '')[-2000:]
                                messages = turn.get('user_messages')
                                if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
                                    messages[-1]['task_error'] = turn['task_error']
                        if turn_id == current_task_id:
                            current_task_id = ''
                        continue

                    if (item_type == 'response_item' and
                            payload_type in ('function_call', 'custom_tool_call')):
                        metadata = payload.get('internal_chat_message_metadata_passthrough')
                        metadata = metadata if isinstance(metadata, dict) else {}
                        turn_id = str(metadata.get('turn_id') or current_task_id or '')
                        turn = ensure_turn(turn_id, timestamp)
                        if turn:
                            turn['tool_call_count'] = _safe_nonnegative_int(turn.get('tool_call_count')) + 1
                            messages = turn.get('user_messages')
                            if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
                                messages[-1]['tool_call_count'] = (
                                    _safe_nonnegative_int(messages[-1].get('tool_call_count')) + 1
                                )
                        continue

                    if (item_type == 'response_item' and
                            payload_type in ('function_call_output', 'custom_tool_call_output')):
                        metadata = payload.get('internal_chat_message_metadata_passthrough')
                        metadata = metadata if isinstance(metadata, dict) else {}
                        turn_id = str(metadata.get('turn_id') or current_task_id or '')
                        turn = ensure_turn(turn_id, timestamp)
                        if turn:
                            failed = _codex_tool_output_failed(payload.get('output'))
                            field = 'tool_failure_count' if failed else 'tool_success_count'
                            turn[field] = _safe_nonnegative_int(turn.get(field)) + 1
                            messages = turn.get('user_messages')
                            if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
                                messages[-1][field] = _safe_nonnegative_int(messages[-1].get(field)) + 1
                        continue

                    if item_type == 'response_item' and payload_type == 'message':
                        role = str(payload.get('role') or '').lower()
                        turn = ensure_turn(current_task_id, timestamp)
                        if not turn:
                            continue
                        if role == 'user':
                            message = _codex_message_text(payload)
                            if _is_codex_injected_user_context(message):
                                continue
                            if message:
                                existing = str(turn.get('user_text') or '')
                                turn['user_text'] = ((existing + '\n' + message).strip())[:1600]
                                turn['user_chars'] = _safe_nonnegative_int(turn.get('user_chars')) + len(message)
                                turn['user_tokens_est'] = _safe_nonnegative_int(turn.get('user_tokens_est')) + estimate_tokens(message)
                                turn['user_message_count'] = _safe_nonnegative_int(turn.get('user_message_count')) + 1
                                messages = turn.setdefault('user_messages', [])
                                if isinstance(messages, list):
                                    messages.append({
                                        'text': message[:1600],
                                        'started_at': _codex_timestamp_iso(timestamp),
                                        'user_chars': len(message),
                                        'user_tokens_est': estimate_tokens(message),
                                        'assistant_message_count': 0,
                                        'tool_call_count': 0,
                                        'tool_success_count': 0,
                                        'tool_failure_count': 0,
                                        'task_error': '',
                                        'model_key': str(turn.get('model_key') or ''),
                                        'model_id': str(turn.get('model_id') or ''),
                                        'reasoning_effort': str(turn.get('reasoning_effort') or ''),
                                        'cwd': str(turn.get('cwd') or current_cwd or ''),
                                    })
                        elif role == 'assistant':
                            turn['assistant_message_count'] = _safe_nonnegative_int(turn.get('assistant_message_count')) + 1
                            assistant_text = _codex_message_text(payload)
                            if assistant_text:
                                # Keep only the final user-visible conclusion for each
                                # logical request.  The classifier needs completion and
                                # caveat evidence, not the model's full progress stream.
                                turn['assistant_text'] = assistant_text[-4000:]
                            messages = turn.get('user_messages')
                            if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
                                messages[-1]['assistant_message_count'] = (
                                    _safe_nonnegative_int(messages[-1].get('assistant_message_count')) + 1
                                )
                                if assistant_text:
                                    messages[-1]['assistant_text'] = assistant_text[-4000:]
        except Exception:
            discovery_truncated = True
            continue

        cache_files[key] = {
            'mtime': mtime, 'size': size, 'offset': last_complete_offset,
            'attribution_version': 3,
            'session_id': session_id or key,
            'thread_source': thread_source,
            'parent_thread_id': parent_thread_id,
            'current_task_id': current_task_id,
            'current_model': current_model, 'current_effort': current_effort,
            'pending_service_tier': pending_service_tier,
            'current_service_tier': current_service_tier,
            'current_cwd': current_cwd, 'turns': turns,
        }
        files_updated += 1
        cache_modified = True

    if cache_modified:
        _atomic_write_json(cache_path, cache_store)

    merged_outer_turns = {}
    for file_key, entry in cache_files.items():
        if not _valid_codex_mission_turns_cache_entry(entry):
            continue
        fallback_thread = str(entry.get('session_id') or file_key)
        for turn_id, raw_turn in entry.get('turns', {}).items():
            turn = dict(raw_turn)
            turn['turn_id'] = str(turn.get('turn_id') or turn_id)
            turn['thread_id'] = str(turn.get('thread_id') or fallback_thread)
            turn['started_at'] = _codex_timestamp_iso(turn.get('started_at'))
            turn['completed_at'] = _codex_timestamp_iso(turn.get('completed_at'))
            turn['source_file'] = file_key
            prior = merged_outer_turns.get(turn['turn_id'])
            if prior is None or (
                _safe_nonnegative_int(turn.get('user_chars')),
                str(turn.get('completed_at') or turn.get('started_at') or '')
            ) > (
                _safe_nonnegative_int(prior.get('user_chars')),
                str(prior.get('completed_at') or prior.get('started_at') or '')
            ):
                merged_outer_turns[turn['turn_id']] = turn

    turns = []
    for outer_turn in merged_outer_turns.values():
        turns.extend(_expand_codex_mission_turn(outer_turn))
    turns.sort(key=lambda turn: (str(turn.get('started_at') or ''), turn.get('turn_id') or ''))
    return {
        'available': bool(turns),
        'turns': turns,
        'diagnostics': {
            'files_discovered': discovered,
            'files_in_cache': len(cache_files),
            'files_cached': files_cached,
            'files_updated': files_updated,
            'files_rebuilt': files_rebuilt,
            'bytes_read': bytes_read,
            'coverage_complete': not discovery_truncated,
            'roots_scanned': [os.path.realpath(root) for root in roots],
            'active_files': len(active_keys),
            'retained_historical_files': max(0, len(cache_files) - len(active_keys)),
            'observed_at': now_utc.isoformat(),
        },
    }


def load_codex_mission_reviews(reviews_file=None):
    path = reviews_file if reviews_file is not None else (
        globals().get('CODEX_MISSION_REVIEWS_FILE') or CODEX_MISSION_REVIEWS_FILE
    )
    data = _read_json_file(path, {'version': 1, 'turn_boundaries': {}, 'missions': {}})
    if not isinstance(data, dict) or data.get('version') != 1:
        data = {'version': 1, 'turn_boundaries': {}, 'missions': {}}
    if not isinstance(data.get('turn_boundaries'), dict):
        data['turn_boundaries'] = {}
    if not isinstance(data.get('missions'), dict):
        data['missions'] = {}
    return data


def update_codex_mission_review(body, reviews_file=None):
    if not isinstance(body, dict):
        raise ValueError('Invalid mission review payload')
    action = str(body.get('action') or 'review')
    anchor = str(body.get('anchor_turn_id') or '')
    turn_id = str(body.get('turn_id') or '')
    safe_id = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
    if anchor and not safe_id.fullmatch(anchor):
        raise ValueError('Invalid mission anchor')
    if turn_id and not safe_id.fullmatch(turn_id):
        raise ValueError('Invalid turn id')

    path = reviews_file if reviews_file is not None else (
        globals().get('CODEX_MISSION_REVIEWS_FILE') or CODEX_MISSION_REVIEWS_FILE
    )
    data = load_codex_mission_reviews(path)
    boundaries = data['turn_boundaries']
    missions = data['missions']

    if action == 'merge_previous':
        if not anchor:
            raise ValueError('Missing mission anchor')
        boundaries[anchor] = 'continue'
    elif action == 'split_last':
        if not turn_id:
            raise ValueError('Missing split turn id')
        boundaries[turn_id] = 'new'
    elif action == 'reset_boundary':
        target = turn_id or anchor
        if not target:
            raise ValueError('Missing boundary turn id')
        boundaries.pop(target, None)
    elif action == 'review':
        if not anchor:
            raise ValueError('Missing mission anchor')
        review = missions.get(anchor)
        if not isinstance(review, dict):
            review = {}
        if 'categories' in body:
            raw_categories = body.get('categories')
            if raw_categories in (None, 'auto'):
                raw_categories = []
            if not isinstance(raw_categories, list):
                raise ValueError('Invalid task categories')
            categories = []
            for raw_category in raw_categories:
                category = str(raw_category or '').strip()
                if not category or category == 'auto':
                    continue
                if category not in CODEX_TASK_CATEGORY_KEYS:
                    raise ValueError('Invalid task category')
                if category not in categories:
                    categories.append(category)
            if categories:
                review['categories'] = categories
                # Preserve the legacy scalar field for old review files/UI code.
                review['category'] = categories[0]
            else:
                review.pop('categories', None)
                review.pop('category', None)
        if 'category' in body:
            category = str(body.get('category') or 'auto')
            if category == 'auto':
                review.pop('category', None)
                if 'categories' not in body:
                    review.pop('categories', None)
            elif category in CODEX_TASK_CATEGORY_KEYS:
                review['category'] = category
                if 'categories' not in body:
                    review['categories'] = [category]
            else:
                raise ValueError('Invalid task category')
        if 'outcome' in body:
            outcome = str(body.get('outcome') or 'auto')
            if outcome == 'auto':
                review.pop('outcome', None)
            elif outcome in CODEX_MISSION_OUTCOME_OVERRIDES:
                review['outcome'] = outcome
            else:
                raise ValueError('Invalid mission outcome')
        if 'repair_of_anchor_turn_id' in body:
            repair_anchor = body.get('repair_of_anchor_turn_id')
            if repair_anchor == 'auto':
                review.pop('repair_of_anchor_turn_id', None)
            elif repair_anchor in (None, '', 'none'):
                # Keep an explicit null so the user can suppress an incorrect
                # automatic repair link without changing mission boundaries.
                review['repair_of_anchor_turn_id'] = None
            else:
                repair_anchor = str(repair_anchor)
                if not safe_id.fullmatch(repair_anchor) or repair_anchor == anchor:
                    raise ValueError('Invalid repair mission anchor')
                review['repair_of_anchor_turn_id'] = repair_anchor
        review['updated_at'] = datetime.now(timezone.utc).isoformat()
        if set(review) == {'updated_at'}:
            missions.pop(anchor, None)
        else:
            missions[anchor] = review
    else:
        raise ValueError('Unsupported mission review action')

    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(path, data)
    return data


def _codex_task_categories(text, cwd=''):
    prompt_text = _codex_plain_text(text)
    path_text = _codex_plain_text(cwd).replace('\\', '/')
    scores = {}

    def contains(token):
        return re.search(
            rf'(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])',
            prompt_text,
        ) is not None

    def contains_any(tokens):
        return any(contains(token) for token in tokens)

    def add(category, score):
        scores[category] = max(scores.get(category, 0), score)

    if any(token in path_text for token in ('trading', 'backtest', 'forex', 'market-replay', 'gbpusd')):
        add('trading_setup', 3)
    if contains_any(('trading setup', 'setup giao dich', 'giao dich', 'backtest',
                     'market replay', 'stop loss', 'take profit', 'risk management',
                     'quan ly rui ro', 'out of sample', 'oos', 'lenh giao dich',
                     'gbpusd',
                     'tp2', 'tp3', 'entry pullback', 'trailing sl', 'lookback',
                     'rpnl', 'plateau', 'fvg', 'bos', 'giai lenh', 'lenh nay',
                     'dinh nay', 'day nay', 'chon tp', 'filter 2 cot', 'filter 3 cot')):
        add('trading_setup', 3)

    # SU-30 is a subject domain, not evidence that the task is a 3D simulation.
    # Treat only the dedicated cockpit workspace or explicit 3D language as 3D;
    # otherwise OCR/Word/PDF work under an SU-30 folder is a document task.
    if 'cockpit360' in path_text:
        add('simulation_3d', 3)
    if contains_any(('mo phong 3d', 'cockpit360', 'three.js', 'threejs',
                     'viewport 3d', 'hotspot 3d', 'buong lai 3d',
                     'buong lai tuong tac', 'panel 31', 'panel 18')):
        add('simulation_3d', 3)

    # A workspace path is useful context, but it must not hide another explicit
    # function in the prompt (for example document repair + permission diagnosis).
    if 'usage-tracker' in path_text:
        add('web', 2)
    if contains('usage tracker') or contains_any(
        ('lam web', 'website', 'dashboard', 'frontend', 'giao dien web',
         'html', 'css', 'javascript', 'bieu do', 'bang xep hang model')
    ):
        add('web', 3)

    document_terms = (
        'docx', 'microsoft word', 'file word', 'pdf', 'ocr', 'unicode', 'vni', 'bien ban',
        'tai lieu', 'spreadsheet', 'excel', 'slide', 'ho so', 'ly lich', 'que quan',
        'cap hoc', 'dong chi', 'nhan su',
        'su30', 'su-30', 'su 30', 'ho so dang vien', 'ly lich dang vien',
        'bien dich', 'chuyen doi font', 'loi font',
    )
    has_education_level = (
        'trình độ' in unicodedata.normalize('NFC', str(text or '').lower()) or
        re.search(r'(?<!chuong )(?<!chương )\btrinh do\b', prompt_text) is not None
    )
    if contains_any(document_terms) or has_education_level:
        add('documents', 3)

    if contains_any((
        'wifi', 'o dia', 'chkdsk', 'windows', 'listener', 'cong 5050', 'server khong chay',
        'mat ket noi', 'chan doan he thong', 'rate limit khong cap nhat', 'quyen doc',
        'quyen ghi', 'chan quyen', 'permission denied', 'permission', 'filesystem',
        'sandbox', 'command rejected', 'khong doc duoc', 'khong ghi duoc',
        'usb', 'adb', 'xiaomi', 'sac nhanh', 'man hinh den', 'gpu', 'quat tan nhiet',
        'o cung', 'o dia c', 'disk cleanup', 'wireless debug', 'android',
        'cache chrome', 'cache capcut', 'dung luong dia', 'giai phong dung luong',
    )):
        add('system_diagnostics', 3)

    if contains_any(('gpt 6 astra', 'model astra', 'suy luan',
                     'reasoning effort', 'model reasoning')):
        add('research', 3)
    if contains_any(('nghien cuu', 'tra cuu', 'kiem chung', 'xac minh nguon',
                     'so sanh model', 'danh gia model', 'bai viet tren x',
                     'artificial analysis', 'han muc', 'gia token', 'cache hit',
                     'chatgpt plus', 'codex free', 'provider', 'context window',
                     'reasoning effort', 'model nao', 'codex auto review',
                     'cua so ngu canh', 'do dai ngu canh', 'token duoc ko',
                     'computer use', 'lam ngam')):
        add('research', 2)

    if contains_any(('github', 'git la sao', 'git repo')):
        add('software_debugging', 3)
    if contains_any((
        'sua loi phan mem', 'loi phan mem', 'bug', 'refactor', 'unit test', 'test suite',
        'ma nguon', 'lap trinh', 'stack trace', 'exception', 'codex web gpt',
        'chatgpt web', 'stream disconnected', 'launcher', 'runtime', 'bridge',
        'connector', 'compaction', 'remote compact', 'context handoff', 'turn id',
    )):
        add('software_debugging', 2)
    if contains('python') and contains_any((
        'script', 'chuong trinh', 'ma nguon', 'lap trinh', 'sua code', 'chay code',
    )):
        add('software_debugging', 2)
    if contains('repository') and contains_any(('source', 'code', 'ma nguon', 'git')):
        add('software_debugging', 2)
    if (contains_any((
            'gioi han context', 'context 80k', 'context 160k', 'context 200k',
            'context 240k', 'context 80 k', 'context 240 k',
        )) and contains_any((
            'chinh sua', 'chinh lai', 'cap nhat', 'cai dat', 'file',
        ))):
        # Editing Codex context configuration is maintenance of the local
        # software/runtime setup, rather than research about model context.
        add('software_debugging', 3)

    # Prompts that design Usage Tracker's own classifier often quote task-type
    # examples. Those examples are training material for the web feature, not
    # simultaneous document/diagnostic/3D work in the current mission.
    tracker_classifier_work = (
        contains('usage tracker') and
        contains_any((
            'phan loai', 'loai cong viec', 'kiem tra cac nhiem vu da ghep',
            'ma tran tong token', 'ma tran han muc', 'soft audit',
        ))
    )
    if tracker_classifier_work:
        scores = {'web': 3}

    # Questions about the tracker/model numbers themselves are analysis work.
    # Do not let the Usage Tracker workspace path turn them into web-development
    # samples; implementation follow-ups are classified independently per turn.
    if _codex_is_quota_accounting_question(prompt_text):
        scores = {'research': 3}

    if not scores:
        return ['other'], 'other', 'low', False

    category_order = {item['key']: index for index, item in enumerate(CODEX_TASK_CATEGORIES)}
    ordered = sorted(scores, key=lambda key: (-scores[key], category_order.get(key, 999)))
    strongest = scores[ordered[0]]
    categories = [key for key in ordered if scores[key] >= max(2, strongest - 1)]
    primary = ordered[0]
    confidence = 'high' if strongest >= 3 else 'medium'
    ambiguous = len([key for key in categories if scores[key] == strongest]) > 1
    if ambiguous:
        confidence = 'medium'
    return categories, primary, confidence, ambiguous


def _codex_task_category(text, cwd=''):
    _, primary, confidence, _ = _codex_task_categories(text, cwd)
    return primary, confidence


def _codex_prompt_flags(text):
    clean = _codex_plain_text(text)
    acceptance = (
        'ok', 'oke', 'okay', 'tot roi', 'dung roi', 'on roi', 'duoc roi', 'xong roi',
        'o duoc roi', 'hay qua', 'chap nhan roi', 'cam on', 'tuyet voi',
    )
    work = (
        'lam di', 'lam tiep', 'bat dau', 'nhiem vu', 'tiep theo', 'sua', 'them', 'bo sung',
        'cap nhat', 'kiem tra', 'xay', 'tao', 'trien khai', 'toi muon', 'giup toi', 'hoi la',
    )
    followup = (
        'lam tiep', 'tiep tuc', 'ban lam di', 'sao ban dung', 'chua xong', 'chua dung',
        'khong dung', 'sai', 'sua lai', 'van chua', 'van bi', 'them nua', 'dong thoi',
        'bo sung', 'con cai', 'cho nay', 'cai nay', 'vua roi', 'toi hoi cai', 'y toi la',
        'giai thich lai', 'giang lai', 'vay gio', 'luc nay', 'con may', 'the ban',
        'the thi', 'sua di', 'ban sua', 'sao lai', 'sao no lai', 'kiem tra lai',
    )
    new_work = (
        'nhiem vu tiep theo', 'y tuong tiep theo', 'toi co them y tuong', 'gio toi muon',
        'toi muon lam', 'toi muon them', 'them tinh nang', 'xay them',
    )
    starts_acceptance = any(clean == phrase or clean.startswith(phrase + ' ') or clean.startswith(phrase + ',')
                            for phrase in acceptance)
    has_work = any(phrase in clean for phrase in work)
    vague_continuation = _codex_is_vague_continuation_prompt(clean)
    is_new_work = any(phrase in clean for phrase in new_work)
    is_followup = (
        any(phrase in clean for phrase in followup) or
        vague_continuation or
        (starts_acceptance and has_work and not is_new_work)
    )
    acceptance_only = starts_acceptance and not has_work and len(clean) <= 220
    acceptance_plus_work = starts_acceptance and has_work and is_new_work
    return {
        'acceptance_only': acceptance_only,
        'acceptance_plus_work': acceptance_plus_work,
        'followup': is_followup,
        'new_work': is_new_work,
        'manual_user_repair': _codex_is_manual_user_repair_signal(clean),
        'informational_question': _codex_is_informational_question(clean),
    }


def _codex_is_manual_user_repair_signal(text):
    """Detect only explicit claims that the user finished the failed work by hand."""
    clean = _codex_plain_text(text)
    return any(phrase in clean for phrase in (
        'toi da tu sua bang tay', 'toi tu sua bang tay',
        'toi da tu lam bang tay', 'toi tu lam bang tay',
        'toi da tu sua xong', 'toi tu sua xong',
        'toi phai tu sua bang tay', 'toi phai tu lam bang tay',
    ))


def _codex_is_informational_question(text):
    """Recognize answerable questions without treating implementation requests as Q&A."""
    clean = _codex_plain_text(text)
    if not clean:
        return False
    action_terms = (
        'hay sua', 'sua cho toi', 'hay tao', 'tao cho toi', 'hay cap nhat',
        'cap nhat cho toi', 'hay xoa', 'xoa cho toi', 'hay chuyen',
        'chuyen cho toi', 'bat dau lam', 'trien khai', 'xay them',
    )
    if any(term in clean for term in action_terms):
        return False
    if any(term in clean for term in (
        'toi muon hoi', 'toi hoi cai', 'cho toi hoi', 'toi can hoi',
        'la gi', 'nghia la gi', 'tai sao', 'vi sao', 'the nao',
        'bao nhieu', 'co phai', 'dung ko', 'dung khong',
    )):
        return True
    # Quota/accounting questions often quote the words used in an earlier
    # correction (for example "sua lai") while only asking how that old work
    # should be charged.  They are evidence about a repair, not a new repair
    # request themselves.
    if _codex_is_quota_accounting_question(clean):
        return True
    return bool(re.search(r'\bco\b.{0,100}\b(?:duoc|the|phai)\b.{0,60}\b(?:ko|khong)\b', clean))


def _codex_is_quota_accounting_question(text):
    clean = _codex_plain_text(text)
    if any(clean.startswith(prefix) for prefix in (
        'ban lam tiep', 'lam tiep', 'tiep tuc', 'ban tiep tuc', 'sao ban dung',
    )):
        return False
    question_signal = bool(
        any(term in clean for term in (
            'tai sao', 'vi sao', 'sao ', 'co phai', 'dung ko', 'dung khong',
        )) or
        re.search(r'\b(?:chua\w*|nhi|the|ko|khong)$', clean)
    )
    quota_metric = any(term in clean for term in (
        'han muc', 'token', 'chi phi', 'credit', 'khoan phat',
        'phat sua loi', 'repair penalty', 'he so',
    ))
    accounting_action = any(term in clean for term in (
        'bi tru', 'cong vao', 'da cong', 'tinh vao', 'tieu hao', 'ton hon',
        'tang manh', 'giam manh', 'tang len', 'giam xuong', 'cao hon', 'thap hon',
    ))
    model_movement = (
        any(term in clean for term in (
            'sol high', 'sol xhigh', 'sol extra high', 'astra', 'luna',
            'gpt-5.5', 'gpt 5.5', 'chatgpt web', 'model',
        )) and
        any(term in clean for term in (
            'tang manh', 'giam manh', 'tang len', 'giam xuong',
            'cao hon', 'thap hon', 'ton hon', 'tieu hao',
        ))
    )
    return bool(question_signal and ((quota_metric and accounting_action) or model_movement))


def _codex_assistant_completion_signal(text):
    """Return strong completion evidence from the final assistant message only."""
    clean = _codex_plain_text(text)
    if not clean:
        return False
    incomplete = (
        'chua hoan tat', 'chua lam xong', 'chua sua xong', 'chua the hoan tat',
        'khong the hoan tat', 'chua co du bang chung', 'chua co ket qua',
        'chua truc tiep kiem tra', 'chua truc tiep thu', 'can ban xac nhan',
        'can kiem tra tren dien thoai', 'can thu tren dien thoai',
    )
    if any(phrase in clean for phrase in incomplete):
        return False
    return any(phrase in clean for phrase in (
        'da hoan tat', 'da lam xong', 'da sua xong', 'da cap nhat xong',
        'xong roi', 'toan bo kiem tra', 'tat ca kiem tra',
        'test deu pass', 'tests deu pass', 'regression pass',
    ))


def _codex_turn_infrastructure_blocked(turn):
    """Detect explicit tool/bridge unavailability only when no tool succeeded."""
    if _safe_nonnegative_int(turn.get('tool_success_count')) > 0:
        return False
    clean = _codex_plain_text(
        f"{turn.get('task_error') or ''} {turn.get('assistant_text') or ''}"
    )
    return any(phrase in clean for phrase in (
        'khong co phien cong cu thao tac',
        'chua co kenh cong cu thao tac',
        'khong goi duoc tool',
        'can ban mo lai task co quyen',
        'stream disconnected before completion',
        'launcher browser control channel failed',
        'chatgpt browser is busy',
        'rate limit exceeded',
        'usage limit reached',
        'het han muc nen khong',
    ))


def _codex_infer_terminal_status(raw_mission):
    """Infer only strong terminal outcomes; ambiguous work remains unresolved."""
    turns = list(raw_mission.get('turns') or [])
    if not turns:
        return None
    last = turns[-1]
    assistant_text = str(last.get('assistant_text') or '')
    has_answer = bool(
        assistant_text.strip() and
        _safe_nonnegative_int(last.get('assistant_message_count')) > 0
    )
    if not has_answer:
        return None
    if _codex_assistant_completion_signal(assistant_text):
        return 'accepted_inferred', 'medium'
    first_text = str(turns[0].get('user_text') or '')
    no_correction = not any(
        _codex_is_repair_prompt(turn.get('user_text') or '')
        for turn in turns[1:]
    )
    if no_correction and _codex_is_informational_question(first_text):
        return 'accepted_inferred', 'medium'
    return None


def _codex_is_non_task_prompt(text):
    """Ignore pure greetings/connectivity pings without hiding substantive prompts."""
    clean = _codex_plain_text(text)
    clean = re.sub(r'[^a-z0-9]+', ' ', clean).strip()
    return bool(re.fullmatch(
        r'(?:xin chao|chao ban|hello|helo|hi|alo|a lo|h[eu]+l+[eo]+)',
        clean,
    ))


def _codex_is_vague_continuation_prompt(text):
    clean = re.sub(r'[^a-z0-9]+', ' ', _codex_plain_text(text)).strip()
    if len(clean) > 90:
        return False
    return bool(re.fullmatch(
        r'(?:(?:ok|oke|okay) )?(?:the |gio )?(?:ban )?'
        r'(?:(?:lam|sua|kiem tra)(?: tiep)?(?: di)?(?: nhe| nha| ne)?|'
        r'bat dau(?: lam)?(?: tiep)?(?: di)?(?: nhe| nha| ne)?|'
        r'lam gi tiep theo day)',
        clean,
    ) or re.fullmatch(
        r'(?:sao )?(?:ban )?(?:dang )?(?:test|kiem tra|lam)(?: cai)? gi ma lau(?: the)?',
        clean,
    ))


def _codex_is_explicit_continuation_prompt(text):
    """Return true when the user explicitly resumes the earlier objective."""
    raw = str(text or '').lstrip().lower()
    if raw.startswith('<codex_internal_context') and 'source="goal"' in raw[:160]:
        return True
    if _codex_is_vague_continuation_prompt(text):
        return True
    clean = re.sub(r'[^a-z0-9:/._-]+', ' ', _codex_plain_text(text)).strip()
    if any(clean.startswith(prefix) for prefix in (
        'ban lam tiep', 'lam tiep', 'tiep tuc', 'ban tiep tuc', 'hay tiep tuc',
        'ban co lam tiep', 'co lam tiep nhiem vu nay',
        'hom bua dang lam', 'hom bua lam chua xong', 'nhiem vu vua roi',
        'cong viec vua roi', 'phan vua roi', 'ban dang do',
        'ok hom bua dang lam do', 'hom bua dang lam do',
    )):
        return True
    if ('codex://threads/' in clean and
            any(term in clean for term in ('tiep tuc', 'lam tiep', 'cong viec dang do'))):
        return True
    return bool(
        any(term in clean for term in (
            'het han muc', 'co han muc lai', 'han muc lai roi', 'bi tat may', 'mat dien',
        )) and
        any(term in clean for term in (
            'ban lam tiep', 'gio ban lam tiep', 'gio ban chay tiep', 'tiep tuc',
        ))
    )


def _codex_is_embedded_classification_example(text):
    clean = _codex_plain_text(text)
    return (
        ('vi du' in clean or 'truong hop lai' in clean or 'luot 1' in clean) and
        ('review lai tung mau' in clean or 'phan loai' in clean or 'loai cong viec' in clean)
    )


def _codex_usage_by_turn(recent_events, logical_turns=None):
    grouped = {}
    events = [event for event in (recent_events or []) if isinstance(event, dict) and event.get('task_id')]
    events.sort(key=lambda event: str(event.get('ts') or ''))
    logical_by_outer = {}
    for raw_turn in logical_turns or []:
        if not isinstance(raw_turn, dict) or not raw_turn.get('turn_id'):
            continue
        outer_turn_id = str(raw_turn.get('outer_turn_id') or raw_turn.get('usage_task_id') or '')
        if not outer_turn_id:
            continue
        logical_by_outer.setdefault(outer_turn_id, []).append(raw_turn)
    for turns in logical_by_outer.values():
        turns.sort(key=lambda turn: (
            str(turn.get('started_at') or ''),
            _safe_nonnegative_int(turn.get('logical_user_index')),
            str(turn.get('turn_id') or ''),
        ))

    for event in events:
        task_id = str(event.get('task_id') or '')
        turn_id = task_id
        candidates = logical_by_outer.get(task_id) or []
        if candidates:
            event_dt = _parse_iso_utc(event.get('ts'))
            selected = candidates[0]
            if event_dt is not None:
                for candidate in candidates:
                    candidate_dt = _parse_iso_utc(candidate.get('started_at'))
                    if candidate_dt is None or candidate_dt > event_dt:
                        break
                    selected = candidate
            turn_id = str(selected.get('turn_id') or task_id)
        model_key = str(event.get('model_key') or 'unknown')
        turn_usage = grouped.setdefault(turn_id, {
            'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
            'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
            'models': {}, 'route': [], 'last_at': '',
        })
        model_usage = turn_usage['models'].setdefault(model_key, {
            'model_key': model_key, 'input_tokens': 0, 'cached_input_tokens': 0,
            'cache_write_input_tokens': 0, 'output_tokens': 0,
            'thinking_tokens': 0, 'total_tokens': 0,
        })
        values = {
            'input_tokens': _safe_nonnegative_int(event.get('in')),
            'cached_input_tokens': _safe_nonnegative_int(event.get('cached_in')),
            'cache_write_input_tokens': _safe_nonnegative_int(event.get('cache_write_in')),
            'output_tokens': _safe_nonnegative_int(event.get('out')),
            'thinking_tokens': _safe_nonnegative_int(event.get('think')),
            'total_tokens': _safe_nonnegative_int(event.get('tot')),
        }
        for field, value in values.items():
            turn_usage[field] += value
            model_usage[field] += value
        if not turn_usage['route'] or turn_usage['route'][-1] != model_key:
            turn_usage['route'].append(model_key)
        turn_usage['last_at'] = max(str(turn_usage.get('last_at') or ''), str(event.get('ts') or ''))

    for turn_usage in grouped.values():
        total_cost = 0.0
        cost_known = True
        for model_usage in turn_usage['models'].values():
            cost = estimate_model_cost(
                model_usage['model_key'],
                input_tokens=model_usage['input_tokens'],
                output_tokens=model_usage['output_tokens'],
                cached_input_tokens=model_usage['cached_input_tokens'],
                cache_write_input_tokens=model_usage['cache_write_input_tokens'],
            )
            model_usage['cost_known'] = cost is not None
            model_usage['cost_usd'] = round(cost or 0.0, 6)
            if cost is None:
                cost_known = False
            else:
                total_cost += cost
        turn_usage['cost_known'] = cost_known
        turn_usage['cost_usd'] = round(total_cost, 6)
        turn_usage['models'] = list(turn_usage['models'].values())
    return grouped


def _codex_turn_has_model_evidence(turn):
    usage = turn.get('usage') if isinstance(turn.get('usage'), dict) else {}
    if usage.get('route') or _safe_nonnegative_int(usage.get('total_tokens')) > 0:
        return True
    return bool(turn.get('completed') or _safe_nonnegative_int(turn.get('assistant_message_count')) > 0)


def _codex_finalize_mission(raw_mission, reviews):
    turns = list(raw_mission.get('turns') or [])
    if not turns:
        return None
    anchor = str(turns[0].get('turn_id') or '')
    review = (reviews.get('missions') or {}).get(anchor)
    review = review if isinstance(review, dict) else {}

    category = raw_mission.get('category') or 'other'
    category_confidence = raw_mission.get('category_confidence') or 'low'
    raw_categories = []
    for raw_category in raw_mission.get('categories') or []:
        if raw_category in CODEX_TASK_CATEGORY_KEYS and raw_category not in raw_categories:
            raw_categories.append(raw_category)
    if category in CODEX_TASK_CATEGORY_KEYS and category not in raw_categories:
        raw_categories.insert(0, category)
    known_categories = [key for key in raw_categories if key != 'other']
    if known_categories:
        # A vague opening such as “can you read that chat?” may be classified
        # only after a later concrete turn names the actual work. Backfill only
        # low-confidence `other` turns from the nearest concrete turn so their
        # tokens are attributed to the resolved mission instead of reopening a
        # hard review case.
        for index, turn in enumerate(turns):
            if turn.get('category') != 'other':
                continue
            nearest = None
            for distance in range(1, len(turns) + 1):
                for candidate_index in (index - distance, index + distance):
                    if not 0 <= candidate_index < len(turns):
                        continue
                    candidate = turns[candidate_index]
                    candidate_category = candidate.get('category')
                    if candidate_category in CODEX_TASK_CATEGORY_KEYS and candidate_category != 'other':
                        nearest = candidate
                        break
                if nearest is not None:
                    break
            inherited_categories = list(
                (nearest or {}).get('categories') or
                ([nearest.get('category')] if nearest else known_categories[:1])
            )
            inherited_categories = [key for key in inherited_categories if key != 'other'] or known_categories[:1]
            turn['category'] = inherited_categories[0]
            turn['categories'] = inherited_categories
            turn['category_confidence'] = 'medium'
            turn['category_ambiguous'] = len(inherited_categories) > 1
            turn['category_inherited'] = True
        raw_categories = known_categories
        if category == 'other':
            category = known_categories[0]
            category_confidence = 'medium'
    review_categories = []
    if isinstance(review.get('categories'), list):
        for raw_category in review.get('categories') or []:
            if raw_category in CODEX_TASK_CATEGORY_KEYS and raw_category not in review_categories:
                review_categories.append(raw_category)
    if review_categories:
        categories = review_categories
        category = categories[0]
        category_confidence = 'high'
    elif review.get('category') in CODEX_TASK_CATEGORY_KEYS:
        category = review['category']
        categories = [category]
        category_confidence = 'high'
    else:
        categories = raw_categories or [category]

    status = raw_mission.get('status') or 'unresolved'
    status_confidence = raw_mission.get('status_confidence') or 'low'
    outcome = review.get('outcome')
    if outcome == 'accepted':
        status, status_confidence = 'accepted_manual', 'high'
    elif outcome == 'inferred':
        # The user no longer remembers the result, but the local history shows
        # a completed answer with no correction, objection or repair follow-up.
        # Keep that weaker evidence distinct from a direct confirmation.
        status, status_confidence = 'accepted_inferred_review', 'medium'
    elif outcome == 'unresolved':
        status, status_confidence = 'unresolved_manual', 'high'
    elif outcome == 'abandoned':
        status, status_confidence = 'abandoned_manual', 'high'
    elif outcome == 'user_repaired':
        status, status_confidence = 'failed_user_repaired_manual', 'high'
    elif outcome == 'excluded':
        status, status_confidence = 'excluded_manual', 'high'

    tool_call_count = sum(_safe_nonnegative_int(turn.get('tool_call_count')) for turn in turns)
    tool_success_count = sum(_safe_nonnegative_int(turn.get('tool_success_count')) for turn in turns)
    tool_failure_count = sum(_safe_nonnegative_int(turn.get('tool_failure_count')) for turn in turns)
    infrastructure_exclusion_reason = ''
    if outcome not in CODEX_MISSION_OUTCOME_OVERRIDES and tool_success_count <= 0:
        has_assistant_response = any(
            _safe_nonnegative_int(turn.get('assistant_message_count')) > 0
            for turn in turns
        )
        if not has_assistant_response:
            status, status_confidence = 'excluded_no_response_auto', 'high'
            infrastructure_exclusion_reason = 'no_model_response'
        elif (any(_codex_turn_infrastructure_blocked(turn) for turn in turns) and
              not any(_codex_assistant_completion_signal(turn.get('assistant_text') or '')
                      for turn in turns)):
            status, status_confidence = 'excluded_infrastructure_auto', 'high'
            infrastructure_exclusion_reason = 'tool_or_bridge_unavailable'

    totals = {
        'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
        'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
    }
    total_cost = 0.0
    cost_known = True
    route = []
    model_totals = {}
    for turn in turns:
        usage = turn.get('usage') if isinstance(turn.get('usage'), dict) else {}
        for field in totals:
            totals[field] += _safe_nonnegative_int(usage.get(field))
        if usage.get('cost_known') is False:
            cost_known = False
        total_cost += float(usage.get('cost_usd') or 0.0)
        turn_route = list(usage.get('route') or [])
        if not turn_route and turn.get('model_key') and _codex_turn_has_model_evidence(turn):
            turn_route = [turn['model_key']]
        for model_key in turn_route:
            if not route or route[-1] != model_key:
                route.append(model_key)
        for model_usage in usage.get('models') or []:
            key = str(model_usage.get('model_key') or 'unknown')
            target = model_totals.setdefault(key, {'model_key': key, 'total_tokens': 0, 'cost_usd': 0.0})
            target['total_tokens'] += _safe_nonnegative_int(model_usage.get('total_tokens'))
            target['cost_usd'] += float(model_usage.get('cost_usd') or 0.0)

    start_at = str(turns[0].get('started_at') or '')
    end_at = str(raw_mission.get('accepted_at') or turns[-1].get('completed_at') or
                 turns[-1].get('usage', {}).get('last_at') or turns[-1].get('started_at') or '')
    start_dt = _parse_iso_utc(start_at)
    end_dt = _parse_iso_utc(end_at)
    duration_minutes = None
    if start_dt is not None and end_dt is not None:
        duration_minutes = round(max(0.0, (end_dt - start_dt).total_seconds()) / 60.0, 1)

    prompt = re.sub(r'\s+', ' ', str(turns[0].get('user_text') or '')).strip()
    category_labels = {item['key']: item['label'] for item in CODEX_TASK_CATEGORIES}
    accepted = status.startswith('accepted_')
    mission = {
        'id': hashlib.sha1(f"{turns[0].get('thread_id')}|{anchor}".encode('utf-8')).hexdigest()[:16],
        'anchor_turn_id': anchor,
        'thread_id': str(turns[0].get('thread_id') or ''),
        'title': prompt[:180] or '(Không có nội dung yêu cầu)',
        'category': category,
        'category_label': category_labels.get(category, category_labels['other']),
        'categories': categories,
        'category_labels': [category_labels.get(key, category_labels['other']) for key in categories],
        'mixed_category': len(categories) > 1,
        'category_confidence': category_confidence,
        'status': status,
        'status_confidence': status_confidence,
        'accepted': accepted,
        'user_repaired': status.startswith('failed_user_repaired'),
        'accepted_at': str(raw_mission.get('accepted_at') or ''),
        'reviewed': bool(review),
        'category_reviewed': bool(review_categories) or review.get('category') in CODEX_TASK_CATEGORY_KEYS,
        'outcome_reviewed': review.get('outcome') in CODEX_MISSION_OUTCOME_OVERRIDES,
        'repair_link_mode': (
            'manual' if isinstance(review.get('repair_of_anchor_turn_id'), str)
            else ('none' if 'repair_of_anchor_turn_id' in review else 'auto')
        ),
        'inferred_boundary_reason': str(raw_mission.get('inferred_boundary_reason') or ''),
        'repair_of_anchor_turn_id_override': (
            str(review.get('repair_of_anchor_turn_id') or '')
            if isinstance(review.get('repair_of_anchor_turn_id'), str) else None
        ),
        'start_at': start_at,
        'end_at': end_at,
        'duration_minutes': duration_minutes,
        'turn_count': len(turns),
        'correction_turns': max(0, len(turns) - 1),
        'first_pass_success': bool(accepted and len(turns) == 1),
        'initial_instruction_tokens_est': _safe_nonnegative_int(turns[0].get('user_tokens_est')),
        'added_guidance_tokens_est': sum(_safe_nonnegative_int(turn.get('user_tokens_est')) for turn in turns[1:]),
        'user_messages': sum(_safe_nonnegative_int(turn.get('user_message_count')) for turn in turns),
        'tool_call_count': tool_call_count,
        'tool_success_count': tool_success_count,
        'tool_failure_count': tool_failure_count,
        'infrastructure_blocked': bool(infrastructure_exclusion_reason),
        'infrastructure_exclusion_reason': infrastructure_exclusion_reason,
        'route': route,
        'route_label': ' → '.join(route) if route else 'Không rõ model',
        'pure_model': len(set(route)) == 1 and bool(route),
        'model_key': route[0] if len(set(route)) == 1 and route else None,
        'model_totals': [
            {**value, 'cost_usd': round(value['cost_usd'], 6)}
            for value in sorted(model_totals.values(), key=lambda item: -item['total_tokens'])
        ],
        **totals,
        'cost_known': cost_known,
        'cost_usd': round(total_cost, 6),
        'turn_ids': [str(turn.get('turn_id') or '') for turn in turns],
        'turns': [{
            'turn_id': str(turn.get('turn_id') or ''),
            'usage_task_id': str(turn.get('usage_task_id') or turn.get('outer_turn_id') or turn.get('turn_id') or ''),
            'started_at': str(turn.get('started_at') or ''),
            'completed_at': str(turn.get('completed_at') or ''),
            'completed': bool(turn.get('completed')),
            'model_key': str(turn.get('model_key') or ''),
            'category': str(turn.get('category') or category),
            'categories': list(turn.get('categories') or [turn.get('category') or category]),
            'category_confidence': str(turn.get('category_confidence') or 'low'),
            'category_ambiguous': bool(turn.get('category_ambiguous')),
            'category_inherited': bool(turn.get('category_inherited')),
            'category_from_assistant': bool(turn.get('category_from_assistant')),
            'completion_signal': _codex_assistant_completion_signal(turn.get('assistant_text') or ''),
            'prompt': re.sub(r'\s+', ' ', str(turn.get('user_text') or '')).strip()[:320],
            'user_tokens_est': _safe_nonnegative_int(turn.get('user_tokens_est')),
            'total_tokens': _safe_nonnegative_int((turn.get('usage') or {}).get('total_tokens')),
            'cost_known': (turn.get('usage') or {}).get('cost_known') is not False,
            'cost_usd': round(float((turn.get('usage') or {}).get('cost_usd') or 0.0), 6),
            'tool_call_count': _safe_nonnegative_int(turn.get('tool_call_count')),
            'tool_success_count': _safe_nonnegative_int(turn.get('tool_success_count')),
            'tool_failure_count': _safe_nonnegative_int(turn.get('tool_failure_count')),
            'task_error': str(turn.get('task_error') or '')[-500:],
            'model_totals': [
                {
                    'model_key': str(item.get('model_key') or 'unknown'),
                    'total_tokens': _safe_nonnegative_int(item.get('total_tokens')),
                    'cost_known': item.get('cost_known') is not False,
                    'cost_usd': round(float(item.get('cost_usd') or 0.0), 6),
                }
                for item in ((turn.get('usage') or {}).get('models') or [])
                if isinstance(item, dict)
            ],
        } for turn in turns],
    }
    mission['referenced_thread_ids'] = sorted({
        ref
        for turn in mission['turns']
        for ref in _codex_referenced_thread_ids(turn.get('prompt') or '')
        if ref != mission['thread_id']
    })
    return mission


def _is_sol_high_baseline(model_key):
    clean = _codex_plain_text(model_key)
    return ('5.6 sol high' in clean and 'xhigh' not in clean and
            'extra high' not in clean and '(fast)' not in clean)


def _codex_mission_usable_for_matrix(mission):
    status = str(mission.get('status') or '')
    if status.startswith('excluded'):
        return False
    # A failed attempt becomes usable evidence when a later mission explicitly
    # repairs the same goal. This keeps the failed usage in the final
    # cost-to-acceptance chain instead of silently dropping it.
    if status.startswith('abandoned') and not _safe_nonnegative_int(
            mission.get('repair_followup_received_tokens')):
        return False
    if _safe_nonnegative_int(mission.get('total_tokens')) <= 0:
        return False
    if mission.get('has_delegated_work'):
        return False

    # A normal mixed-model route is confounded: no single model completed the
    # objective on its own.  The one useful exception is an explicitly detected
    # repair chain, because those tokens are attributed to the repair model and
    # (as a penalty) to the model that produced the result being repaired.
    if mission.get('pure_model') is False:
        mission_id = str(mission.get('id') or '')
        repair_of = str(mission.get('repair_of_mission_id') or '')
        return bool(
            _safe_nonnegative_int(mission.get('repair_followup_received_tokens')) or
            mission.get('repair_penalty_events') or
            (repair_of and repair_of == mission_id)
        )

    # Legacy/manual fixtures may predate the pure_model field.  Infer purity
    # conservatively from the model evidence instead of dropping them.
    if mission.get('pure_model') is None:
        models = set()
        if mission.get('model_key'):
            models.add(str(mission.get('model_key')))
        for item in mission.get('model_totals') or []:
            if isinstance(item, dict) and item.get('model_key'):
                models.add(str(item.get('model_key')))
        for turn in mission.get('turns') or []:
            if not isinstance(turn, dict):
                continue
            for model_key, tokens in _codex_turn_model_tokens(turn, mission.get('model_key')):
                if model_key and tokens > 0:
                    models.add(model_key)
        if len(models) > 1:
            return False
    return True


def _codex_is_repair_prompt(text):
    """Return only actionable evidence that an earlier result needs repair.

    Accent stripping makes Vietnamese ``dung`` ambiguous: it can mean either
    "correct" (đúng) or "use" (dùng).  Broad substring checks therefore used
    to misread constraints such as "không dùng dữ liệu cũ" and observations
    such as "chưa dùng hết CPU" as failed work.  Prefer accent-aware evidence,
    then use a narrow fallback for genuinely unaccented prompts.
    """
    raw = unicodedata.normalize('NFC', str(text or '').lower())
    raw = re.sub(r'\s+', ' ', raw).strip()
    clean = _codex_plain_text(raw)
    if not clean:
        return False

    user_completed_repair = any(token in clean for token in (
        'toi vua lam lai', 'toi da lam lai', 'toi vua sua lai', 'toi da sua lai',
        'toi vua tu sua', 'toi da tu sua',
    ))
    model_repair_request = bool(re.search(
        r'\bban\s+(?:hay\s+)?(?:sua|lam lai|kiem tra lai|khac phuc)\b|\b(?:sua|lam lai)\s+cho toi\b',
        clean,
    ))
    if user_completed_repair and not model_repair_request:
        return False

    if any(token in raw for token in (
        'chưa ổn', 'chưa đúng', 'không đúng', 'vẫn sai', 'sai rồi', 'sửa lại',
        'làm lại', 'chưa sửa', 'chưa được', 'không ổn', 'fix lại',
        'chưa chính xác', 'không chính xác', 'sửa chưa đúng', 'sửa chưa ổn',
        'đâu mất tiêu', 'biến mất hết',
    )):
        return True

    # These forms remain unambiguous after accent removal.
    if any(token in clean for token in (
        'chua on', 'van sai', 'sai roi', 'sua lai', 'lam lai', 'chua sua',
        'chua duoc', 'khong on', 'fix lai', 'chua chinh xac',
        'khong chinh xac', 'sua chua on', 'dau mat tieu', 'bien mat het',
    )):
        return True

    # Preserve support for users who type without accents while rejecting
    # "chua dung het" / "khong dung <object>" instruction clauses.
    if re.search(r'\b(?:chua|khong) dung(?:\s*[,.;!?]|\s*$)', raw):
        return True
    if re.search(r'\b(?:chua|khong) dung\s+(?:roi|lam|nhu|o|cho)\b', clean):
        return True

    # "Vẫn chưa" is useful only when it names a failed result.  A bare
    # "vẫn chưa đưa lên GitHub" is normally a constraint, not repair evidence.
    return bool(re.search(
        r'\bvan chua\s+(?:duoc|xong|sua|on|dung|chinh xac|hien|load|cap nhat|chay|hoat dong|tim|ra)\b',
        clean,
    ))


def _codex_turn_primary_model_hint(turn):
    if not isinstance(turn, dict):
        return ''
    usage = turn.get('usage') if isinstance(turn.get('usage'), dict) else {}
    route = [str(item) for item in (usage.get('route') or []) if str(item)]
    if route:
        return route[-1]
    return str(turn.get('model_key') or '')


def _codex_is_cross_model_new_action_transition(text, previous_text, model_changed):
    """Detect a concrete new objective after a completed Q&A/model handoff.

    This is deliberately narrower than the normal ``new_work`` vocabulary.
    It handles prompts such as "ok giờ bạn đem bản mới sang folder public" but
    leaves plan approvals and vague continuations ("ok giờ làm tiếp") inside
    their existing mission.
    """
    if not model_changed or not _codex_is_informational_question(previous_text):
        return False
    clean = _codex_plain_text(text)
    if not re.match(r'^(?:ok\s*,?\s*)?gio ban\b', clean):
        return False
    if any(token in clean for token in (
        'lam tiep', 'tiep tuc', 'chay tiep', 'chay xem', 'thu xem',
        'thuc hien tiep', 'cong viec chua', 'bat tay sua',
    )):
        return False
    return bool(re.search(
        r'\bgio ban\s+(?:hay\s+)?(?:dem|dua|chuyen|sao chep|cap nhat|tao|xay|them)\b',
        clean,
    ))


def _codex_referenced_thread_ids(text):
    return {
        match.lower()
        for match in re.findall(
            r'codex://threads/([0-9a-f]{8}-[0-9a-f-]{20,})',
            str(text or ''),
            flags=re.IGNORECASE,
        )
    }


_CODEX_REPAIR_TOPIC_STOPWORDS = {
    'ban', 'toi', 'minh', 'anh', 'em', 'nay', 'kia', 'do', 'day', 'roi', 'van',
    'chua', 'khong', 'duoc', 'dung', 'sai', 'sua', 'lai', 'lam', 'giup', 'cho',
    'thu', 'kiem', 'tra', 'tiep', 'theo', 'them', 'cai', 'cho', 'mot', 'nhieu',
    'viec', 'task', 'model', 'prompt', 'ok', 'nhe', 'nhi', 'a', 'oi', 'va', 'la',
    'co', 'cua', 'trong', 'tren', 'duoi', 'sau', 'truoc', 'luc', 'gio', 'nay',
}


def _codex_repair_topic_tokens(text):
    clean = _codex_plain_text(text)
    return {
        token for token in re.findall(r'[a-z0-9_.:/-]{3,}', clean)
        if token not in _CODEX_REPAIR_TOPIC_STOPWORDS
    }


def _codex_repair_link_candidates(mission, prior_missions, limit=8):
    """Rank plausible earlier objectives without silently forcing a weak link."""
    repair_text = ' '.join([
        str(mission.get('title') or ''),
        *[str(turn.get('prompt') or '') for turn in mission.get('turns') or [] if isinstance(turn, dict)],
    ])
    repair_tokens = _codex_repair_topic_tokens(repair_text)
    repair_categories = set(mission.get('categories') or [mission.get('category') or 'other'])
    repair_thread = str(mission.get('thread_id') or '')
    repair_dt = _parse_iso_utc(mission.get('start_at'))
    referenced_threads = set(mission.get('referenced_thread_ids') or [])
    ranked = []
    for recency, candidate in enumerate(reversed(prior_missions)):
        candidate_text = ' '.join([
            str(candidate.get('title') or ''),
            *[str(turn.get('prompt') or '') for turn in candidate.get('turns') or [] if isinstance(turn, dict)],
        ])
        candidate_tokens = _codex_repair_topic_tokens(candidate_text)
        shared = repair_tokens & candidate_tokens
        shared_numeric = {token for token in shared if any(char.isdigit() for char in token)}
        same_thread = bool(repair_thread and repair_thread == str(candidate.get('thread_id') or ''))
        explicit_thread_reference = str(candidate.get('thread_id') or '').lower() in referenced_threads
        candidate_dt = _parse_iso_utc(candidate.get('end_at') or candidate.get('start_at'))
        gap_hours = (
            max(0.0, (repair_dt - candidate_dt).total_seconds() / 3600.0)
            if repair_dt is not None and candidate_dt is not None else None
        )
        category_overlap = bool(
            repair_categories & set(candidate.get('categories') or [candidate.get('category') or 'other'])
        )
        # Cross-thread automatic links require distinctive shared evidence.
        if not same_thread and not explicit_thread_reference and len(shared) < 2 and not shared_numeric:
            continue
        score = (
            (8.0 if same_thread else 0.0) +
            (18.0 if explicit_thread_reference else 0.0) +
            (3.0 if category_overlap else 0.0) +
            min(12.0, 3.0 * len(shared)) +
            min(12.0, 6.0 * len(shared_numeric)) +
            max(0.0, 2.0 - 0.08 * recency)
        )
        ranked.append((score, len(shared), bool(shared_numeric), recency, gap_hours, candidate))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[3]))
    result = []
    for score, shared_count, has_numeric, _, gap_hours, candidate in ranked[:max(1, int(limit or 8))]:
        result.append({
            'mission_id': candidate.get('id'),
            'anchor_turn_id': candidate.get('anchor_turn_id'),
            'title': candidate.get('title'),
            'start_at': candidate.get('start_at'),
            'model_key': _codex_mission_primary_model(candidate),
            'categories': list(candidate.get('categories') or [candidate.get('category') or 'other']),
            'score': round(score, 3),
            'shared_topic_tokens': shared_count,
            'has_shared_identifier': has_numeric,
            'same_thread': str(candidate.get('thread_id') or '') == repair_thread,
            'explicit_thread_reference': explicit_thread_reference,
            'gap_hours': round(gap_hours, 3) if gap_hours is not None else None,
        })
    return result


def _codex_turn_model_tokens(turn, fallback_model=None):
    values = []
    for item in turn.get('model_totals') or []:
        if not isinstance(item, dict):
            continue
        model_key = str(item.get('model_key') or '')
        tokens = _safe_nonnegative_int(item.get('total_tokens'))
        if model_key and tokens > 0:
            values.append((model_key, tokens))
    if values:
        return values
    model_key = str(turn.get('model_key') or fallback_model or '')
    tokens = _safe_nonnegative_int(turn.get('total_tokens'))
    return [(model_key, tokens)] if model_key and tokens > 0 else []


def _codex_mission_primary_model(mission):
    turns = mission.get('turns') or []
    if turns:
        values = _codex_turn_model_tokens(turns[0], mission.get('model_key'))
        if values:
            return max(values, key=lambda item: item[1])[0]
    if mission.get('model_key'):
        return str(mission.get('model_key'))
    totals = [item for item in mission.get('model_totals') or [] if isinstance(item, dict)]
    if totals:
        return str(max(totals, key=lambda item: _safe_nonnegative_int(item.get('total_tokens'))).get('model_key') or '')
    return ''


def _codex_build_repair_capabilities(missions):
    """Summarize accepted cross-model repair evidence.

    A terminal accepted repair is evidence that its model repaired every distinct
    model earlier in the same repair chain.  Failed/intermediate repair missions
    still count as attempts, but never as successful rescues.  Artificial
    Analysis intelligence is attached only as a capability prior; it does not
    turn an unobserved pair into an empirical success.
    """
    by_id = {
        str(mission.get('id') or ''): mission
        for mission in missions or []
        if mission.get('id')
    }
    pairs = {}

    for mission in missions or []:
        repair_of = str(mission.get('repair_of_mission_id') or '')
        target_model = _codex_mission_primary_model(mission)
        if not repair_of or not target_model:
            continue

        ancestors = []
        seen_ids = set()
        current_id = repair_of
        while current_id and current_id not in seen_ids:
            seen_ids.add(current_id)
            ancestor = by_id.get(current_id)
            if ancestor is None:
                break
            ancestors.append(ancestor)
            current_id = str(ancestor.get('repair_of_mission_id') or '')

        # Every cross-model repair is an attempt against its direct predecessor.
        # Once the terminal repair is accepted, it also proves a successful path
        # back to each earlier distinct model in the chain.
        sources = ancestors if mission.get('accepted') else ancestors[:1]
        seen_source_models = set()
        for ancestor in sources:
            source_model = _codex_mission_primary_model(ancestor)
            if (not source_model or source_model == target_model or
                    source_model in seen_source_models):
                continue
            seen_source_models.add(source_model)
            key = (source_model, target_model)
            item = pairs.setdefault(key, {
                'from_model': source_model,
                'to_model': target_model,
                'attempts': 0,
                'accepted_repairs': 0,
                'accepted_mission_ids': [],
                'categories': set(),
                'quota_samples': [],
                'link_confidences': [],
            })
            item['attempts'] += 1
            item['link_confidences'].append(str(mission.get('repair_link_confidence') or 'low'))
            if mission.get('accepted'):
                item['accepted_repairs'] += 1
                item['accepted_mission_ids'].append(mission.get('id'))
                item['categories'].update(
                    mission.get('repair_penalty_categories') or
                    ancestor.get('categories') or
                    [ancestor.get('category') or 'other']
                )
                quota = mission.get('quota_pct_5h')
                if quota is not None:
                    try:
                        item['quota_samples'].append(float(quota))
                    except (TypeError, ValueError):
                        pass

    rows = []
    for item in pairs.values():
        source_metrics = get_verified_aa_metrics(get_benchmark_for_model(item['from_model']))
        target_metrics = get_verified_aa_metrics(get_benchmark_for_model(item['to_model']))
        source_iq = source_metrics.get('intelligence_index')
        target_iq = target_metrics.get('intelligence_index')
        iq_delta = (
            round(float(target_iq) - float(source_iq), 2)
            if source_iq is not None and target_iq is not None else None
        )
        accepted = item['accepted_repairs']
        attempts = item['attempts']
        confidences = item['link_confidences']
        empirical_confidence = 'low'
        if accepted >= 3 and all(value in {'high', 'manual'} for value in confidences):
            empirical_confidence = 'high'
        elif accepted >= 2 or (accepted >= 1 and any(value == 'manual' for value in confidences)):
            empirical_confidence = 'medium'
        rows.append({
            'from_model': item['from_model'],
            'to_model': item['to_model'],
            'attempts': attempts,
            'accepted_repairs': accepted,
            'success_rate': round(accepted / attempts, 4) if attempts else None,
            'categories': sorted(item['categories']),
            'median_repair_quota_pct_5h': (
                round(float(statistics.median(item['quota_samples'])), 4)
                if item['quota_samples'] else None
            ),
            'quota_sample_count': len(item['quota_samples']),
            'source_intelligence_index': source_iq,
            'target_intelligence_index': target_iq,
            'intelligence_delta': iq_delta,
            'capability_prior': (
                'higher' if iq_delta is not None and iq_delta > 0
                else ('not_higher' if iq_delta is not None else 'unavailable')
            ),
            'empirical_confidence': empirical_confidence,
            'accepted_mission_ids': item['accepted_mission_ids'],
        })

    rows.sort(key=lambda row: (
        row['accepted_repairs'] > 0,
        row['accepted_repairs'],
        row['success_rate'] if row['success_rate'] is not None else -1,
        -(row['median_repair_quota_pct_5h'] or 10**9),
    ), reverse=True)
    return {
        'pairs': rows,
        'accepted_pair_count': sum(1 for row in rows if row['accepted_repairs'] > 0),
        'method': 'accepted_cross_model_repair_chains',
        'capability_prior': 'artificial_analysis_intelligence_v4.3.2',
    }


def _codex_apply_repair_penalties(missions):
    for mission in missions:
        mission['repair_of_mission_id'] = None
        mission['repair_of_anchor_turn_id'] = None
        mission['repair_original_model'] = None
        mission['repair_tokens'] = 0
        mission['repair_penalty_tokens'] = 0
        mission['repair_penalty_applied_to_model'] = None
        mission['repair_penalty_received_tokens'] = 0
        mission['repair_followup_received_tokens'] = 0
        mission['repair_penalty_events'] = []
        mission['penalized_total_tokens'] = _safe_nonnegative_int(mission.get('total_tokens'))
        mission['repair_root_mission_id'] = mission.get('id')
        mission['repair_depth'] = 0
        mission['repair_chain_models'] = []
        mission['repair_link_candidates'] = []
        mission['repair_link_confidence'] = None

    ordered = sorted(
        missions,
        key=lambda item: (str(item.get('start_at') or ''), str(item.get('id') or '')),
    )
    by_anchor = {
        str(mission.get('anchor_turn_id') or ''): mission
        for mission in ordered if mission.get('anchor_turn_id')
    }
    by_id = {
        str(mission.get('id') or ''): mission
        for mission in ordered if mission.get('id')
    }
    previous_by_thread = {}

    # First establish direct repair links.  Manual links can cross intervening
    # work (and even threads); automatic non-adjacent links require distinctive
    # topic evidence, while the immediate same-thread behavior remains the
    # conservative fallback for short deictic prompts such as “sửa lại”.
    for index, mission in enumerate(ordered):
        turns = list(mission.get('turns') or [])
        thread_key = str(mission.get('thread_id') or '')
        previous = previous_by_thread.get(thread_key)
        repair_source = None
        repair_start = None
        mode = str(mission.get('repair_link_mode') or 'auto')
        override_anchor = str(mission.get('repair_of_anchor_turn_id_override') or '')
        candidates = _codex_repair_link_candidates(mission, ordered[:index])
        mission['repair_link_candidates'] = candidates

        if mode == 'manual' and override_anchor:
            repair_source = by_anchor.get(override_anchor)
            if repair_source is not None and repair_source is not mission:
                repair_start = 0
                mission['repair_link_confidence'] = 'manual'
            else:
                repair_source = None
        elif (mode != 'none' and turns and
              _safe_nonnegative_int(mission.get('total_tokens')) > 0 and
              _codex_is_repair_prompt(turns[0].get('prompt') or '') and
              not _codex_is_quota_accounting_question(turns[0].get('prompt') or '')):
            explicit = next((
                item for item in candidates if item.get('explicit_thread_reference')
            ), None)
            repair_dt = _parse_iso_utc(mission.get('start_at'))
            previous_dt = _parse_iso_utc(
                (previous or {}).get('end_at') or (previous or {}).get('start_at')
            )
            recent_same_thread = bool(
                previous is not None and repair_dt is not None and previous_dt is not None and
                0 <= (repair_dt - previous_dt).total_seconds() <= 2 * 3600
            )
            # A correction immediately after another mission normally refers to
            # that result. Prefer it over an older mission that happens to share
            # more generic topic words. An explicit task link still wins.
            if explicit:
                strong = explicit
            elif recent_same_thread:
                strong = next((
                    item for item in candidates
                    if item.get('mission_id') == previous.get('id')
                ), None)
            else:
                strong = next((
                    item for item in candidates
                    if item.get('same_thread') and
                    item.get('gap_hours') is not None and
                    item.get('gap_hours') <= 7 * 24 and
                    (item.get('shared_topic_tokens', 0) >= 1 or
                     item.get('has_shared_identifier'))
                ), None)
            if strong:
                repair_source = by_id.get(str(strong.get('mission_id') or ''))
                repair_start = 0
                mission['repair_link_confidence'] = (
                    'high' if (strong.get('explicit_thread_reference') or
                               strong.get('has_shared_identifier') or
                               strong.get('shared_topic_tokens', 0) >= 2 or
                               repair_source is previous)
                    else 'medium'
                )
            elif previous is not None and recent_same_thread:
                repair_source = previous
                repair_start = 0
                mission['repair_link_confidence'] = 'high' if recent_same_thread else 'low'
        if repair_source is None and mode != 'none' and turns:
            for turn_index, turn in enumerate(turns[1:], start=1):
                if (_codex_is_repair_prompt(turn.get('prompt') or '') and
                        not _codex_is_quota_accounting_question(turn.get('prompt') or '')):
                    repair_source = mission
                    repair_start = turn_index
                    mission['repair_link_confidence'] = 'high'
                    break

        if repair_source is None:
            previous_by_thread[thread_key] = mission
            continue
        original_model = _codex_mission_primary_model(repair_source)
        if not original_model:
            previous_by_thread[thread_key] = mission
            continue

        model_tokens = {}
        if turns and repair_start is not None:
            for turn in turns[repair_start:]:
                for model_key, tokens in _codex_turn_model_tokens(turn, mission.get('model_key')):
                    model_tokens[model_key] = model_tokens.get(model_key, 0) + tokens
        if not model_tokens:
            for item in mission.get('model_totals') or []:
                if not isinstance(item, dict):
                    continue
                model_key = str(item.get('model_key') or '')
                tokens = _safe_nonnegative_int(item.get('total_tokens'))
                if model_key and tokens > 0:
                    model_tokens[model_key] = model_tokens.get(model_key, 0) + tokens
        if not model_tokens and mission.get('model_key'):
            model_tokens[str(mission.get('model_key'))] = _safe_nonnegative_int(mission.get('total_tokens'))

        repair_tokens = sum(model_tokens.values())
        penalty_tokens = sum(tokens for model_key, tokens in model_tokens.items() if model_key != original_model)
        mission['repair_of_mission_id'] = repair_source.get('id')
        mission['repair_of_anchor_turn_id'] = repair_source.get('anchor_turn_id')
        mission['repair_original_model'] = original_model
        mission['repair_tokens'] = repair_tokens
        mission['repair_penalty_tokens'] = penalty_tokens
        mission['repair_penalty_applied_to_model'] = original_model if penalty_tokens > 0 else None
        mission['repair_penalty_categories'] = list(
            repair_source.get('categories') or [repair_source.get('category') or 'other']
        )
        mission['_repair_model_tokens'] = dict(model_tokens)
        previous_by_thread[thread_key] = mission

    # Then propagate every later repair to each distinct model that previously
    # failed in the chain.  A -> B -> C therefore charges C to C as real usage
    # and as a penalty to both A and B; repeated use of the same model is only
    # penalized once for that downstream repair.
    for mission in ordered:
        repair_of = str(mission.get('repair_of_mission_id') or '')
        if not repair_of or repair_of == str(mission.get('id') or ''):
            continue
        direct_source = by_id.get(repair_of)
        if direct_source is None:
            continue
        ancestors = []
        seen_ids = set()
        current = direct_source
        while current is not None:
            current_id = str(current.get('id') or '')
            if not current_id or current_id in seen_ids:
                break
            seen_ids.add(current_id)
            ancestors.append(current)
            parent_id = str(current.get('repair_of_mission_id') or '')
            current = by_id.get(parent_id) if parent_id and parent_id != current_id else None
        root = ancestors[-1] if ancestors else direct_source
        mission['repair_root_mission_id'] = root.get('id')
        mission['repair_depth'] = len(ancestors)
        chain_models = []
        for ancestor in reversed(ancestors):
            model_key = _codex_mission_primary_model(ancestor)
            if model_key and (not chain_models or chain_models[-1] != model_key):
                chain_models.append(model_key)
        repair_models = list((mission.get('_repair_model_tokens') or {}).keys())
        for model_key in repair_models:
            if model_key and (not chain_models or chain_models[-1] != model_key):
                chain_models.append(model_key)
        mission['repair_chain_models'] = chain_models
        root_categories = list(root.get('categories') or [root.get('category') or 'other'])

        penalized_models = set()
        for ancestor in ancestors:
            ancestor['repair_followup_received_tokens'] = (
                _safe_nonnegative_int(ancestor.get('repair_followup_received_tokens')) +
                _safe_nonnegative_int(mission.get('repair_tokens'))
            )
            ancestor_model = _codex_mission_primary_model(ancestor)
            if not ancestor_model or ancestor_model in penalized_models:
                continue
            penalized_models.add(ancestor_model)
            penalty = sum(
                tokens for model_key, tokens in (mission.get('_repair_model_tokens') or {}).items()
                if model_key != ancestor_model
            )
            if penalty <= 0:
                continue
            event = {
                'repair_mission_id': mission.get('id'),
                'repair_anchor_turn_id': mission.get('anchor_turn_id'),
                'repair_model_tokens': dict(mission.get('_repair_model_tokens') or {}),
                'penalty_tokens': penalty,
                'applied_to_model': ancestor_model,
                'categories': root_categories,
                'at': mission.get('start_at'),
                'repair_depth': mission.get('repair_depth'),
            }
            ancestor['repair_penalty_received_tokens'] = (
                _safe_nonnegative_int(ancestor.get('repair_penalty_received_tokens')) + penalty
            )
            ancestor.setdefault('repair_penalty_events', []).append(event)
            ancestor['penalized_total_tokens'] = (
                _safe_nonnegative_int(ancestor.get('total_tokens')) +
                _safe_nonnegative_int(ancestor.get('repair_penalty_received_tokens'))
            )

        direct_model = _codex_mission_primary_model(direct_source)
        mission['repair_penalty_tokens'] = sum(
            tokens for model_key, tokens in (mission.get('_repair_model_tokens') or {}).items()
            if model_key != direct_model
        )
        mission['repair_penalty_applied_to_model'] = direct_model if mission['repair_penalty_tokens'] > 0 else None
        mission['repair_penalty_categories'] = root_categories
        mission.pop('_repair_model_tokens', None)


def _codex_repair_accounting_summary(missions):
    by_model = {}
    raw_total = 0
    penalty_total = 0
    raw_quota_total = 0.0
    penalty_quota_total = 0.0
    quota_complete = True
    for mission in missions or []:
        quota_by_model = {
            str(item.get('model_key') or ''): item
            for item in mission.get('quota_model_totals') or []
            if isinstance(item, dict) and item.get('model_key')
        }
        for item in mission.get('model_totals') or []:
            if not isinstance(item, dict):
                continue
            model_key = str(item.get('model_key') or '')
            tokens = _safe_nonnegative_int(item.get('total_tokens'))
            if not model_key or tokens <= 0:
                continue
            target = by_model.setdefault(model_key, {
                'model_key': model_key,
                'raw_tokens': 0,
                'repair_penalty_tokens': 0,
                'effective_tokens': 0,
                'raw_quota_pct_5h': 0.0,
                'repair_penalty_quota_pct_5h': 0.0,
                'effective_quota_pct_5h': None,
                'quota_known': True,
            })
            target['raw_tokens'] += tokens
            raw_total += tokens
            quota_item = quota_by_model.get(model_key)
            if quota_item and quota_item.get('quota_known'):
                quota_value = float(quota_item.get('quota_pct_5h') or 0.0)
                target['raw_quota_pct_5h'] += quota_value
                raw_quota_total += quota_value
            else:
                target['quota_known'] = False
                quota_complete = False

        for event in mission.get('repair_penalty_events') or []:
            if not isinstance(event, dict):
                continue
            original_model = str(event.get('applied_to_model') or '')
            penalty = _safe_nonnegative_int(event.get('penalty_tokens'))
            if not original_model or penalty <= 0:
                continue
            target = by_model.setdefault(original_model, {
                'model_key': original_model,
                'raw_tokens': 0,
                'repair_penalty_tokens': 0,
                'effective_tokens': 0,
                'raw_quota_pct_5h': 0.0,
                'repair_penalty_quota_pct_5h': 0.0,
                'effective_quota_pct_5h': None,
                'quota_known': True,
            })
            target['repair_penalty_tokens'] += penalty
            penalty_total += penalty
            if event.get('quota_known') and event.get('penalty_quota_pct_5h') is not None:
                penalty_quota = float(event.get('penalty_quota_pct_5h') or 0.0)
                target['repair_penalty_quota_pct_5h'] += penalty_quota
                penalty_quota_total += penalty_quota
            else:
                target['quota_known'] = False
                quota_complete = False

    rows = []
    for target in by_model.values():
        target['effective_tokens'] = target['raw_tokens'] + target['repair_penalty_tokens']
        if target.get('quota_known'):
            target['raw_quota_pct_5h'] = round(target['raw_quota_pct_5h'], 6)
            target['repair_penalty_quota_pct_5h'] = round(target['repair_penalty_quota_pct_5h'], 6)
            target['effective_quota_pct_5h'] = round(
                target['raw_quota_pct_5h'] + target['repair_penalty_quota_pct_5h'], 6
            )
        rows.append(target)
    rows.sort(key=lambda item: (-item['effective_tokens'], item['model_key']))
    return {
        'raw_tokens': raw_total,
        'repair_penalty_tokens': penalty_total,
        'effective_tokens': raw_total + penalty_total,
        'raw_quota_pct_5h': round(raw_quota_total, 6) if quota_complete else None,
        'repair_penalty_quota_pct_5h': round(penalty_quota_total, 6) if quota_complete else None,
        'effective_quota_pct_5h': (
            round(raw_quota_total + penalty_quota_total, 6) if quota_complete else None
        ),
        'quota_known': quota_complete,
        'by_model': rows,
    }


def _codex_apply_mission_quota_estimates(missions, quota_efficiency=None, quota_task_samples=None):
    """Attach empirical 5-hour quota burn to each model contribution.

    A validated same-task quota delta is used first and distributed across
    logical turns by token share.  Otherwise the model's measured
    tokens-per-1%-quota rate is used.  Missing calibration stays unavailable;
    it is never silently replaced by a universal token conversion.
    """
    rates = {}
    for row in (quota_efficiency or {}).get('models') or []:
        if not isinstance(row, dict):
            continue
        model_key = str(row.get('model_key') or '')
        try:
            tokens_per_pct = float(row.get('tokens_per_quota_pct') or 0.0)
        except (TypeError, ValueError, OverflowError):
            tokens_per_pct = 0.0
        if model_key and math.isfinite(tokens_per_pct) and tokens_per_pct > 0:
            rates[model_key] = {
                'tokens_per_quota_pct': tokens_per_pct,
                'confidence': str(row.get('confidence') or 'low'),
                'sample_count': _safe_nonnegative_int(row.get('sample_count')),
            }

    direct_by_task = {}
    for sample in quota_task_samples or []:
        if not isinstance(sample, dict):
            continue
        task_id = str(sample.get('task_id') or '')
        try:
            quota_pct = float(sample.get('estimated_quota_delta_pct') or 0.0)
        except (TypeError, ValueError, OverflowError):
            quota_pct = 0.0
        if task_id and math.isfinite(quota_pct) and quota_pct > 0:
            direct_by_task[task_id] = sample

    task_model_tokens = {}
    for mission in missions or []:
        for turn in mission.get('turns') or []:
            if not isinstance(turn, dict):
                continue
            task_id = str(turn.get('usage_task_id') or turn.get('turn_id') or '')
            for model_key, tokens in _codex_turn_model_tokens(turn, mission.get('model_key')):
                task_model_tokens[(task_id, model_key)] = task_model_tokens.get((task_id, model_key), 0) + tokens

    confidence_rank = {'low': 0, 'medium': 1, 'high': 2}
    mission_by_id = {str(m.get('id') or ''): m for m in missions or [] if m.get('id')}
    for mission in missions or []:
        mission_models = {}
        mission_sources = set()
        mission_confidences = []
        all_known = True
        had_contribution = False
        for turn in mission.get('turns') or []:
            if not isinstance(turn, dict):
                continue
            task_id = str(turn.get('usage_task_id') or turn.get('turn_id') or '')
            turn_quota = 0.0
            turn_known = True
            turn_had_contribution = False
            turn_sources = set()
            model_totals = [item for item in turn.get('model_totals') or [] if isinstance(item, dict)]
            if not model_totals:
                model_totals = [
                    {'model_key': model_key, 'total_tokens': tokens}
                    for model_key, tokens in _codex_turn_model_tokens(turn, mission.get('model_key'))
                ]
                turn['model_totals'] = model_totals
            for model_usage in model_totals:
                model_key = str(model_usage.get('model_key') or '')
                tokens = _safe_nonnegative_int(model_usage.get('total_tokens'))
                if not model_key or tokens <= 0:
                    continue
                had_contribution = True
                turn_had_contribution = True
                quota_pct = None
                source = 'unavailable'
                confidence = 'low'
                direct = direct_by_task.get(task_id)
                if direct and str(direct.get('model_key') or '') == model_key:
                    denominator = task_model_tokens.get((task_id, model_key), 0)
                    try:
                        direct_total = float(direct.get('estimated_quota_delta_pct') or 0.0)
                    except (TypeError, ValueError, OverflowError):
                        direct_total = 0.0
                    if denominator > 0 and direct_total > 0:
                        quota_pct = direct_total * tokens / denominator
                        source = 'direct_task'
                        coverage = float(direct.get('token_coverage') or 0.0)
                        confidence = 'high' if coverage >= 0.75 else ('medium' if coverage >= 0.5 else 'low')
                if quota_pct is None and model_key in rates:
                    quota_pct = tokens / rates[model_key]['tokens_per_quota_pct']
                    source = 'calibrated_model'
                    confidence = rates[model_key]['confidence']
                known = quota_pct is not None and math.isfinite(quota_pct) and quota_pct >= 0
                model_usage['quota_known'] = known
                model_usage['quota_pct_5h'] = round(quota_pct, 6) if known else None
                model_usage['quota_source'] = source
                model_usage['quota_confidence'] = confidence if known else 'low'
                target = mission_models.setdefault(model_key, {
                    'model_key': model_key,
                    'total_tokens': 0,
                    'quota_pct_5h': 0.0,
                    'quota_known': True,
                    'quota_sources': set(),
                    'quota_confidences': [],
                })
                target['total_tokens'] += tokens
                target['quota_known'] = bool(target['quota_known']) and known
                if known:
                    target['quota_pct_5h'] += quota_pct
                    target['quota_sources'].add(source)
                    target['quota_confidences'].append(confidence)
                    turn_quota += quota_pct
                    turn_sources.add(source)
                    mission_sources.add(source)
                    mission_confidences.append(confidence)
                else:
                    turn_known = False
                    all_known = False
            turn['quota_known'] = bool(turn_known and turn_had_contribution)
            turn['quota_pct_5h'] = round(turn_quota, 6) if turn['quota_known'] else None
            turn['quota_sources'] = sorted(turn_sources)

        # Legacy/manual fixtures may not contain turn details.
        if not mission_models:
            for model_usage in mission.get('model_totals') or []:
                if not isinstance(model_usage, dict):
                    continue
                model_key = str(model_usage.get('model_key') or '')
                tokens = _safe_nonnegative_int(model_usage.get('total_tokens'))
                rate = rates.get(model_key)
                known = bool(rate and tokens > 0)
                quota_pct = tokens / rate['tokens_per_quota_pct'] if known else None
                if tokens > 0:
                    had_contribution = True
                mission_models[model_key] = {
                    'model_key': model_key,
                    'total_tokens': tokens,
                    'quota_pct_5h': quota_pct or 0.0,
                    'quota_known': known,
                    'quota_sources': {'calibrated_model'} if known else set(),
                    'quota_confidences': [rate['confidence']] if known else [],
                }
                if known:
                    mission_sources.add('calibrated_model')
                    mission_confidences.append(rate['confidence'])
                else:
                    all_known = False

        quota_model_totals = []
        for target in mission_models.values():
            confidences = target.pop('quota_confidences')
            sources = target.pop('quota_sources')
            target['quota_pct_5h'] = round(target['quota_pct_5h'], 6) if target['quota_known'] else None
            target['quota_sources'] = sorted(sources)
            target['quota_confidence'] = min(
                confidences or ['low'], key=lambda value: confidence_rank.get(value, 0)
            )
            quota_model_totals.append(target)
        quota_model_totals.sort(key=lambda item: -_safe_nonnegative_int(item.get('total_tokens')))
        mission['quota_model_totals'] = quota_model_totals
        mission['quota_known'] = bool(had_contribution and all_known and quota_model_totals)
        mission['quota_pct_5h'] = (
            round(sum(float(item.get('quota_pct_5h') or 0.0) for item in quota_model_totals), 6)
            if mission['quota_known'] else None
        )
        mission['quota_sources'] = sorted(mission_sources)
        mission['quota_confidence'] = min(
            mission_confidences or ['low'], key=lambda value: confidence_rank.get(value, 0)
        )
        mission['repair_penalty_received_quota_pct_5h'] = 0.0
        mission['penalized_quota_pct_5h'] = mission.get('quota_pct_5h')

    # Convert the already-built cumulative repair events to quota using the
    # repair mission's own model-specific quota estimates.
    for source in missions or []:
        for event in source.get('repair_penalty_events') or []:
            if not isinstance(event, dict):
                continue
            repair = mission_by_id.get(str(event.get('repair_mission_id') or ''))
            applied_model = str(event.get('applied_to_model') or '')
            repair_quota = {}
            repair_known = True
            if repair is None:
                repair_known = False
            else:
                for item in repair.get('quota_model_totals') or []:
                    model_key = str(item.get('model_key') or '')
                    if not model_key or model_key == applied_model:
                        continue
                    if not item.get('quota_known'):
                        repair_known = False
                        continue
                    repair_quota[model_key] = float(item.get('quota_pct_5h') or 0.0)
            penalty_quota = sum(repair_quota.values()) if repair_known else None
            event['repair_model_quota_pct_5h'] = repair_quota
            event['quota_known'] = repair_known
            event['penalty_quota_pct_5h'] = round(penalty_quota, 6) if penalty_quota is not None else None
            if penalty_quota is not None:
                source['repair_penalty_received_quota_pct_5h'] = round(
                    float(source.get('repair_penalty_received_quota_pct_5h') or 0.0) + penalty_quota,
                    6,
                )
        if source.get('quota_known'):
            source['penalized_quota_pct_5h'] = round(
                float(source.get('quota_pct_5h') or 0.0) +
                float(source.get('repair_penalty_received_quota_pct_5h') or 0.0),
                6,
            )


def _codex_collect_review_cases(missions):
    cases = []
    for mission in missions or []:
        if str(mission.get('status') or '').startswith('excluded'):
            mission['needs_category_review'] = False
            mission['needs_review'] = False
            mission['category_review_reasons'] = []
            continue
        reasons = []
        hard_turns = []
        category_reviewed = bool(mission.get('category_reviewed'))
        if not category_reviewed:
            if mission.get('category') == 'other':
                reasons.append('other_category')
            if str(mission.get('category_confidence') or 'low') == 'low':
                reasons.append('low_category_confidence')
        turns = mission.get('turns') or []
        if not turns:
            reasons.append('legacy_no_turn_detail')
        if not category_reviewed:
            for index, turn in enumerate(turns):
                turn_reasons = []
                # A tied score between multiple explicit task categories is not, by
                # itself, a review problem.  Multi-label classification is an
                # intentional output and the matrix can attribute the turn across
                # those categories without forcing a human to choose one winner.
                # Keep category_ambiguous on the turn as provenance, but reserve the
                # review queue for genuinely weak/unknown classifications.
                if str(turn.get('category_confidence') or 'low') == 'low':
                    turn_reasons.append('low_confidence')
                if turn.get('category') == 'other':
                    turn_reasons.append('other_category')
                if turn_reasons:
                    hard_turns.append({
                        'turn_id': turn.get('turn_id'),
                        'turn_index': index,
                        'categories': list(turn.get('categories') or []),
                        'reasons': turn_reasons,
                        'prompt': turn.get('prompt') or '',
                    })
        if hard_turns:
            reasons.append('hard_turns')
        if (mission.get('repair_of_mission_id') and
                mission.get('repair_link_mode') == 'auto' and
                mission.get('repair_link_confidence') == 'low'):
            reasons.append('low_repair_link_confidence')
        reasons = list(dict.fromkeys(reasons))
        mission['needs_category_review'] = bool(reasons)
        mission['needs_review'] = bool(reasons)
        mission['category_review_reasons'] = reasons
        if reasons:
            cases.append({
                'mission_id': mission.get('id'),
                'anchor_turn_id': mission.get('anchor_turn_id'),
                'title': mission.get('title'),
                'category': mission.get('category'),
                'categories': list(mission.get('categories') or []),
                'category_confidence': mission.get('category_confidence'),
                'reasons': reasons,
                'hard_turns': hard_turns,
            })
    return cases


def _codex_collect_audit_cases(missions):
    """Surface uncertain automatic decisions without reopening valid classifications.

    `needs_review` is intentionally reserved for weak/unknown classifications.
    Multi-category work, delayed continuations, and model changes are supported
    mission shapes.  The soft layer is therefore limited to an automatic repair
    link whose target is still uncertain.
    """
    cases = []
    grouped = {}
    patterns = {}
    reason_weights = {
        'hard_review': 4,
        'auto_repair_link_not_high': 2,
    }

    for mission in missions or []:
        if str(mission.get('status') or '').startswith('excluded'):
            mission['audit_needed'] = False
            mission['soft_audit_needed'] = False
            mission['audit_score'] = 0
            mission['audit_priority'] = None
            mission['audit_reasons'] = []
            mission['audit_turns'] = []
            mission['audit_pattern_key'] = ''
            continue
        reasons = []
        audit_turns = []
        turns = mission.get('turns') or []
        categories = list(dict.fromkeys(
            key for key in (mission.get('categories') or [mission.get('category')])
            if key in CODEX_TASK_CATEGORY_KEYS
        ))

        if mission.get('needs_review') or mission.get('needs_category_review'):
            reasons.append('hard_review')

        if (mission.get('repair_of_mission_id') and
                mission.get('repair_link_mode') == 'auto' and
                str(mission.get('repair_link_confidence') or 'low') != 'high'):
            reasons.append('auto_repair_link_not_high')

        reasons = list(dict.fromkeys(reasons))
        soft_reasons = [reason for reason in reasons if reason != 'hard_review']
        soft_audit_needed = bool(soft_reasons) and 'hard_review' not in reasons
        pattern_key = '+'.join(soft_reasons) if soft_audit_needed else ''
        score = sum(reason_weights.get(reason, 1) for reason in reasons)
        mission['audit_needed'] = bool(reasons)
        mission['soft_audit_needed'] = soft_audit_needed
        mission['audit_reasons'] = reasons
        mission['audit_pattern_key'] = pattern_key
        mission['audit_score'] = score
        mission['audit_priority'] = 'high' if score >= 5 else ('medium' if score >= 3 else ('low' if score else 'none'))
        mission['audit_turns'] = audit_turns

        if not reasons:
            continue
        case = {
            'mission_id': mission.get('id'),
            'anchor_turn_id': mission.get('anchor_turn_id'),
            'title': mission.get('title'),
            'category': mission.get('category'),
            'categories': categories,
            'category_confidence': mission.get('category_confidence'),
            'score': score,
            'priority': mission['audit_priority'],
            'reasons': reasons,
            'soft_audit_needed': soft_audit_needed,
            'pattern_key': pattern_key,
            'audit_turns': audit_turns,
        }
        cases.append(case)
        for reason in reasons:
            group = grouped.setdefault(reason, {
                'reason': reason,
                'mission_count': 0,
                'max_score': 0,
                'samples': [],
            })
            group['mission_count'] += 1
            group['max_score'] = max(group['max_score'], score)
            if len(group['samples']) < 8:
                group['samples'].append({
                    'mission_id': mission.get('id'),
                    'anchor_turn_id': mission.get('anchor_turn_id'),
                    'title': mission.get('title'),
                    'categories': categories,
                    'score': score,
                })

        if pattern_key:
            pattern = patterns.setdefault(pattern_key, {
                'key': pattern_key,
                'reasons': list(soft_reasons),
                'mission_count': 0,
                'max_score': 0,
                'samples': [],
            })
            pattern['mission_count'] += 1
            pattern['max_score'] = max(pattern['max_score'], score)
            if len(pattern['samples']) < 8:
                pattern['samples'].append({
                    'mission_id': mission.get('id'),
                    'anchor_turn_id': mission.get('anchor_turn_id'),
                    'title': mission.get('title'),
                    'categories': categories,
                    'score': score,
                })

    cases.sort(key=lambda item: (-item['score'], str(item.get('title') or '')))
    groups = sorted(grouped.values(), key=lambda item: (-item['max_score'], -item['mission_count'], item['reason']))
    pattern_groups = sorted(
        patterns.values(),
        key=lambda item: (-item['max_score'], -item['mission_count'], item['key']),
    )
    return cases, groups, pattern_groups


def _codex_unconfirmed_review_group(mission):
    """Group unresolved missions for review without changing their classification."""
    prompts = [str(turn.get('prompt') or '') for turn in (mission.get('turns') or [])]
    text = _codex_plain_text(' '.join(prompts) or mission.get('title') or '')
    categories = set(mission.get('categories') or [])

    # A direct reference to the tracker is stronger than generic words such as
    # "token", "Astra", or "Codex".  Those generic words also occur in model
    # advice, runtime debugging, document conversion, and SU-30 work, so using
    # them as standalone tracker signals makes the review queue misleading.
    usage_tracker_phrases = (
        'usage tracker', 'model breakdown', 'codex usage',
        'bieu do xu huong tieu thu token', 'bieu do xu huong chi phi',
        'ma tran tong token', 'ma tran hao phi han muc',
        'thong ke token 7 ngay',
    )
    if any(phrase in text for phrase in usage_tracker_phrases):
        topic_key, topic_label = 'usage_tracker', 'Usage Tracker / model / hạn mức'
    else:
        topic_key = None
        topic_label = None

    topic_rules = (
        ('gbpusd', 'GBPUSD / giao dịch', (
            'gbpusd', 'trading', 'trade', 'tp2', 'tp3', 'pullback',
            'trailing', 'backtest', 'fxreplay', 'forex factory', 'nen m1',
        )),
        ('su30', 'SU-30 / tài liệu kỹ thuật', (
            'su-30', 'su30', 'buong lai', 'thuy luc', 'dong co',
            'may bay', 'tai lieu su30',
        )),
        ('personnel', 'Hồ sơ nhân sự / tài liệu', (
            'dang vien', 'ho so', 'dong chi', 'li lich', 'cmnd',
            'cap hoc', 'chinh tri vien',
        )),
        ('codex_runtime', 'Runtime / lỗi phần mềm Codex', (
            'codex-chatgpt-web', 'bug minimize', 'prompt insertion',
            'electron', 'diagnostic patch', 'source map', 'stream disconnected',
            'context 80k', 'native2', 'auto review', 'config.toml',
            'model picker', 'model provider', 'codex workflow',
        )),
        ('device_network', 'Máy tính / điện thoại / mạng', (
            'xiaomi', 'sac nhanh', 'gpu', 'quat lam mat', 'den man hinh',
            'vpn', 'nha mang', 'toc do mang', 'virus',
        )),
        ('product_model', 'Câu hỏi sản phẩm / model', (
            'chatgpt plus', 'chatgpt go', 'codex free', 'thue bao',
            'context window', '372k', 'gemini 3.5', 'model 5.5', 'model 5.4',
            'gpt reverse', 'astra', '5.6 sol', 'luna', 'muse',
        )),
        ('usage_tracker', 'Usage Tracker / model / hạn mức', (
            'han muc codex', 'quota codex', 'rate limit', 'khung 5 gio',
            'han muc 5h', 'han muc tuan', 'reset han muc codex',
        )),
    )
    if topic_key is None:
        for candidate_key, candidate_label, phrases in topic_rules:
            if any(phrase in text for phrase in phrases):
                topic_key, topic_label = candidate_key, candidate_label
                break
    if topic_key is None:
        fallback = (
            ('documents', 'Tài liệu / bảng biểu khác'),
            ('software_debugging', 'Chẩn đoán / phần mềm khác'),
            ('system_diagnostics', 'Chẩn đoán / phần mềm khác'),
            ('research', 'Nghiên cứu khác'),
            ('web', 'Làm web khác'),
        )
        topic_key, topic_label = next(
            ((key, label) for key, label in fallback if key in categories),
            ('other', 'Khác / chưa đủ ngữ cảnh'),
        )

    first = _codex_plain_text(prompts[0] if prompts else mission.get('title') or '')
    action_phrases = (
        'sua ', 'lam ', 'kiem tra', 'tim ', 'tao ', 'cai ', 'chinh ',
        'xuat ', 'doc ', 'thuc hien', 'toi uu', 'chay ', 'tiep tuc',
        'cap nhat', 'dieu tra',
    )
    question_phrases = (
        '?', 'toi muon hoi', 'toi hoi', 'tai sao', 'vi sao', 'co phai',
        'duoc ko', 'duoc khong', 'nhu the nao', 'khac nhau', 'so voi', 'co the',
    )
    if re.match(r'^(?:ok[ ,]*|the |gio )?(?:ban )?(?:lam|tiep tuc|chay) ', first):
        form_key, form_label = 'continuation', 'Lệnh tiếp tục thiếu tên việc'
    elif any(phrase in text for phrase in question_phrases) and not any(
            phrase in text for phrase in action_phrases):
        form_key, form_label = 'question', 'Câu hỏi / tư vấn'
    else:
        form_key, form_label = 'action', 'Nhiệm vụ hành động / điều tra'
    return {
        'key': f'{topic_key}:{form_key}',
        'topic_key': topic_key,
        'topic_label': topic_label,
        'form_key': form_key,
        'form_label': form_label,
        'label': f'{topic_label} · {form_label}',
    }


def _codex_collect_unconfirmed_groups(missions):
    grouped = {}
    for mission in missions or []:
        if not str(mission.get('status') or '').startswith('unresolved'):
            mission['unconfirmed_group_key'] = ''
            continue
        group = _codex_unconfirmed_review_group(mission)
        mission['unconfirmed_group_key'] = group['key']
        target = grouped.setdefault(group['key'], {
            **group,
            'mission_count': 0,
            'anchor_turn_ids': [],
            'samples': [],
        })
        target['mission_count'] += 1
        target['anchor_turn_ids'].append(mission.get('anchor_turn_id'))
        if len(target['samples']) < 4:
            target['samples'].append({
                'anchor_turn_id': mission.get('anchor_turn_id'),
                'title': mission.get('title'),
                'categories': list(mission.get('categories') or []),
                'turn_count': mission.get('turn_count'),
            })
    return sorted(
        grouped.values(),
        key=lambda item: (-item['mission_count'], item['topic_label'], item['form_label']),
    )


def _codex_matrix_attributed_samples(missions, now=None):
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    usable = [mission for mission in missions if _codex_mission_usable_for_matrix(mission)]
    synthetic_ids = {
        id(mission): f'legacy-mission-{index}'
        for index, mission in enumerate(missions)
    }

    def mission_identity(mission):
        return str(
            mission.get('id') or mission.get('anchor_turn_id') or synthetic_ids[id(mission)]
        )

    mission_by_id = {
        mission_identity(mission): mission for mission in missions
    }
    attributed = {}

    def mission_categories(mission):
        return list(dict.fromkeys(
            key for key in (mission.get('categories') or [mission.get('category') or 'other'])
            if key in CODEX_TASK_CATEGORY_KEYS
        )) or ['other']

    def attribution_root(mission):
        current = mission
        seen = set()
        while current is not None:
            current_id = mission_identity(current)
            if current_id in seen:
                break
            seen.add(current_id)
            repair_of = str(current.get('repair_of_mission_id') or '')
            if not repair_of or repair_of == current_id or repair_of not in mission_by_id:
                break
            current = mission_by_id[repair_of]
        return current or mission

    def add_contribution(root, category, model_key, raw_tokens=0.0, cost_usd=0.0,
                         cost_known=True, repair_penalty_tokens=0.0,
                         raw_quota_pct_5h=None, quota_known=False,
                         quota_source=None, repair_penalty_quota_pct_5h=None,
                         category_split=False, repair_attributed=False,
                         status=None, accepted=None):
        if category not in CODEX_TASK_CATEGORY_KEYS or not model_key:
            return
        tokens = float(raw_tokens or 0.0)
        penalty = float(repair_penalty_tokens or 0.0)
        quota_penalty = (
            float(repair_penalty_quota_pct_5h)
            if repair_penalty_quota_pct_5h is not None else None
        )
        if tokens <= 0 and penalty <= 0 and not (quota_penalty and quota_penalty > 0):
            return
        root_id = mission_identity(root)
        key = (root_id, category, model_key)
        sample = attributed.setdefault(key, {
            'mission_id': root_id,
            'anchor_turn_id': root.get('anchor_turn_id'),
            'category': category,
            'model_key': model_key,
            'raw_total_tokens': 0.0,
            'repair_penalty_tokens': 0.0,
            'total_tokens': 0.0,
            'raw_quota_pct_5h': 0.0,
            'repair_penalty_quota_pct_5h': 0.0,
            'total_quota_pct_5h': 0.0,
            'quota_known': True,
            'quota_sources': [],
            'cost_known': True,
            'cost_usd': 0.0,
            'correction_turns': root.get('correction_turns', 0),
            'added_guidance_tokens_est': root.get('added_guidance_tokens_est', 0),
            'first_pass_success': root.get('first_pass_success', False),
            'status': status if status is not None else root.get('status'),
            'accepted': bool(accepted) if accepted is not None else root.get('accepted', False),
            # "Unconfirmed" and "needs review" are deliberately separate.
            # An unresolved mission is still a usable matrix sample, with
            # lower confidence, but it should only count as pending review
            # when the review classifier found a concrete reason.
            'unconfirmed': not (
                bool(accepted) if accepted is not None else bool(root.get('accepted'))
            ),
            'pending_review': bool(
                root.get('needs_review') or root.get('needs_category_review')
            ),
            'category_split': False,
            'repair_attributed': False,
            'start_at': root.get('start_at'),
        })
        sample['raw_total_tokens'] += tokens
        sample['repair_penalty_tokens'] += penalty
        sample['total_tokens'] = sample['raw_total_tokens'] + sample['repair_penalty_tokens']
        if raw_quota_pct_5h is not None:
            sample['raw_quota_pct_5h'] += float(raw_quota_pct_5h)
        if quota_penalty is not None:
            sample['repair_penalty_quota_pct_5h'] += quota_penalty
        sample['total_quota_pct_5h'] = (
            sample['raw_quota_pct_5h'] + sample['repair_penalty_quota_pct_5h']
        )
        sample['quota_known'] = bool(sample['quota_known']) and bool(quota_known)
        if quota_source and quota_source not in sample['quota_sources']:
            sample['quota_sources'].append(quota_source)
        sample['cost_usd'] += float(cost_usd or 0.0)
        sample['cost_known'] = bool(sample['cost_known']) and bool(cost_known)
        sample['category_split'] = bool(sample['category_split'] or category_split)
        sample['repair_attributed'] = bool(sample['repair_attributed'] or repair_attributed or penalty > 0)
        if accepted:
            sample['status'] = status or sample.get('status')
            sample['accepted'] = True
            sample['unconfirmed'] = False
        if sample['repair_attributed']:
            sample['first_pass_success'] = False
            sample['correction_turns'] = max(1, _safe_nonnegative_int(sample.get('correction_turns')))

    accepted_repair_roots = {}
    for mission in usable:
        root = attribution_root(mission)
        base_categories = mission_categories(mission)
        repair_of = str(mission.get('repair_of_mission_id') or '')
        mission_id = mission_identity(mission)
        separate_repair = bool(repair_of and repair_of != mission_id and repair_of in mission_by_id)
        if separate_repair:
            repair_categories = [
                key for key in (mission.get('repair_penalty_categories') or mission_categories(root))
                if key in CODEX_TASK_CATEGORY_KEYS
            ]
            base_categories = list(dict.fromkeys(repair_categories)) or mission_categories(root)
        category_reviewed = bool(mission.get('category_reviewed')) and not separate_repair
        contribution_count = 0

        mission_turns = list(mission.get('turns') or [])
        skipped_provisional_tail = False
        for turn_index, turn in enumerate(mission_turns):
            if not isinstance(turn, dict):
                continue
            # The session log exposes token movement while the current answer is
            # still being generated.  Keep interrupted historical attempts once
            # a later turn exists, but do not let the live unfinished tail move a
            # completed-mission median before that answer has finished.
            started_at = _parse_iso_utc(turn.get('started_at'))
            provisional_tail = bool(
                turn_index == len(mission_turns) - 1 and
                turn.get('completed') is False and
                started_at is not None and
                timedelta(0) <= now_utc - started_at <= timedelta(hours=6)
            )
            if provisional_tail:
                skipped_provisional_tail = True
                continue
            turn_tokens = _safe_nonnegative_int(turn.get('total_tokens'))
            if turn_tokens <= 0:
                continue
            if separate_repair:
                categories = list(base_categories)
            else:
                turn_categories = [
                    key for key in (turn.get('categories') or [turn.get('category') or base_categories[0]])
                    if key in CODEX_TASK_CATEGORY_KEYS
                ]
                if category_reviewed:
                    selected = [key for key in turn_categories if key in base_categories]
                    categories = selected or list(base_categories)
                else:
                    categories = turn_categories or list(base_categories)
            categories = list(dict.fromkeys(categories)) or ['other']
            category_weight = 1.0 / len(categories)
            model_totals = [
                item for item in (turn.get('model_totals') or [])
                if isinstance(item, dict) and _safe_nonnegative_int(item.get('total_tokens')) > 0
            ]
            if not model_totals:
                fallback_model = str(turn.get('model_key') or mission.get('model_key') or '')
                if fallback_model:
                    model_totals = [{
                        'model_key': fallback_model,
                        'total_tokens': turn_tokens,
                        'cost_known': turn.get('cost_known') is not False,
                        'cost_usd': float(turn.get('cost_usd') or 0.0),
                    }]
            for model_usage in model_totals:
                model_key = str(model_usage.get('model_key') or '')
                model_tokens = _safe_nonnegative_int(model_usage.get('total_tokens'))
                if not model_key or model_tokens <= 0:
                    continue
                model_cost = float(model_usage.get('cost_usd') or 0.0)
                model_quota = model_usage.get('quota_pct_5h')
                model_quota_known = bool(model_usage.get('quota_known'))
                for category in categories:
                    add_contribution(
                        root, category, model_key,
                        raw_tokens=model_tokens * category_weight,
                        cost_usd=model_cost * category_weight,
                        cost_known=model_usage.get('cost_known') is not False,
                        raw_quota_pct_5h=(
                            float(model_quota) * category_weight
                            if model_quota_known and model_quota is not None else None
                        ),
                        quota_known=model_quota_known,
                        quota_source=model_usage.get('quota_source'),
                        category_split=len(categories) > 1,
                        repair_attributed=separate_repair,
                        status=mission.get('status'),
                        accepted=mission.get('accepted'),
                    )
                    contribution_count += 1

        # Legacy/manual mission fixtures may not contain turn detail.
        if contribution_count == 0 and not skipped_provisional_tail:
            category_weight = 1.0 / len(base_categories)
            model_totals = [
                item for item in (mission.get('quota_model_totals') or mission.get('model_totals') or [])
                if isinstance(item, dict) and _safe_nonnegative_int(item.get('total_tokens')) > 0
            ]
            if not model_totals and mission.get('model_key'):
                model_totals = [{
                    'model_key': mission.get('model_key'),
                    'total_tokens': _safe_nonnegative_int(mission.get('total_tokens')),
                    'cost_known': mission.get('cost_known') is not False,
                    'cost_usd': float(mission.get('cost_usd') or 0.0),
                }]
            for model_usage in model_totals:
                model_key = str(model_usage.get('model_key') or '')
                model_tokens = _safe_nonnegative_int(model_usage.get('total_tokens'))
                if not model_key or model_tokens <= 0:
                    continue
                model_cost = float(model_usage.get('cost_usd') or 0.0)
                model_quota = model_usage.get('quota_pct_5h')
                model_quota_known = bool(model_usage.get('quota_known'))
                for category in base_categories:
                    add_contribution(
                        root, category, model_key,
                        raw_tokens=model_tokens * category_weight,
                        cost_usd=model_cost * category_weight,
                        cost_known=model_usage.get('cost_known') is not False,
                        raw_quota_pct_5h=(
                            float(model_quota) * category_weight
                            if model_quota_known and model_quota is not None else None
                        ),
                        quota_known=model_quota_known,
                        quota_source=(model_usage.get('quota_sources') or ['calibrated_model'])[0]
                        if model_quota_known else None,
                        category_split=len(base_categories) > 1,
                        repair_attributed=separate_repair,
                        status=mission.get('status'),
                        accepted=mission.get('accepted'),
                    )

        # Acceptance belongs to the completed objective, not only to the model
        # used by the final repair. Mark every model contribution under the
        # same repair root as accepted once the terminal repair is accepted.
        if separate_repair and mission.get('accepted'):
            root_id = mission_identity(root)
            accepted_repair_roots[root_id] = mission.get('status')
            for sample_key, sample in attributed.items():
                if sample_key[0] != root_id:
                    continue
                sample['status'] = mission.get('status')
                sample['accepted'] = True
                sample['unconfirmed'] = False

    # Missions are not guaranteed to be ordered root-first. A repaired root
    # can therefore add another model contribution after the accepted repair
    # was processed; normalize every contribution after the first pass.
    for sample_key, sample in attributed.items():
        accepted_status = accepted_repair_roots.get(sample_key[0])
        if accepted_status is None:
            continue
        sample['status'] = accepted_status
        sample['accepted'] = True
        sample['unconfirmed'] = False

    # A cross-model repair is real usage for the new model and an explicit
    # penalty for the model whose output had to be repaired.  Add that penalty
    # to the same completed-objective sample; do not create an extra sample.
    for source in usable:
        root = attribution_root(source)
        for event in source.get('repair_penalty_events') or []:
            if not isinstance(event, dict):
                continue
            model_key = str(event.get('applied_to_model') or '')
            penalty = _safe_nonnegative_int(event.get('penalty_tokens'))
            penalty_quota = event.get('penalty_quota_pct_5h')
            penalty_quota_known = bool(event.get('quota_known'))
            categories = [
                key for key in (event.get('categories') or mission_categories(root))
                if key in CODEX_TASK_CATEGORY_KEYS
            ]
            categories = list(dict.fromkeys(categories)) or mission_categories(root)
            category_weight = 1.0 / len(categories)
            for category in categories:
                add_contribution(
                    root, category, model_key,
                    repair_penalty_tokens=penalty * category_weight,
                    repair_penalty_quota_pct_5h=(
                        float(penalty_quota) * category_weight
                        if penalty_quota_known and penalty_quota is not None else None
                    ),
                    quota_known=penalty_quota_known,
                    quota_source='repair_chain',
                    cost_known=False,
                    category_split=len(categories) > 1,
                    repair_attributed=True,
                )

    return sorted(
        attributed.values(),
        key=lambda item: (str(item.get('start_at') or ''), item['mission_id'], item['category'], item['model_key']),
    )


def _codex_task_matrix(missions, range_days, now_utc):
    cutoff = None if range_days is None else now_utc - timedelta(days=range_days)
    bridge_missions = [mission for mission in missions if _codex_mission_usable_for_matrix(mission)]
    bridge_eligible = _codex_matrix_attributed_samples(bridge_missions, now=now_utc)
    eligible = []
    for sample in bridge_eligible:
        started = _parse_iso_utc(sample.get('start_at'))
        if cutoff is not None and (started is None or started < cutoff):
            continue
        eligible.append(sample)
    eligible_mission_ids = {
        sample.get('mission_id') for sample in eligible if sample.get('mission_id')
    }

    by_cell = {}
    model_counts = {}
    for sample in eligible:
        key = (sample.get('category') or 'other', sample.get('model_key'))
        by_cell.setdefault(key, []).append(sample)
        model_key = sample.get('model_key')
        if model_key:
            model_counts[model_key] = model_counts.get(model_key, 0) + 1
    historical_by_cell = {}
    for sample in bridge_eligible:
        key = (sample.get('category') or 'other', sample.get('model_key'))
        historical_by_cell.setdefault(key, []).append(sample)
    models = sorted(model_counts, key=lambda model: (0 if _is_sol_high_baseline(model) else 1,
                                                     -model_counts[model], model))
    historical_models = sorted({
        sample.get('model_key') for sample in bridge_eligible if sample.get('model_key')
    })
    baseline_model = next((model for model in models if _is_sol_high_baseline(model)), None)
    if baseline_model is None:
        baseline_model = next((model for model in historical_models if _is_sol_high_baseline(model)), None)
        if baseline_model:
            models = [baseline_model] + [model for model in models if model != baseline_model]

    bridge_contexts = {}
    for category in CODEX_TASK_CATEGORIES:
        category_key = category['key']
        values = {}
        for model in historical_models:
            samples = historical_by_cell.get((category_key, model), [])
            if len(samples) < 3:
                continue
            median_tokens = _median_numeric(item.get('total_tokens') for item in samples)
            if median_tokens and median_tokens > 0:
                values[model] = median_tokens
        if len(values) >= 2:
            bridge_contexts[category_key] = values
    bridge_graph = _codex_ratio_bridge_graph(bridge_contexts)
    quota_bridge_contexts = {}
    for category in CODEX_TASK_CATEGORIES:
        category_key = category['key']
        values = {}
        for model in historical_models:
            quota_samples = [
                item for item in historical_by_cell.get((category_key, model), [])
                if item.get('quota_known') and float(item.get('total_quota_pct_5h') or 0.0) > 0
            ]
            if len(quota_samples) < 3:
                continue
            median_quota = _median_numeric(item.get('total_quota_pct_5h') for item in quota_samples)
            if median_quota and median_quota > 0:
                values[model] = median_quota
        if len(values) >= 2:
            quota_bridge_contexts[category_key] = values
    quota_bridge_graph = _codex_ratio_bridge_graph(quota_bridge_contexts)
    if models and bridge_graph:
        current_models = list(models)
        historical_counts = {}
        for sample in bridge_eligible:
            model_key = sample.get('model_key')
            if model_key:
                historical_counts[model_key] = historical_counts.get(model_key, 0) + 1
        connected_historical = []
        for candidate in historical_models:
            if candidate in models:
                continue
            if any(
                _codex_find_bridge_ratio(bridge_graph, anchor, candidate)
                for anchor in current_models if anchor != candidate
            ):
                connected_historical.append(candidate)
        connected_historical.sort(key=lambda model: (-historical_counts.get(model, 0), model))
        models.extend(connected_historical)
    rows = []
    for category in CODEX_TASK_CATEGORIES:
        category_key = category['key']
        direct_values = {}
        observed_values = {}
        quota_direct_values = {}
        quota_observed_values = {}
        for model in models:
            direct_samples = by_cell.get((category_key, model), [])
            direct_median = _median_numeric(item.get('total_tokens') for item in direct_samples)
            if direct_samples and direct_median and direct_median > 0:
                observed_values[model] = direct_median
            if len(direct_samples) >= 3 and direct_median and direct_median > 0:
                direct_values[model] = direct_median
            known_quota_samples = [
                item for item in direct_samples
                if item.get('quota_known') and float(item.get('total_quota_pct_5h') or 0.0) > 0
            ]
            quota_median = _median_numeric(item.get('total_quota_pct_5h') for item in known_quota_samples)
            if known_quota_samples and quota_median and quota_median > 0:
                quota_observed_values[model] = quota_median
            if len(known_quota_samples) >= 3 and quota_median and quota_median > 0:
                quota_direct_values[model] = quota_median

        baseline_samples = by_cell.get((category_key, baseline_model), []) if baseline_model else []
        baseline_median = direct_values.get(baseline_model) if baseline_model else None
        baseline_source = 'direct' if baseline_median and baseline_median > 0 else 'unavailable'
        baseline_estimate = None
        if baseline_model and baseline_source != 'direct' and (direct_values or observed_values):
            baseline_estimate = _codex_estimate_baseline_from_anchors(
                direct_values, bridge_graph, baseline_model
            )
            if baseline_estimate is None and observed_values:
                baseline_estimate = _codex_estimate_baseline_from_anchors(
                    observed_values, bridge_graph, baseline_model
                )
            if baseline_estimate:
                baseline_anchor = ((baseline_estimate.get('representative') or {}).get('anchor_model'))
                baseline_anchor_count = len(by_cell.get((category_key, baseline_anchor), [])) if baseline_anchor else 0
                baseline_estimate['anchor_sample_count'] = baseline_anchor_count
                if baseline_anchor_count < 3:
                    baseline_estimate['confidence'] = 'low'
            if baseline_estimate and baseline_estimate.get('value', 0) > 0:
                baseline_median = float(baseline_estimate['value'])
                baseline_source = 'bridge'
        baseline_quota_samples = [
            item for item in by_cell.get((category_key, baseline_model), [])
            if item.get('quota_known') and float(item.get('total_quota_pct_5h') or 0.0) > 0
        ] if baseline_model else []
        baseline_quota_median = quota_direct_values.get(baseline_model) if baseline_model else None
        baseline_quota_source = 'direct' if baseline_quota_median and baseline_quota_median > 0 else 'unavailable'
        if baseline_model and baseline_quota_source == 'unavailable':
            sparse_baseline_quota = quota_observed_values.get(baseline_model)
            if sparse_baseline_quota and sparse_baseline_quota > 0:
                baseline_quota_median = sparse_baseline_quota
                baseline_quota_source = 'sparse_direct'
        baseline_quota_estimate = None
        if (baseline_model and baseline_quota_source == 'unavailable' and
                (quota_direct_values or quota_observed_values)):
            baseline_quota_estimate = _codex_estimate_baseline_from_anchors(
                quota_direct_values, quota_bridge_graph, baseline_model
            )
            if baseline_quota_estimate is None and quota_observed_values:
                baseline_quota_estimate = _codex_estimate_baseline_from_anchors(
                    quota_observed_values, quota_bridge_graph, baseline_model
                )
            if baseline_quota_estimate and baseline_quota_estimate.get('value', 0) > 0:
                baseline_quota_median = float(baseline_quota_estimate['value'])
                baseline_quota_source = 'bridge'
        cells = {}
        for model in models:
            samples = by_cell.get((category_key, model), [])
            count = len(samples)
            mission_count = len({item.get('mission_id') for item in samples if item.get('mission_id')})
            pending_count = sum(1 for item in samples if item.get('pending_review'))
            unconfirmed_count = sum(1 for item in samples if item.get('unconfirmed'))
            split_count = sum(1 for item in samples if item.get('category_split'))
            repair_count = sum(1 for item in samples if item.get('repair_attributed'))
            median_tokens = _median_numeric([item['total_tokens'] for item in samples])
            median_raw_tokens = _median_numeric([item.get('raw_total_tokens') for item in samples])
            median_repair_penalty = _median_numeric([item.get('repair_penalty_tokens') for item in samples])
            quota_samples = [
                item for item in samples
                if item.get('quota_known') and float(item.get('total_quota_pct_5h') or 0.0) > 0
            ]
            quota_count = len(quota_samples)
            median_quota = _median_numeric(item.get('total_quota_pct_5h') for item in quota_samples)
            median_raw_quota = _median_numeric(item.get('raw_quota_pct_5h') for item in quota_samples)
            median_repair_penalty_quota = _median_numeric(
                item.get('repair_penalty_quota_pct_5h') for item in quota_samples
            )
            known_costs = [item['cost_usd'] for item in samples if item.get('cost_known')]
            median_cost = _median_numeric(known_costs) if len(known_costs) == count else None
            median_corrections = _median_numeric([item['correction_turns'] for item in samples])
            median_guidance = _median_numeric([item['added_guidance_tokens_est'] for item in samples])
            first_pass_pct = (100.0 * sum(1 for item in samples if item.get('first_pass_success')) / count) if count else 0.0
            explicit_pct = (100.0 * sum(1 for item in samples if item.get('status') in ('accepted_explicit', 'accepted_manual')) / count) if count else 0.0
            direct_ready = count >= 3 and median_tokens and median_tokens > 0
            sparse_direct = bool(count and median_tokens and median_tokens > 0 and not direct_ready)
            estimate = None
            estimated_tokens = None
            if not direct_ready and not sparse_direct and (direct_values or observed_values):
                estimate = _codex_estimate_baseline_from_anchors(
                    direct_values, bridge_graph, model, prefer_excluding=model
                )
                if estimate is None and observed_values:
                    estimate = _codex_estimate_baseline_from_anchors(
                        observed_values, bridge_graph, model, prefer_excluding=model
                    )
                if estimate:
                    estimate_anchor = ((estimate.get('representative') or {}).get('anchor_model'))
                    estimate_anchor_count = len(by_cell.get((category_key, estimate_anchor), [])) if estimate_anchor else 0
                    estimate['anchor_sample_count'] = estimate_anchor_count
                    if estimate_anchor_count < 3:
                        estimate['confidence'] = 'low'
                if estimate and estimate.get('value', 0) > 0:
                    estimated_tokens = float(estimate['value'])
            if direct_ready or sparse_direct:
                effective_tokens = float(median_tokens)
                model_value_source = 'direct' if direct_ready else 'sparse_direct'
            elif estimated_tokens is not None:
                effective_tokens = estimated_tokens
                model_value_source = 'bridge'
            else:
                effective_tokens = None
                model_value_source = 'unavailable'

            if effective_tokens and baseline_median and baseline_median > 0:
                sample_status = 'ready' if direct_ready and baseline_source == 'direct' else 'estimated'
            elif count == 0:
                sample_status = 'no_data'
            elif count < 3:
                sample_status = 'insufficient'
            else:
                sample_status = 'no_baseline'
            ratio = (effective_tokens / baseline_median) if effective_tokens and baseline_median else None
            quota_direct_ready = quota_count >= 3 and median_quota and median_quota > 0
            quota_sparse_direct = bool(quota_count and median_quota and median_quota > 0 and not quota_direct_ready)
            quota_estimate = None
            estimated_quota = None
            if (not quota_direct_ready and not quota_sparse_direct and
                    (quota_direct_values or quota_observed_values)):
                quota_estimate = _codex_estimate_baseline_from_anchors(
                    quota_direct_values, quota_bridge_graph, model, prefer_excluding=model
                )
                if quota_estimate is None and quota_observed_values:
                    quota_estimate = _codex_estimate_baseline_from_anchors(
                        quota_observed_values, quota_bridge_graph, model, prefer_excluding=model
                    )
                if quota_estimate and quota_estimate.get('value', 0) > 0:
                    estimated_quota = float(quota_estimate['value'])
            if quota_direct_ready or quota_sparse_direct:
                effective_quota = float(median_quota)
                quota_value_source = 'direct' if quota_direct_ready else 'sparse_direct'
            elif estimated_quota is not None:
                effective_quota = estimated_quota
                quota_value_source = 'bridge'
            else:
                effective_quota = None
                quota_value_source = 'unavailable'
            quota_ratio = (
                effective_quota / baseline_quota_median
                if effective_quota and baseline_quota_median else None
            )
            if effective_quota and baseline_quota_median:
                if quota_direct_ready and baseline_quota_source == 'direct':
                    quota_sample_status = 'ready'
                elif quota_value_source == 'bridge' or baseline_quota_source == 'bridge':
                    quota_sample_status = 'estimated'
                else:
                    quota_sample_status = 'sparse_direct'
            elif quota_count == 0:
                quota_sample_status = 'no_data'
            elif quota_count < 3:
                quota_sample_status = 'insufficient'
            else:
                quota_sample_status = 'no_baseline'
            if count >= 12 and explicit_pct >= 60:
                confidence = 'high'
            elif count >= 6:
                confidence = 'medium'
            else:
                confidence = 'low'
            confidence_rank = {'low': 0, 'medium': 1, 'high': 2}
            if estimate:
                confidence = min(
                    (confidence if count else 'high', estimate.get('confidence') or 'low'),
                    key=lambda value: confidence_rank.get(value, 0),
                )
            if baseline_source == 'bridge' and baseline_estimate:
                confidence = min(
                    (confidence, baseline_estimate.get('confidence') or 'low'),
                    key=lambda value: confidence_rank.get(value, 0),
                )
            if quota_estimate:
                confidence = min(
                    (confidence if quota_count else 'high', quota_estimate.get('confidence') or 'low'),
                    key=lambda value: confidence_rank.get(value, 0),
                )
            if baseline_quota_source == 'bridge' and baseline_quota_estimate:
                confidence = min(
                    (confidence, baseline_quota_estimate.get('confidence') or 'low'),
                    key=lambda value: confidence_rank.get(value, 0),
                )
            if split_count:
                confidence = 'low'
            elif repair_count:
                confidence = 'low'
            elif 0 < quota_count < 3:
                confidence = 'low'
            elif unconfirmed_count or pending_count:
                confidence = min(
                    (confidence, 'medium'),
                    key=lambda value: confidence_rank.get(value, 0),
                )
            estimate_rep = (estimate or {}).get('representative') or {}
            estimate_path = estimate_rep.get('bridge') or {}
            baseline_rep = (baseline_estimate or {}).get('representative') or {}
            baseline_path = baseline_rep.get('bridge') or {}
            quota_estimate_rep = (quota_estimate or {}).get('representative') or {}
            quota_estimate_path = quota_estimate_rep.get('bridge') or {}
            quota_baseline_rep = (baseline_quota_estimate or {}).get('representative') or {}
            quota_baseline_path = quota_baseline_rep.get('bridge') or {}
            cells[model] = {
                'sample_status': sample_status,
                'sample_count': count,
                'mission_count': mission_count,
                'pending_review_count': pending_count,
                'pending_review_pct': round(100.0 * pending_count / count, 1) if count else 0.0,
                'unconfirmed_count': unconfirmed_count,
                'unconfirmed_pct': round(100.0 * unconfirmed_count / count, 1) if count else 0.0,
                'split_attribution_count': split_count,
                'repair_attribution_count': repair_count,
                'median_total_tokens': round(median_tokens or 0),
                'median_raw_total_tokens': round(median_raw_tokens or 0),
                'median_repair_penalty_tokens': round(median_repair_penalty or 0),
                'quota_sample_status': quota_sample_status,
                'quota_sample_count': quota_count,
                'median_quota_pct_5h': round(median_quota, 4) if median_quota is not None else None,
                'median_raw_quota_pct_5h': round(median_raw_quota, 4) if median_raw_quota is not None else None,
                'median_repair_penalty_quota_pct_5h': (
                    round(median_repair_penalty_quota, 4)
                    if median_repair_penalty_quota is not None else None
                ),
                'estimated_quota_pct_5h': round(estimated_quota, 4) if estimated_quota is not None else None,
                'effective_quota_pct_5h': round(effective_quota, 4) if effective_quota is not None else None,
                'quota_value_source': quota_value_source,
                'estimated_total_tokens': round(estimated_tokens) if estimated_tokens is not None else None,
                'effective_total_tokens': round(effective_tokens) if effective_tokens is not None else None,
                'model_value_source': model_value_source,
                'median_cost_usd': round(median_cost, 4) if median_cost is not None else None,
                'cost_known_sample_count': len(known_costs),
                'median_correction_turns': round(median_corrections or 0, 1),
                'median_added_guidance_tokens': round(median_guidance or 0),
                'first_pass_pct': round(first_pass_pct, 1),
                'explicit_acceptance_pct': round(explicit_pct, 1),
                'relative_tokens_vs_sol_high': round(ratio, 3) if ratio is not None else None,
                'relative_quota_vs_sol_high': round(quota_ratio, 3) if quota_ratio is not None else None,
                'quota_baseline_source': baseline_quota_source,
                'quota_comparison_estimated': quota_sample_status == 'estimated',
                'quota_bridge_anchor_model': quota_estimate_rep.get('anchor_model'),
                'quota_bridge_path': quota_estimate_path.get('path') or [],
                'quota_bridge_hops': int(quota_estimate_path.get('hops') or 0),
                'quota_bridge_support': int(quota_estimate_path.get('support') or 0),
                'quota_baseline_bridge_anchor_model': quota_baseline_rep.get('anchor_model'),
                'quota_baseline_bridge_path': quota_baseline_path.get('path') or [],
                'quota_baseline_bridge_hops': int(quota_baseline_path.get('hops') or 0),
                'quota_baseline_bridge_support': int(quota_baseline_path.get('support') or 0),
                'confidence': confidence,
                'comparison_estimated': sample_status == 'estimated',
                'baseline_source': baseline_source,
                'bridge_anchor_model': estimate_rep.get('anchor_model'),
                'bridge_anchor_sample_count': int((estimate or {}).get('anchor_sample_count') or 0),
                'bridge_path': estimate_path.get('path') or [],
                'bridge_hops': int(estimate_path.get('hops') or 0),
                'bridge_support': int(estimate_path.get('support') or 0),
                'baseline_bridge_anchor_model': baseline_rep.get('anchor_model'),
                'baseline_bridge_anchor_sample_count': int((baseline_estimate or {}).get('anchor_sample_count') or 0),
                'baseline_bridge_path': baseline_path.get('path') or [],
                'baseline_bridge_hops': int(baseline_path.get('hops') or 0),
                'baseline_bridge_support': int(baseline_path.get('support') or 0),
            }
        rows.append({'category': category_key, 'label': category['label'], 'cells': cells})
    return {
        'range_days': range_days,
        'baseline_model': baseline_model,
        'minimum_samples': 3,
        'eligible_missions': len(eligible_mission_ids),
        'eligible_samples': len(eligible),
        'pending_review_samples': sum(1 for item in eligible if item.get('pending_review')),
        'unconfirmed_samples': sum(1 for item in eligible if item.get('unconfirmed')),
        'split_attribution_samples': sum(1 for item in eligible if item.get('category_split')),
        'repair_attribution_samples': sum(1 for item in eligible if item.get('repair_attributed')),
        'models': models,
        'rows': rows,
    }


def _attach_codex_delegated_turn(mission, turn):
    usage = turn.get('usage') if isinstance(turn.get('usage'), dict) else {}
    mission['delegated_turn_count'] = _safe_nonnegative_int(mission.get('delegated_turn_count')) + 1
    mission['delegated_tokens'] = _safe_nonnegative_int(mission.get('delegated_tokens')) + _safe_nonnegative_int(usage.get('total_tokens'))
    for field in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                  'output_tokens', 'thinking_tokens', 'total_tokens'):
        mission[field] = _safe_nonnegative_int(mission.get(field)) + _safe_nonnegative_int(usage.get(field))
    mission['cost_usd'] = round(float(mission.get('cost_usd') or 0.0) + float(usage.get('cost_usd') or 0.0), 6)
    mission['cost_known'] = bool(mission.get('cost_known')) and usage.get('cost_known') is not False

    route = list(mission.get('route') or [])
    worker_route = list(usage.get('route') or [])
    if not worker_route and turn.get('model_key') and _codex_turn_has_model_evidence(turn):
        worker_route = [turn['model_key']]
    for model_key in worker_route:
        if not route or route[-1] != model_key:
            route.append(model_key)
    mission['route'] = route
    mission['route_label'] = ' → '.join(route) if route else 'Không rõ model'
    mission['pure_model'] = False
    mission['model_key'] = None
    mission['has_delegated_work'] = True

    totals = {item.get('model_key'): dict(item) for item in mission.get('model_totals') or [] if item.get('model_key')}
    for model_usage in usage.get('models') or []:
        model_key = str(model_usage.get('model_key') or 'unknown')
        target = totals.setdefault(model_key, {'model_key': model_key, 'total_tokens': 0, 'cost_usd': 0.0})
        target['total_tokens'] = _safe_nonnegative_int(target.get('total_tokens')) + _safe_nonnegative_int(model_usage.get('total_tokens'))
        target['cost_usd'] = round(float(target.get('cost_usd') or 0.0) + float(model_usage.get('cost_usd') or 0.0), 6)
    mission['model_totals'] = sorted(totals.values(), key=lambda item: -_safe_nonnegative_int(item.get('total_tokens')))


def _codex_mission_active_minutes(mission):
    """Observed wall-clock union of completed work turns, excluding user pauses."""
    intervals = []
    for turn in mission.get('turns') or []:
        if not isinstance(turn, dict) or not turn.get('completed'):
            continue
        start = _parse_iso_utc(turn.get('started_at'))
        end = _parse_iso_utc(turn.get('completed_at'))
        if start is not None and end is not None and end > start:
            intervals.append((start, end))
    if not intervals:
        return None
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged) / 60.0


def build_codex_task_duration_timeline(missions, now=None, days=180,
                                       windows=(7, 14, 30)):
    """Rolling mean active minutes for accepted missions with measured turn times."""
    now_utc = _parse_iso_utc(now) if not isinstance(now, datetime) else now
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = max(1, int(days))
    first_day = now_utc.date() - timedelta(days=days - 1)
    daily = {}
    missing_duration = 0
    for mission in missions or []:
        if not isinstance(mission, dict) or not mission.get('accepted'):
            continue
        end = _parse_iso_utc(mission.get('end_at'))
        if end is None or not first_day <= end.date() <= now_utc.date():
            continue
        minutes = _codex_mission_active_minutes(mission)
        if minutes is None:
            missing_duration += 1
            continue
        daily.setdefault(end.date(), []).append({
            'minutes': minutes,
            'reviewed': bool(mission.get('outcome_reviewed')),
        })
    dates = [(first_day + timedelta(days=index)) for index in range(days)]
    result = {}
    for window in windows:
        width = max(1, int(window))
        points = []
        for day in dates:
            samples = [item for offset in range(width)
                       for item in daily.get(day - timedelta(days=offset), [])]
            values = [item['minutes'] for item in samples]
            points.append({
                'date': day.isoformat(),
                'mean_minutes': round(statistics.mean(values), 2) if values else None,
                'median_minutes': round(statistics.median(values), 2) if values else None,
                'task_count': len(values),
                'reviewed_count': sum(bool(item['reviewed']) for item in samples),
            })
        result[str(width)] = {'points': points}
    return {
        'available': bool(daily),
        'source': 'Local Codex completed-turn timestamps',
        'generated_at': now_utc.isoformat(),
        'dates': [day.isoformat() for day in dates],
        'windows': result,
        'task_count': sum(len(items) for items in daily.values()),
        'missing_duration_count': missing_duration,
    }


def build_codex_task_outcomes(turn_scan, recent_events, reviews=None, now=None,
                              quota_efficiency=None, quota_task_samples=None):
    now_utc = now if isinstance(now, datetime) else (_parse_iso_utc(now) if now else datetime.now(timezone.utc))
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    reviews = reviews if isinstance(reviews, dict) else load_codex_mission_reviews()
    boundaries = reviews.get('turn_boundaries') if isinstance(reviews.get('turn_boundaries'), dict) else {}
    logical_turns = list((turn_scan or {}).get('turns') or [])
    usage_by_turn = _codex_usage_by_turn(recent_events, logical_turns)

    thread_turns = {}
    delegated_turns = []
    reviewed_anchors = set(
        str(anchor) for anchor in (reviews.get('missions') or {})
        if str(anchor)
    )
    for raw_turn in logical_turns:
        if not isinstance(raw_turn, dict) or not raw_turn.get('turn_id') or not raw_turn.get('user_text'):
            continue
        turn = dict(raw_turn)
        turn['usage'] = usage_by_turn.get(str(turn['turn_id']), {
            'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
            'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
            'models': [], 'route': [], 'cost_known': False, 'cost_usd': 0.0, 'last_at': '',
        })
        signal_flags = _codex_prompt_flags(turn.get('user_text') or '')
        if (not turn.get('completed') and
                _safe_nonnegative_int(turn.get('assistant_message_count')) <= 0 and
                _safe_nonnegative_int(turn['usage'].get('total_tokens')) <= 0 and
                not signal_flags['acceptance_only'] and
                not signal_flags['manual_user_repair']):
            continue
        outer_turn_id = str(
            turn.get('outer_turn_id') or turn.get('usage_task_id') or turn.get('turn_id') or ''
        )
        reviewed_orphan = bool(
            turn.get('is_subagent') and
            not str(turn.get('parent_thread_id') or '') and
            (str(turn.get('turn_id') or '') in reviewed_anchors or
             outer_turn_id in reviewed_anchors)
        )
        if turn.get('is_subagent') and not reviewed_orphan:
            delegated_turns.append(turn)
        else:
            if reviewed_orphan:
                # Some migrated/direct desktop tasks are labelled ``subagent``
                # without a parent.  Do not promote all such orphan logs: a
                # manual mission review is the explicit evidence that this is
                # user-owned work which belongs in the outcome matrix.
                turn['reviewed_orphan_promoted'] = True
            thread_turns.setdefault(str(turn.get('thread_id') or turn.get('source_file') or ''), []).append(turn)

    missions = []
    for _, turns in sorted(thread_turns.items()):
        turns.sort(key=lambda turn: (str(turn.get('started_at') or ''), str(turn.get('turn_id') or '')))
        current = None
        thread_missions = []

        def finish_current(default_status=None, confidence=None, accepted_at=None):
            nonlocal current
            if current is None:
                return
            if not default_status and current.get('status') == 'unresolved':
                inferred = _codex_infer_terminal_status(current)
                if inferred:
                    default_status, confidence = inferred
            if default_status and current.get('status') == 'unresolved':
                current['status'] = default_status
                current['status_confidence'] = confidence or current.get('status_confidence') or 'low'
            if accepted_at:
                current['accepted_at'] = accepted_at
            first_current_turn = (current.get('turns') or [None])[0]
            vague_continuation = bool(
                isinstance(first_current_turn, dict) and
                _codex_is_vague_continuation_prompt(first_current_turn.get('user_text') or '')
            )
            if (thread_missions and vague_continuation and
                    (current.get('category') == 'other' or
                     first_current_turn.get('category_from_assistant'))):
                previous_mission = thread_missions[-1]
                previous_categories = list(
                    previous_mission.get('categories') or [previous_mission.get('category') or 'other']
                )
                if previous_categories and previous_categories[0] != 'other':
                    current['category'] = previous_categories[0]
                    current['categories'] = previous_categories
                    current['category_confidence'] = 'medium'
                    for inherited_turn in current.get('turns') or []:
                        if (inherited_turn.get('category') == 'other' or
                                inherited_turn.get('category_from_assistant') or
                                _codex_is_vague_continuation_prompt(inherited_turn.get('user_text') or '')):
                            inherited_turn['category'] = previous_categories[0]
                            inherited_turn['categories'] = list(previous_categories)
                            inherited_turn['category_confidence'] = 'medium'
                            inherited_turn['category_inherited'] = True
            completed = _codex_finalize_mission(current, reviews)
            if completed:
                completed['boundary_override'] = boundaries.get(completed['anchor_turn_id'])
                thread_missions.append(completed)
            current = None

        for turn in turns:
            text = str(turn.get('user_text') or '')
            if _codex_is_non_task_prompt(text):
                continue
            flags = _codex_prompt_flags(text)
            boundary = boundaries.get(str(turn.get('turn_id') or ''))
            if flags['manual_user_repair'] and current is not None and boundary != 'continue':
                # A concrete "I fixed it myself by hand" statement is evidence
                # that the model did not finish the preceding objective.  Do not
                # let the later artifact silently convert that model run to success.
                finish_current('failed_user_repaired_auto', 'high', turn.get('started_at'))
                if not flags['new_work']:
                    continue
            if flags['acceptance_only'] and boundary != 'new':
                if current is not None:
                    finish_current('accepted_explicit', 'high', turn.get('started_at'))
                continue

            categories, category, category_confidence, category_ambiguous = _codex_task_categories(
                text, turn.get('cwd')
            )
            category_from_assistant = False
            if ((category == 'other' or category_confidence == 'low') and
                    str(turn.get('assistant_text') or '').strip()):
                assistant_categories, assistant_category, assistant_confidence, assistant_ambiguous = (
                    _codex_task_categories(turn.get('assistant_text') or '', turn.get('cwd'))
                )
                if assistant_category != 'other' and assistant_confidence in ('medium', 'high'):
                    categories = assistant_categories
                    category = assistant_category
                    category_confidence = 'medium'
                    category_ambiguous = assistant_ambiguous
                    category_from_assistant = True
            if (current is not None and
                    'web' in (current.get('categories') or []) and
                    _codex_is_embedded_classification_example(text)):
                categories = ['web']
                category = 'web'
                category_confidence = 'high'
                category_ambiguous = False
            if flags['acceptance_plus_work'] and current is not None and boundary != 'continue':
                finish_current('accepted_explicit', 'high', turn.get('started_at'))

            start_new = current is None
            inferred_boundary = False
            inferred_boundary_reason = ''
            repair_split = False
            if current is not None:
                previous_turn = (current.get('turns') or [])[-1] if current.get('turns') else None
                previous_model = _codex_turn_primary_model_hint(previous_turn)
                current_model = _codex_turn_primary_model_hint(turn)
                model_changed = bool(
                    previous_model and current_model and previous_model != current_model
                )
                previous_has_response = bool(
                    isinstance(previous_turn, dict) and
                    (_safe_nonnegative_int((previous_turn.get('usage') or {}).get('total_tokens')) > 0 or
                     _safe_nonnegative_int(previous_turn.get('assistant_message_count')) > 0)
                )
                current_has_response = bool(
                    _safe_nonnegative_int(turn.get('assistant_message_count')) > 0
                )
                current_categories = set(current.get('categories') or [current.get('category') or 'other'])
                repair_category_overlap = bool(
                    set(categories) & current_categories or
                    category == 'other' or category_confidence == 'low' or
                    current.get('category') == 'other'
                )
                repair_request = bool(
                    _codex_is_repair_prompt(text) and
                    not _codex_is_quota_accounting_question(text)
                )
                cross_model_new_action = _codex_is_cross_model_new_action_transition(
                    text,
                    (previous_turn or {}).get('user_text') or '',
                    model_changed,
                )
                previous_dt = _parse_iso_utc((previous_turn or {}).get('started_at'))
                turn_dt = _parse_iso_utc(turn.get('started_at'))
                gap_hours = (
                    (turn_dt - previous_dt).total_seconds() / 3600.0
                    if previous_dt is not None and turn_dt is not None else 0.0
                )
                if boundary == 'new':
                    start_new = True
                    repair_split = (
                        repair_request and model_changed and previous_has_response and current_has_response and
                        repair_category_overlap
                    )
                elif boundary == 'continue':
                    start_new = False
                elif flags['acceptance_plus_work'] or flags['new_work']:
                    start_new = True
                    inferred_boundary = True
                    inferred_boundary_reason = 'explicit_new_work'
                elif cross_model_new_action:
                    start_new = True
                    inferred_boundary = True
                    inferred_boundary_reason = 'cross_model_new_action'
                elif (flags['informational_question'] and
                      _codex_is_quota_accounting_question(text)):
                    start_new = True
                    inferred_boundary = True
                    inferred_boundary_reason = 'quota_accounting_question'
                elif (repair_request and model_changed and previous_has_response and current_has_response and
                      repair_category_overlap):
                    # A correction performed by another model must be its own
                    # mission so the repair model keeps its real usage and the
                    # failed model receives the same amount as a penalty.
                    start_new = True
                    inferred_boundary = True
                    inferred_boundary_reason = 'cross_model_repair'
                    repair_split = True
                elif flags['followup']:
                    if (gap_hours >= 6.0 and
                            not _codex_is_explicit_continuation_prompt(text)):
                        start_new = True
                        inferred_boundary = True
                        inferred_boundary_reason = 'long_gap_followup'
                    else:
                        start_new = False
                elif current.get('turns'):
                    if gap_hours >= 6.0:
                        # A later same-category request is normally a new unit
                        # of work. Explicit continuation/correction wording was
                        # handled above, so delayed repairs remain connected.
                        start_new = True
                        inferred_boundary = True
                        inferred_boundary_reason = 'long_gap'
                    elif (category != current.get('category') and
                          category_confidence == 'high' and
                          current.get('category_confidence') == 'high'):
                        start_new = True
                        inferred_boundary = True
                        inferred_boundary_reason = 'category_change'
                    else:
                        start_new = False
                elif (category != current.get('category') and category_confidence == 'high' and
                      current.get('category_confidence') == 'high'):
                    start_new = True
                    inferred_boundary = True
                    inferred_boundary_reason = 'category_change'
                else:
                    start_new = False

            category_inherited = False
            if current is not None and not start_new and (category == 'other' or category_confidence == 'low'):
                previous_turn = (current.get('turns') or [])[-1] if current.get('turns') else None
                inherited_category = None
                inherited_confidence = 'low'
                if isinstance(previous_turn, dict):
                    previous_category = previous_turn.get('category')
                    previous_confidence = str(previous_turn.get('category_confidence') or 'low')
                    if previous_category in CODEX_TASK_CATEGORY_KEYS and previous_category != 'other':
                        inherited_category = previous_category
                        inherited_confidence = previous_confidence
                if inherited_category is None:
                    current_category = current.get('category')
                    if current_category in CODEX_TASK_CATEGORY_KEYS and current_category != 'other':
                        inherited_category = current_category
                        inherited_confidence = str(current.get('category_confidence') or 'low')
                if inherited_category:
                    category = inherited_category
                    categories = [inherited_category]
                    category_confidence = 'medium' if inherited_confidence in ('high', 'medium') else 'low'
                    category_ambiguous = False
                    category_inherited = True

            turn['category'] = category
            turn['categories'] = list(categories)
            turn['category_confidence'] = category_confidence
            turn['category_ambiguous'] = category_ambiguous
            turn['category_inherited'] = category_inherited
            turn['category_from_assistant'] = category_from_assistant

            if start_new:
                if current is not None:
                    if repair_split:
                        finish_current('abandoned_auto_repaired', 'high', turn.get('started_at'))
                    elif inferred_boundary:
                        finish_current('accepted_inferred', 'medium', turn.get('started_at'))
                    else:
                        finish_current()
                current = {
                    'turns': [], 'category': category, 'categories': list(categories),
                    'category_confidence': category_confidence,
                    'status': 'unresolved', 'status_confidence': 'low',
                    'accepted_at': '',
                    'inferred_boundary_reason': inferred_boundary_reason,
                }
            elif (category_confidence == 'high' and
                  (current.get('category_confidence') != 'high' or
                   current.get('category') in ('other', 'software_debugging', 'research'))):
                current['category'] = category
                current['category_confidence'] = category_confidence
            current_categories = list(current.get('categories') or [])
            for category_key in categories:
                if category_key not in current_categories:
                    current_categories.append(category_key)
            current['categories'] = current_categories
            current['turns'].append(turn)

        finish_current()
        for index, mission in enumerate(thread_missions):
            mission['previous_anchor_turn_id'] = (
                thread_missions[index - 1]['anchor_turn_id'] if index > 0 else None
            )
        missions.extend(thread_missions)

    missions_by_thread = {}
    for mission in missions:
        missions_by_thread.setdefault(str(mission.get('thread_id') or ''), []).append(mission)
    attached_delegated = 0
    orphan_delegated = 0
    for turn in delegated_turns:
        parent_id = str(turn.get('parent_thread_id') or '')
        candidates = missions_by_thread.get(parent_id) or []
        turn_dt = _parse_iso_utc(turn.get('started_at'))
        prior = []
        for mission in candidates:
            mission_dt = _parse_iso_utc(mission.get('start_at'))
            if mission_dt is not None and turn_dt is not None and mission_dt <= turn_dt:
                prior.append((mission_dt, mission))
        if prior:
            target = max(prior, key=lambda item: item[0])[1]
            _attach_codex_delegated_turn(target, turn)
            attached_delegated += 1
        else:
            orphan_delegated += 1

    missions.sort(key=lambda mission: (str(mission.get('start_at') or ''), mission.get('id') or ''), reverse=True)
    _codex_apply_repair_penalties(missions)
    _codex_apply_mission_quota_estimates(
        missions,
        quota_efficiency=quota_efficiency,
        quota_task_samples=quota_task_samples,
    )
    review_cases = _codex_collect_review_cases(missions)
    audit_cases, audit_groups, audit_patterns = _codex_collect_audit_cases(missions)
    unconfirmed_groups = _codex_collect_unconfirmed_groups(missions)
    repair_accounting = _codex_repair_accounting_summary(missions)
    repair_capabilities = _codex_build_repair_capabilities(missions)
    matrices = {
        '90': _codex_task_matrix(missions, 90, now_utc),
        '180': _codex_task_matrix(missions, 180, now_utc),
        '365': _codex_task_matrix(missions, 365, now_utc),
        'all': _codex_task_matrix(missions, None, now_utc),
    }
    duration_timeline = build_codex_task_duration_timeline(missions, now_utc)
    accepted_count = sum(1 for mission in missions if mission.get('accepted'))
    abandoned_count = sum(
        1 for mission in missions
        if (str(mission.get('status') or '').startswith('abandoned') or
            str(mission.get('status') or '').startswith('failed_user_repaired'))
    )
    excluded_count = sum(
        1 for mission in missions if str(mission.get('status') or '').startswith('excluded')
    )
    unresolved_count = sum(
        1 for mission in missions if str(mission.get('status') or '').startswith('unresolved')
    )
    mixed_count = sum(1 for mission in missions if len(set(mission.get('route') or [])) > 1)
    reviewed_count = sum(1 for mission in missions if mission.get('reviewed'))
    category_counts = {}
    for mission in missions:
        key = mission.get('category') or 'other'
        category_counts[key] = category_counts.get(key, 0) + 1
    return {
        'available': bool(missions),
        'source': 'Local Codex Session Logs + user review',
        'generated_at': now_utc.isoformat(),
        'categories': list(CODEX_TASK_CATEGORIES),
        'missions': missions,
        'review_cases': review_cases,
        'audit_cases': audit_cases,
        'audit_groups': audit_groups,
        'audit_patterns': audit_patterns,
        'unconfirmed_groups': unconfirmed_groups,
        'repair_accounting': repair_accounting,
        'repair_capabilities': repair_capabilities,
        'matrices': matrices,
        'duration_timeline': duration_timeline,
        'summary': {
            'mission_count': len(missions),
            'accepted_count': accepted_count,
            'unresolved_count': unresolved_count,
            'abandoned_count': abandoned_count,
            'excluded_count': excluded_count,
            'unconfirmed_group_count': len(unconfirmed_groups),
            'mixed_model_count': mixed_count,
            'reviewed_count': reviewed_count,
            'review_case_count': len(review_cases),
            'audit_case_count': len(audit_cases),
            'audit_group_count': len(audit_groups),
            'soft_audit_case_count': sum(1 for item in audit_cases if item.get('soft_audit_needed')),
            'audit_pattern_count': len(audit_patterns),
            'repair_penalty_tokens': repair_accounting['repair_penalty_tokens'],
            'effective_tokens': repair_accounting['effective_tokens'],
            'category_counts': category_counts,
        },
        'diagnostics': {
            **((turn_scan or {}).get('diagnostics') or {}),
            'turns_with_usage': sum(1 for turn_id in usage_by_turn if usage_by_turn[turn_id].get('total_tokens', 0) > 0),
            'turns_indexed': len((turn_scan or {}).get('turns') or []),
            'delegated_turns_attached': attached_delegated,
            'delegated_turns_orphaned': orphan_delegated,
            'grouping_note': 'Automatic semantic heuristics; review merge/split/category/outcome before trusting sparse comparisons.',
        },
    }


def scan_codex_model_usage(sessions_dir=None, cache_file=None, now=None,
                           max_files=500, max_dirs=1000, max_entries=20000,
                           max_line_bytes=10*1024*1024, max_file_size=500*1024*1024,
                           max_total_bytes=4*1024*1024*1024):
    """Scan local Codex session logs for per-model and per-effort usage with bounded incremental caching."""
    with CODEX_SCAN_LOCK:
        max_files = max(0, int(max_files))
        max_dirs = max(0, int(max_dirs))
        max_entries = max(0, int(max_entries))
        max_line_bytes = max(1, int(max_line_bytes))
        max_file_size = max(0, int(max_file_size))
        max_total_bytes = max(0, int(max_total_bytes))
        default_scan = sessions_dir is None
        target_dir = sessions_dir if not default_scan else globals().get('CODEX_SESSIONS_DIR') or CODEX_SESSIONS_DIR
        scan_roots = []

        if target_dir and os.path.exists(target_dir):
            scan_roots.append(target_dir)
        elif default_scan:
            fallback = os.path.expanduser(os.path.join('~', '.codex', 'sessions'))
            if os.path.exists(fallback):
                target_dir = fallback
                scan_roots.append(fallback)
            else:
                fallback_parent = os.path.expanduser(os.path.join('~', '.codex'))
                if os.path.exists(fallback_parent):
                    target_dir = fallback_parent
                    scan_roots.append(fallback_parent)

        # The desktop app moves completed task logs from sessions to
        # archived_sessions. Both roots belong to the same all-time ledger.
        if default_scan and scan_roots and os.path.basename(os.path.normpath(target_dir)).lower() == 'sessions':
            archived_dir = os.path.join(os.path.dirname(os.path.normpath(target_dir)), 'archived_sessions')
            if os.path.isdir(archived_dir):
                real_archived = os.path.realpath(archived_dir)
                if all(os.path.realpath(root) != real_archived for root in scan_roots):
                    scan_roots.append(archived_dir)

        if not scan_roots:
            return {
                'available': False,
                'source': 'local_session_logs',
                'source_label': 'Local Codex Session Logs',
                'observed_at': None,
                'models': {},
                'diagnostics': {
                    'files_scanned': 0,
                    'files_cached': 0,
                    'files_appended': 0,
                    'files_rebuilt': 0,
                    'total_models': 0,
                    'bytes_read': 0,
                    'truncated': False,
                    'coverage_complete': True,
                    'roots_scanned': [],
                    'error': 'Codex session logs directory not found'
                }
            }

        now_utc = now if isinstance(now, datetime) else (_parse_iso_utc(now) if now else datetime.now(timezone.utc))
        if not now_utc:
            now_utc = datetime.now(timezone.utc)
        elif now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        five_hours_ago = now_utc - timedelta(hours=5)
        seven_days_ago = now_utc - timedelta(days=7)
        today_utc_start = _local_day_start_utc(now_utc)
        history_retention_cutoff = now_utc - timedelta(days=367)

        cache_path = cache_file if cache_file is not None else globals().get('CODEX_MODELS_CACHE_FILE') or os.path.join(BASE_DIR, 'codex_models_cache.json')
        cache_store = _read_json_file(cache_path, {'version': CODEX_MODELS_CACHE_VERSION, 'files': {}})
        if (not isinstance(cache_store, dict) or cache_store.get('version') != CODEX_MODELS_CACHE_VERSION or
                not isinstance(cache_store.get('files'), dict)):
            cache_store = {'version': CODEX_MODELS_CACHE_VERSION, 'files': {}}
        else:
            cache_store['version'] = CODEX_MODELS_CACHE_VERSION
        cache_files = cache_store.setdefault('files', {})
        cache_migrated = False
        if default_scan:
            # v4 used paths relative to the active sessions root. Session
            # basenames contain stable task UUIDs, so basename keys survive a
            # move to archived_sessions and avoid rebuilding large logs.
            migrated_files = {}
            for old_key, entry in cache_files.items():
                basename_key = os.path.basename(str(old_key).replace('\\', '/'))
                if not basename_key:
                    continue
                existing = migrated_files.get(basename_key)
                if existing is None:
                    migrated_files[basename_key] = entry
                elif isinstance(entry, dict) and isinstance(existing, dict):
                    incoming_marker = (entry.get('mtime') or 0, entry.get('size') or 0)
                    existing_marker = (existing.get('mtime') or 0, existing.get('size') or 0)
                    if incoming_marker > existing_marker:
                        migrated_files[basename_key] = entry
            if migrated_files != cache_files:
                cache_store['files'] = migrated_files
                cache_files = migrated_files
                cache_migrated = True

        candidate_heap = []
        visited_dirs = 0
        visited_entries = 0
        total_matching_files = 0
        root_file_counts = {os.path.realpath(root): 0 for root in scan_roots}
        discovery_truncated = False
        oversized_files_skipped = False

        try:
            for scan_root in scan_roots:
                real_scan_root = os.path.realpath(scan_root)
                if os.path.isdir(scan_root):
                    for root, _, files in os.walk(scan_root, followlinks=False):
                        visited_dirs += 1
                        if visited_dirs > max_dirs:
                            discovery_truncated = True
                            break
                        for f in files:
                            visited_entries += 1
                            if visited_entries > max_entries:
                                discovery_truncated = True
                                break
                            if f.endswith('.jsonl'):
                                p = os.path.join(root, f)
                                try:
                                    if os.path.islink(p) or is_cloud_offline_file(p):
                                        continue
                                    real_p = os.path.realpath(p)
                                    if os.path.commonpath([real_scan_root, real_p]) != real_scan_root:
                                        continue
                                    sz = os.path.getsize(p)
                                    total_matching_files += 1
                                    root_file_counts[real_scan_root] += 1
                                    if sz <= max_file_size:
                                        cand = (os.path.getmtime(p), p, sz, scan_root)
                                        if max_files <= 0:
                                            continue
                                        if len(candidate_heap) < max_files:
                                            heapq.heappush(candidate_heap, cand)
                                        elif cand > candidate_heap[0]:
                                            heapq.heapreplace(candidate_heap, cand)
                                    else:
                                        oversized_files_skipped = True
                                except Exception:
                                    continue
                        if discovery_truncated:
                            break
                elif os.path.isfile(scan_root) and scan_root.endswith('.jsonl'):
                    if not os.path.islink(scan_root):
                        sz = os.path.getsize(scan_root)
                        total_matching_files += 1
                        root_file_counts[real_scan_root] += 1
                        if sz <= max_file_size:
                            if max_files > 0:
                                candidate_heap.append((os.path.getmtime(scan_root), scan_root, sz, scan_root))
                        else:
                            oversized_files_skipped = True
                if discovery_truncated:
                    break
        except Exception:
            discovery_truncated = True

        if total_matching_files > len(candidate_heap):
            discovery_truncated = True
        if oversized_files_skipped:
            discovery_truncated = True

        candidate_files = [(p, mtime, sz, root) for mtime, p, sz, root in sorted(candidate_heap, reverse=True)]

        truncated = discovery_truncated
        coverage_complete = not truncated

        files_scanned = 0
        files_cached = 0
        files_appended = 0
        files_rebuilt = 0
        bytes_read = 0
        active_rel_paths = set()
        cache_modified = cache_migrated

        for file_path, mtime, size, file_root in candidate_files:
            if bytes_read >= max_total_bytes:
                truncated = True
                coverage_complete = False
                break

            try:
                rel_path = (
                    os.path.basename(file_path)
                    if default_scan
                    else (os.path.relpath(file_path, file_root) if os.path.isdir(file_root) else os.path.basename(file_path))
                )
            except Exception:
                rel_path = os.path.basename(file_path)
            active_rel_paths.add(rel_path)
            files_scanned += 1

            cached_entry = cache_files.get(rel_path)
            cached_offset = cached_entry.get('offset') if isinstance(cached_entry, dict) else None
            cached_offset_valid = (
                isinstance(cached_offset, int) and not isinstance(cached_offset, bool) and
                0 <= cached_offset <= size
            )
            cached_payload_valid = _valid_codex_cache_payload(cached_entry)
            if (cached_entry and isinstance(cached_entry, dict) and
                cached_entry.get('mtime') == mtime and
                cached_entry.get('size') == size and
                cached_offset_valid and cached_offset == size and
                cached_payload_valid and cached_entry.get('attribution_version') == 3 and
                isinstance(cached_entry.get('fingerprint'), str) and cached_entry.get('fingerprint')):
                files_cached += 1
                continue

            start_offset = 0
            current_model = None
            current_effort = None
            pending_service_tier = 'default'
            current_service_tier = 'default'
            current_task_id = None
            prev_cumulative = {}
            seen_request_ids = {}
            last_turn_usage = None
            last_cumulative_shape = None
            all_time = {}
            recent_events = []
            quota_observations = []
            finished_turn_ids = set()
            is_append = False

            if (cached_entry and isinstance(cached_entry, dict) and
                cached_offset_valid and cached_offset > 0 and
                cached_payload_valid and cached_entry.get('attribution_version') == 3 and
                isinstance(cached_entry.get('fingerprint'), str) and cached_entry.get('fingerprint')):
                prefix_hash = _compute_file_prefix_hash(file_path, cached_offset)
                if prefix_hash and prefix_hash == cached_entry.get('fingerprint'):
                    is_append = True
                    files_appended += 1
                    start_offset = cached_offset
                    current_model = cached_entry.get('current_model')
                    current_effort = cached_entry.get('current_effort')
                    pending_service_tier = cached_entry.get('pending_service_tier') or 'default'
                    current_service_tier = cached_entry.get('current_service_tier') or pending_service_tier
                    prev_cumulative = dict(cached_entry.get('prev_cumulative') or {})
                    seen_request_ids = _load_seen_request_ids(cached_entry.get('seen_request_ids'))
                    last_turn_usage = _to_hashable(cached_entry.get('last_turn_usage'))
                    loaded_cumulative_shape = _to_hashable(cached_entry.get('last_cumulative_shape'))
                    if (isinstance(loaded_cumulative_shape, tuple) and len(loaded_cumulative_shape) == 6 and
                            all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                                for value in loaded_cumulative_shape)):
                        last_cumulative_shape = loaded_cumulative_shape
                    all_time = dict(cached_entry.get('all_time') or {})
                    recent_events = list(cached_entry.get('recent_events') or [])
                    quota_observations = list(cached_entry.get('quota_observations') or [])
                    current_task_id = cached_entry.get('current_task_id')
                    finished_turn_ids = {
                        str(turn_id) for turn_id in (cached_entry.get('finished_turn_ids') or [])
                        if isinstance(turn_id, str) and turn_id
                    }

            if not is_append:
                files_rebuilt += 1

            last_complete_pos = start_offset
            current_pos = start_offset
            file_hit_byte_budget = False

            try:
                with open(file_path, 'rb') as f:
                    if start_offset > 0:
                        f.seek(start_offset)
                    while True:
                        remaining_budget = max_total_bytes - bytes_read
                        remaining_file = max(0, size - current_pos)
                        if remaining_file <= 0:
                            break
                        if remaining_budget <= 0:
                            truncated = True
                            coverage_complete = False
                            file_hit_byte_budget = True
                            break
                        read_cap = min(max_line_bytes + 1, remaining_budget, remaining_file)
                        raw_line = f.readline(read_cap)
                        if not raw_line:
                            break
                        line_len = len(raw_line)
                        bytes_read += line_len
                        current_pos += line_len

                        line_complete = raw_line.endswith(b'\n') or raw_line.endswith(b'\r')
                        if not line_complete:
                            if current_pos >= size:
                                # Partial trailing line: keep the durable offset at its start.
                                break
                            if line_len <= max_line_bytes:
                                # The byte budget ended before this complete line could be read.
                                truncated = True
                                coverage_complete = False
                                file_hit_byte_budget = True
                                break

                            # Discard one oversized line in bounded chunks so later valid lines remain usable.
                            oversized_complete = False
                            while current_pos < size:
                                remaining_budget = max_total_bytes - bytes_read
                                if remaining_budget <= 0:
                                    truncated = True
                                    coverage_complete = False
                                    file_hit_byte_budget = True
                                    break
                                discard_cap = min(65536, remaining_budget, size - current_pos)
                                chunk = f.readline(discard_cap)
                                if not chunk:
                                    break
                                bytes_read += len(chunk)
                                current_pos += len(chunk)
                                if chunk.endswith(b'\n') or chunk.endswith(b'\r'):
                                    last_complete_pos = current_pos
                                    oversized_complete = True
                                    break
                            if file_hit_byte_budget:
                                break
                            if not oversized_complete:
                                # Oversized partial line at the active file tail.
                                break
                            continue

                        last_complete_pos = current_pos

                        if line_len > max_line_bytes:
                            continue

                        # Fast prefilter before JSON decoding
                        if (b'turn_context' not in raw_line and
                            b'thread_settings_applied' not in raw_line and
                            b'token_count' not in raw_line and
                            b'task_started' not in raw_line and
                            b'task_complete' not in raw_line and
                            b'custom_tool_call' not in raw_line and
                            b'function_call' not in raw_line and
                            b'user' not in raw_line and
                            b'last_token_usage' not in raw_line and
                            b'total_token_usage' not in raw_line):
                            continue

                        try:
                            text = raw_line.decode('utf-8', errors='ignore').strip()
                            if not text:
                                continue
                            item = json.loads(text)
                        except Exception:
                            continue

                        if not isinstance(item, dict):
                            continue

                        item_type = item.get('type')
                        payload = item.get('payload') if isinstance(item.get('payload'), dict) else {}
                        payload_type = payload.get('type')
                        if item_type == 'event_msg' and payload_type == 'thread_settings_applied':
                            settings = payload.get('thread_settings')
                            if isinstance(settings, dict):
                                pending_service_tier = _codex_service_tier(settings.get('service_tier')) or pending_service_tier
                            continue
                        if item_type == 'event_msg' and payload_type == 'task_started':
                            current_task_id = str(payload.get('turn_id') or '') or None
                            current_service_tier = pending_service_tier
                        if item_type == 'event_msg' and payload_type == 'task_complete':
                            finished_turn_id = str(payload.get('turn_id') or '')
                            if finished_turn_id:
                                finished_turn_ids.add(finished_turn_id)
                            current_task_id = None

                        # Turn context update: Only turn_context records may update model & effort!
                        is_turn_ctx = (item_type == 'turn_context' or (item_type == 'event_msg' and payload_type == 'turn_context') or payload_type == 'turn_context')
                        if is_turn_ctx:
                            context_turn_id = str(payload.get('turn_id') or '')
                            if context_turn_id and context_turn_id != current_task_id:
                                current_task_id = context_turn_id
                                current_service_tier = pending_service_tier
                            context_tier = _codex_service_tier(payload.get('service_tier'))
                            if context_tier:
                                current_service_tier = context_tier
                            raw_m = payload.get('model') if isinstance(payload, dict) else None
                            if not raw_m:
                                raw_m = item.get('model')
                            raw_e = None
                            if isinstance(payload, dict):
                                raw_e = payload.get('effort') or payload.get('reasoning_effort')
                            if raw_e is None:
                                raw_e = item.get('effort') or item.get('reasoning_effort')
                            if raw_m:
                                ck, cm, ev, el = normalize_codex_model_effort(raw_m, raw_e)
                                if cm and cm != 'unknown':
                                    current_model = cm
                                    current_effort = el
                                    last_turn_usage = None
                            continue

                        # User message or prompt reset turn usage dedup
                        if item_type in ('user_message', 'user_prompt', 'prompt', 'user_input', 'user') or payload_type in ('user_message', 'user_prompt', 'prompt', 'user_input', 'user'):
                            last_turn_usage = None

                        # Invariant: No token_count or tool_call event is attributed until a valid turn_context supplies a supported model.
                        if current_model is None:
                            continue

                        # Track tool calls
                        is_tool_call = False
                        if item_type == 'response_item':
                            if payload_type in ('custom_tool_call', 'function_call'):
                                is_tool_call = True
                        elif item_type in ('custom_tool_call', 'function_call') or payload_type in ('custom_tool_call', 'function_call'):
                            is_tool_call = True

                        if is_tool_call:
                            compound_key, canon_model, eff_val, eff_lbl = normalize_codex_model_effort(
                                current_model, current_effort, current_service_tier
                            )
                            m_stat = all_time.setdefault(compound_key, {
                                'model_id': canon_model,
                                'reasoning_effort': eff_val,
                                'service_tier': current_service_tier,
                                'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
                                'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
                                'responses': 0, 'tool_calls': 0, 'last_updated': None
                            })
                            m_stat['tool_calls'] += 1

                        # Track token usage
                        is_token_count = (
                            (item_type == 'event_msg' and payload_type == 'token_count') or
                            item_type == 'token_count' or
                            payload_type == 'token_count'
                        )

                        if is_token_count:
                            info = payload.get('info') if isinstance(payload.get('info'), dict) else {}
                            last_usage = info.get('last_token_usage') or payload.get('last_token_usage') or item.get('last_token_usage')

                            in_toks = 0
                            cached_in = 0
                            cache_write_in = 0
                            out_toks = 0
                            think_toks = 0
                            tot_toks = 0
                            has_usage_event = False
                            response_increment = 0

                            req_id = payload.get('request_id') or info.get('request_id') or item.get('request_id') or payload.get('turn_id') or info.get('turn_id') or item.get('turn_id') or payload.get('response_id') or info.get('response_id') or item.get('response_id')
                            tot_usage = info.get('total_token_usage') or payload.get('total_token_usage') or item.get('total_token_usage')
                            cumulative_state = None
                            cumulative_shape = None
                            if isinstance(tot_usage, dict) and tot_usage:
                                c_in = _safe_nonnegative_int(tot_usage.get('input_tokens') or tot_usage.get('input'))
                                c_cached = _safe_nonnegative_int(tot_usage.get('cached_input_tokens') or tot_usage.get('cached_input'))
                                c_write = _safe_nonnegative_int(tot_usage.get('cache_write_input_tokens') or tot_usage.get('cache_write_input'))
                                c_out = _safe_nonnegative_int(tot_usage.get('output_tokens') or tot_usage.get('output'))
                                c_think = _safe_nonnegative_int(tot_usage.get('reasoning_output_tokens') or tot_usage.get('reasoning_output'))
                                c_tot = _safe_nonnegative_int(tot_usage.get('total_tokens') or tot_usage.get('total'))
                                if c_tot == 0 and (c_in > 0 or c_out > 0):
                                    c_tot = c_in + c_out
                                cumulative_shape = (c_in, c_cached, c_write, c_out, c_think, c_tot)
                                cumulative_state = {
                                    'input_tokens': c_in,
                                    'cached_input_tokens': c_cached,
                                    'cache_write_input_tokens': c_write,
                                    'output_tokens': c_out,
                                    'reasoning_output_tokens': c_think,
                                    'total_tokens': c_tot,
                                }

                            if isinstance(last_usage, dict) and last_usage:
                                in_toks = _safe_nonnegative_int(last_usage.get('input_tokens') or last_usage.get('input'))
                                cached_in = _safe_nonnegative_int(last_usage.get('cached_input_tokens') or last_usage.get('cached_input'))
                                cache_write_in = _safe_nonnegative_int(last_usage.get('cache_write_input_tokens') or last_usage.get('cache_write_input'))
                                out_toks = _safe_nonnegative_int(last_usage.get('output_tokens') or last_usage.get('output'))
                                think_toks = _safe_nonnegative_int(last_usage.get('reasoning_output_tokens') or last_usage.get('reasoning_output'))
                                raw_tot = _safe_nonnegative_int(last_usage.get('total_tokens') or last_usage.get('total'))
                                tot_toks = raw_tot if raw_tot > 0 else (in_toks + out_toks)

                                usage_shape = (in_toks, cached_in, cache_write_in, out_toks, think_toks, tot_toks)
                                if req_id:
                                    request_key = str(req_id)
                                    previous_shape = seen_request_ids.get(request_key)
                                    if previous_shape == usage_shape:
                                        has_usage_event = False
                                    elif previous_shape is None:
                                        seen_request_ids[request_key] = usage_shape
                                        has_usage_event = True
                                        response_increment = 1
                                    else:
                                        deltas = tuple(current - previous for current, previous in zip(usage_shape, previous_shape))
                                        seen_request_ids[request_key] = usage_shape
                                        if all(delta >= 0 for delta in deltas) and any(delta > 0 for delta in deltas):
                                            in_toks, cached_in, cache_write_in, out_toks, think_toks, tot_toks = deltas
                                            has_usage_event = True
                                else:
                                    if cumulative_shape is not None:
                                        if last_cumulative_shape == cumulative_shape:
                                            has_usage_event = False
                                        else:
                                            last_cumulative_shape = cumulative_shape
                                            has_usage_event = True
                                            response_increment = 1
                                    else:
                                        dedup_marker = ('usage',) + usage_shape
                                        if last_turn_usage == dedup_marker:
                                            has_usage_event = False
                                        else:
                                            last_turn_usage = dedup_marker
                                            has_usage_event = True
                                            response_increment = 1
                                if cumulative_state is not None:
                                    # Keep fallback baselines current even when this event supplied last_token_usage.
                                    prev_cumulative = cumulative_state
                            else:
                                if cumulative_state is not None:
                                    p_in = prev_cumulative.get('input_tokens', 0)
                                    p_cached = prev_cumulative.get('cached_input_tokens', 0)
                                    p_write = prev_cumulative.get('cache_write_input_tokens', 0)
                                    p_out = prev_cumulative.get('output_tokens', 0)
                                    p_think = prev_cumulative.get('reasoning_output_tokens', 0)
                                    p_tot = prev_cumulative.get('total_tokens', 0)

                                    d_in = c_in - p_in
                                    d_cached = c_cached - p_cached
                                    d_write = c_write - p_write
                                    d_out = c_out - p_out
                                    d_think = c_think - p_think
                                    d_tot = c_tot - p_tot

                                    prev_cumulative = cumulative_state

                                    if d_in < 0 or d_cached < 0 or d_write < 0 or d_out < 0 or d_think < 0 or d_tot < 0:
                                        has_usage_event = False
                                    elif d_tot > 0 or d_in > 0 or d_out > 0:
                                        in_toks = d_in
                                        cached_in = d_cached
                                        cache_write_in = d_write
                                        out_toks = d_out
                                        think_toks = d_think
                                        tot_toks = d_tot if d_tot > 0 else (d_in + d_out)
                                        has_usage_event = True
                                        response_increment = 1

                            ts_str = item.get('timestamp') or payload.get('timestamp')
                            ev_dt = _parse_iso_utc(ts_str)
                            compound_key, canon_model, eff_val, eff_lbl = normalize_codex_model_effort(
                                current_model, current_effort, current_service_tier
                            )
                            quota_observation = _extract_codex_5h_quota_observation(
                                payload.get('rate_limits'),
                                ts_str,
                                current_model,
                                current_effort,
                                cumulative_state,
                                rel_path,
                                current_task_id,
                                current_service_tier,
                            )
                            if quota_observation is not None:
                                quota_observations.append(quota_observation)

                            if has_usage_event:

                                m_stat = all_time.setdefault(compound_key, {
                                    'model_id': canon_model,
                                    'reasoning_effort': eff_val,
                                    'service_tier': current_service_tier,
                                    'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
                                    'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
                                    'responses': 0, 'tool_calls': 0, 'last_updated': None
                                })
                                m_stat['input_tokens'] += in_toks
                                m_stat['cached_input_tokens'] += cached_in
                                m_stat['cache_write_input_tokens'] += cache_write_in
                                m_stat['output_tokens'] += out_toks
                                m_stat['thinking_tokens'] += think_toks
                                m_stat['total_tokens'] += tot_toks
                                m_stat['responses'] += response_increment
                                if ts_str and ev_dt:
                                    if m_stat['last_updated'] is None or str(ts_str) > str(m_stat['last_updated']):
                                        m_stat['last_updated'] = str(ts_str)

                                if ev_dt:
                                    recent_events.append({
                                        'ts': ts_str,
                                        'task_id': str(current_task_id or ''),
                                        'model_key': compound_key,
                                        'service_tier': current_service_tier,
                                        'in': in_toks,
                                        'cached_in': cached_in,
                                        'cache_write_in': cache_write_in,
                                        'out': out_toks,
                                        'think': think_toks,
                                        'tot': tot_toks,
                                    })
            except Exception:
                pass

            pruned_recent = []
            for ev in recent_events:
                ev_dt = _parse_iso_utc(ev.get('ts'))
                if ev_dt is not None and ev_dt >= history_retention_cutoff:
                    pruned_recent.append(ev)

            pruned_quota_observations = []
            seen_quota_observations = set()
            for obs in quota_observations:
                if not isinstance(obs, dict):
                    continue
                ev_dt = _parse_iso_utc(obs.get('ts'))
                if ev_dt is None or ev_dt < history_retention_cutoff:
                    continue
                marker = (
                    obs.get('ts'), obs.get('model_key'), obs.get('resets_at'),
                    obs.get('used_percent'), obs.get('total_tokens'),
                )
                if marker in seen_quota_observations:
                    continue
                seen_quota_observations.add(marker)
                pruned_quota_observations.append(obs)

            new_fingerprint = _compute_file_prefix_hash(file_path, last_complete_pos)

            cache_files[rel_path] = {
                'mtime': mtime,
                'size': size,
                'offset': last_complete_pos,
                'attribution_version': 3,
                'fingerprint': new_fingerprint,
                'source_bucket': (
                    'archived' if os.path.basename(os.path.normpath(file_root)).lower() == 'archived_sessions'
                    else ('active' if os.path.basename(os.path.normpath(file_root)).lower() == 'sessions' else 'custom')
                ),
                'current_model': current_model,
                'current_effort': current_effort,
                'pending_service_tier': pending_service_tier,
                'current_service_tier': current_service_tier,
                'prev_cumulative': prev_cumulative,
                'seen_request_ids': _serialize_seen_request_ids(seen_request_ids),
                'last_turn_usage': _to_json_safe(last_turn_usage),
                'last_cumulative_shape': _to_json_safe(last_cumulative_shape),
                'all_time': all_time,
                'recent_events': pruned_recent,
                'quota_observations': pruned_quota_observations,
                'current_task_id': current_task_id,
                'finished_turn_ids': sorted(finished_turn_ids),
            }
            cache_modified = True

            if file_hit_byte_budget:
                break

        # Explicit one-root scans keep the old mirror semantics used by tests
        # and diagnostics. The default all-time ledger is append-preserving:
        # moving or removing a local file must not make historical spend fall.
        if coverage_complete and not default_scan:
            stale_keys = [k for k in cache_files if k not in active_rel_paths]
            for k in stale_keys:
                cache_files.pop(k, None)
                cache_modified = True

        if cache_modified:
            try:
                _atomic_write_json(cache_path, cache_store)
            except Exception:
                pass

        aggregated = {}
        sessions_by_model = {}

        for rel_path, f_entry in cache_files.items():
            if not f_entry or not isinstance(f_entry, dict):
                continue

            f_all_time = f_entry.get('all_time') or {}
            for m_key, m_stats in f_all_time.items():
                if not isinstance(m_stats, dict):
                    continue
                if m_stats.get('total_tokens', 0) > 0 or m_stats.get('responses', 0) > 0 or m_stats.get('tool_calls', 0) > 0:
                    sessions_by_model.setdefault(m_key, set()).add(rel_path)

                agg = aggregated.setdefault(m_key, {
                    'model_id': m_stats.get('model_id', m_key),
                    'reasoning_effort': m_stats.get('reasoning_effort', 'medium'),
                    'input_tokens': 0,
                    'cached_input_tokens': 0,
                    'cache_write_input_tokens': 0,
                    'output_tokens': 0,
                    'thinking_tokens': 0,
                    'total_tokens': 0,
                    'weekly_tokens': 0,
                    'weekly_input_tokens': 0,
                    'weekly_output_tokens': 0,
                    'weekly_cached_input_tokens': 0,
                    'weekly_cache_write_input_tokens': 0,
                    'five_hour_tokens': 0,
                    'responses': 0,
                    'tool_calls': 0,
                    'last_updated': None
                })
                agg['input_tokens'] += m_stats.get('input_tokens', 0)
                agg['cached_input_tokens'] += m_stats.get('cached_input_tokens', 0)
                agg['cache_write_input_tokens'] += m_stats.get('cache_write_input_tokens', 0)
                agg['output_tokens'] += m_stats.get('output_tokens', 0)
                agg['thinking_tokens'] += m_stats.get('thinking_tokens', 0)
                agg['total_tokens'] += m_stats.get('total_tokens', 0)
                agg['responses'] += m_stats.get('responses', 0)
                agg['tool_calls'] += m_stats.get('tool_calls', 0)
                if m_stats.get('last_updated'):
                    if agg['last_updated'] is None or str(m_stats['last_updated']) > str(agg['last_updated']):
                        agg['last_updated'] = str(m_stats['last_updated'])

            f_recent = f_entry.get('recent_events') or []
            for ev in f_recent:
                m_key = ev.get('model_key')
                if not m_key:
                    continue
                ev_dt = _parse_iso_utc(ev.get('ts'))
                if not ev_dt:
                    continue
                agg = aggregated.setdefault(m_key, {
                    'model_id': m_key,
                    'reasoning_effort': 'medium',
                    'input_tokens': 0, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
                    'output_tokens': 0, 'thinking_tokens': 0, 'total_tokens': 0,
                    'weekly_tokens': 0, 'weekly_input_tokens': 0, 'weekly_output_tokens': 0,
                    'weekly_cached_input_tokens': 0, 'weekly_cache_write_input_tokens': 0,
                    'five_hour_tokens': 0, 'responses': 0, 'tool_calls': 0, 'last_updated': None
                })
                if ev_dt >= seven_days_ago:
                    agg['weekly_tokens'] += ev.get('tot', 0)
                    agg['weekly_input_tokens'] += ev.get('in', 0)
                    agg['weekly_cached_input_tokens'] += ev.get('cached_in', 0)
                    agg['weekly_cache_write_input_tokens'] += ev.get('cache_write_in', 0)
                    agg['weekly_output_tokens'] += ev.get('out', 0)
                if ev_dt >= five_hours_ago:
                    agg['five_hour_tokens'] += ev.get('tot', 0)
                if ev_dt >= today_utc_start:
                    agg['today_tokens'] = agg.get('today_tokens', 0) + ev.get('tot', 0)
                    agg['today_input_tokens'] = agg.get('today_input_tokens', 0) + ev.get('in', 0)
                    agg['today_cached_input_tokens'] = agg.get('today_cached_input_tokens', 0) + ev.get('cached_in', 0)
                    agg['today_cache_write_input_tokens'] = agg.get('today_cache_write_input_tokens', 0) + ev.get('cache_write_in', 0)
                    agg['today_output_tokens'] = agg.get('today_output_tokens', 0) + ev.get('out', 0)

        all_recent_events = []
        all_quota_observations = []
        all_finished_turn_ids = []
        for rel_path, f_entry in cache_files.items():
            if isinstance(f_entry, dict):
                all_finished_turn_ids.extend(
                    turn_id for turn_id in (f_entry.get('finished_turn_ids') or [])
                    if isinstance(turn_id, str) and turn_id
                )
            if isinstance(f_entry, dict) and f_entry.get('recent_events'):
                for rev in f_entry['recent_events']:
                    rev_dt = _parse_iso_utc(rev.get('ts')) if isinstance(rev, dict) else None
                    if (isinstance(rev, dict) and rev.get('tot', 0) > 0 and
                            rev_dt is not None and rev_dt >= history_retention_cutoff):
                        all_recent_events.append(rev)
            if isinstance(f_entry, dict) and f_entry.get('quota_observations'):
                for observation in f_entry['quota_observations']:
                    if not isinstance(observation, dict):
                        continue
                    observation_dt = _parse_iso_utc(observation.get('ts'))
                    if observation_dt is None or observation_dt < history_retention_cutoff:
                        continue
                    copied = dict(observation)
                    copied['session'] = str(copied.get('session') or rel_path)
                    all_quota_observations.append(copied)

        final_models = {}
        for m_key, m_stats in aggregated.items():
            tot_in = m_stats['input_tokens']
            tot_cached = m_stats['cached_input_tokens']
            tot_write = m_stats['cache_write_input_tokens']
            tot_out = m_stats['output_tokens']
            tot_toks = m_stats['total_tokens']

            all_time_cost = estimate_model_cost(
                m_key,
                input_tokens=tot_in,
                output_tokens=tot_out,
                cached_input_tokens=tot_cached,
                cache_write_input_tokens=tot_write
            )

            wk_in = m_stats['weekly_input_tokens']
            wk_cached = m_stats['weekly_cached_input_tokens']
            wk_write = m_stats['weekly_cache_write_input_tokens']
            wk_out = m_stats['weekly_output_tokens']
            wk_toks = m_stats['weekly_tokens']

            weekly_cost = estimate_model_cost(
                m_key,
                input_tokens=wk_in,
                output_tokens=wk_out,
                cached_input_tokens=wk_cached,
                cache_write_input_tokens=wk_write
            )

            td_in = m_stats.get('today_input_tokens', 0)
            td_cached = m_stats.get('today_cached_input_tokens', 0)
            td_write = m_stats.get('today_cache_write_input_tokens', 0)
            td_out = m_stats.get('today_output_tokens', 0)
            td_toks = m_stats.get('today_tokens', 0)

            today_cost = estimate_model_cost(
                m_key,
                input_tokens=td_in,
                output_tokens=td_out,
                cached_input_tokens=td_cached,
                cache_write_input_tokens=td_write
            )

            cost_known = bool(tot_toks == 0 or all_time_cost is not None)
            weekly_cost_known = bool(wk_toks == 0 or weekly_cost is not None)
            today_cost_known = bool(td_toks == 0 or today_cost is not None)
            pricing_meta = get_pricing_provenance(m_key)

            final_models[m_key] = {
                'model_id': m_stats['model_id'],
                'model_name': m_key,
                'reasoning_effort': m_stats['reasoning_effort'],
                'service_tier': 'fast' if m_key.lower().endswith('(fast)') else 'default',
                'source_kind': 'automatic',
                'source_label': 'Automatic Codex logs',
                'total_tokens': tot_toks,
                'weekly_tokens': wk_toks,
                'five_hour_tokens': m_stats['five_hour_tokens'],
                'today_tokens': td_toks,
                'today_cost_usd': round(today_cost or 0.0, 4) if today_cost is not None else 0.0,
                'today_cost_known': today_cost_known,
                'input_tokens': tot_in,
                'cached_input_tokens': tot_cached,
                'cache_write_input_tokens': tot_write,
                'output_tokens': tot_out,
                'thinking_tokens': m_stats['thinking_tokens'],
                'cost_usd': round(all_time_cost or 0.0, 4),
                'weekly_cost_usd': round(weekly_cost or 0.0, 4),
                'cost_known': cost_known,
                'weekly_cost_known': weekly_cost_known,
                **pricing_meta,
                'sessions': len(sessions_by_model.get(m_key, set())),
                'responses': m_stats['responses'],
                'tool_calls': m_stats['tool_calls'],
                'last_updated': m_stats['last_updated']
            }

        diagnostics = {
            'files_scanned': files_scanned,
            'files_cached': files_cached,
            'files_appended': files_appended,
            'files_rebuilt': files_rebuilt,
            'files_discovered': total_matching_files,
            'files_in_ledger': len(cache_files),
            'retained_historical_files': max(0, len(cache_files) - len(active_rel_paths)),
            'roots_scanned': [os.path.realpath(root) for root in scan_roots],
            'root_file_counts': root_file_counts,
            'includes_archived_sessions': any(
                os.path.basename(os.path.normpath(root)).lower() == 'archived_sessions'
                for root in scan_roots
            ),
            'total_models': len(final_models),
            'bytes_read': bytes_read,
            'truncated': truncated,
            'coverage_complete': coverage_complete,
            'observed_at': now_utc.isoformat(),
        }

        identity_snapshots = load_codex_quota_identity() if default_scan else []
        all_quota_observations = attribute_codex_quota_observations(
            all_quota_observations, identity_snapshots
        )
        quota_identity_events = build_codex_quota_identity_events(identity_snapshots)
        quota_efficiency = build_codex_quota_efficiency(all_quota_observations)
        quota_efficiency_timeline = build_codex_quota_efficiency_timeline(
            all_quota_observations, now=now_utc
        )
        weekly_capacity_history = build_codex_weekly_capacity_history(
            all_quota_observations, now=now_utc
        )
        quota_per_task = build_codex_quota_per_task(
            all_quota_observations, all_recent_events, all_finished_turn_ids
        )
        quota_per_task_timeline = build_codex_quota_per_task_timeline(
            all_quota_observations, all_recent_events, all_finished_turn_ids,
            now=now_utc,
        )
        if default_scan:
            mission_turns = scan_codex_mission_turns(now=now_utc)
            task_quota_samples, _ = _build_codex_quota_task_samples(
                all_quota_observations,
                all_recent_events,
                all_finished_turn_ids,
            )
            task_outcomes = build_codex_task_outcomes(
                mission_turns,
                all_recent_events,
                now=now_utc,
                quota_efficiency=quota_efficiency,
                quota_task_samples=task_quota_samples,
            )
        else:
            task_outcomes = {
                'available': False,
                'source': 'Local Codex Session Logs + user review',
                'missions': [],
                'matrices': {},
                'summary': {},
                'diagnostics': {'error': 'Mission grouping is disabled for explicit diagnostic scans.'},
            }

        return {
            'available': bool(final_models),
            'source': 'local_session_logs',
            'source_label': (
                'Local Codex Session Logs (active + archived)'
                if default_scan and len(scan_roots) > 1
                else 'Local Codex Session Logs'
            ),
            'observed_at': now_utc.isoformat(),
            'models': final_models,
            'recent_events': all_recent_events,
            'quota_observations': all_quota_observations,
            'quota_efficiency': quota_efficiency,
            'quota_efficiency_timeline': quota_efficiency_timeline,
            'weekly_capacity_history': weekly_capacity_history,
            'quota_identity_events': quota_identity_events,
            'quota_identity_snapshot_count': len(identity_snapshots),
            'quota_per_task': quota_per_task,
            'quota_per_task_timeline': quota_per_task_timeline,
            'task_outcomes': task_outcomes,
            'diagnostics': diagnostics
        }


def _resolve_codex_weekly_quota(codex_data):
    """Resolve Codex's weekly denominator with explicit provenance.

    ``weekly_limit_tokens`` predates the official rate-limit scan and is a
    user-configured/manual estimate.  It must never be presented as an
    official limit when a live weekly window has no token denominator (or when
    no official window exists).  Normalized rate-limit windows are preferred
    whenever they expose a finite, positive 7-day token limit.
    """
    codex_data = codex_data if isinstance(codex_data, dict) else {}
    rate_limits = codex_data.get('rate_limits')
    windows = rate_limits.get('windows') if isinstance(rate_limits, dict) else None

    official_limit = None
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            try:
                duration = float(window.get('window_minutes'))
                raw_limit = window.get('limit_tokens')
                if not math.isfinite(duration) or int(duration) != 10080:
                    continue
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)):
                    continue
                limit = float(raw_limit)
                if math.isfinite(limit) and limit > 0:
                    official_limit = int(limit) if limit.is_integer() else limit
                    break
            except (TypeError, ValueError, OverflowError):
                continue

    if official_limit is not None:
        return {
            'limit_tokens': official_limit,
            'weekly_limit_source': 'official_rate_limit',
            'weekly_quota_basis': 'official_rate_limit',
            'weekly_quota_source': 'official_rate_limit',
            'weekly_quota_pct_is_estimate': False,
            'weekly_quota_pct_label': 'Official Codex weekly limit',
        }

    configured = codex_data.get('weekly_limit_tokens')
    configured_limit = None
    if isinstance(configured, (int, float)) and not isinstance(configured, bool):
        try:
            value = float(configured)
            if math.isfinite(value) and value > 0:
                configured_limit = int(value) if value.is_integer() else value
        except (TypeError, ValueError, OverflowError):
            configured_limit = None

    if configured_limit is not None:
        return {
            'limit_tokens': configured_limit,
            'weekly_limit_source': 'configured_manual_estimate',
            'weekly_quota_basis': 'configured_manual_estimate',
            'weekly_quota_source': 'configured_manual_estimate',
            'weekly_quota_pct_is_estimate': True,
            'weekly_quota_pct_label': 'Configured/manual estimate',
        }

    return {
        'limit_tokens': 0,
        'weekly_limit_source': 'unavailable',
        'weekly_quota_basis': 'unavailable',
        'weekly_quota_source': 'unavailable',
        'weekly_quota_pct_is_estimate': None,
        'weekly_quota_pct_label': 'Unavailable (no weekly denominator)',
    }


def build_models_breakdown(global_models_stats, conversations, codex_data=None, quota_data=None):
    """Build unified models_breakdown array combining Antigravity + Codex data with accurate weekly quota calculation"""
    breakdown = []

    # Use the exact capacities already resolved by calculate_quotas() for this
    # analysis pass. Re-reading account defaults here can disagree with a newly
    # learned calibration and make per-model percentages contradict the quota UI.
    weekly_window = (quota_data or {}).get('weekly_window', {})
    gemini_weekly_limit = weekly_window.get('gemini', {}).get('limit_tokens', QUOTA_BUCKET_DEFAULTS['gemini_weekly'])
    external_weekly_limit = weekly_window.get('external', {}).get('limit_tokens', QUOTA_BUCKET_DEFAULTS['external_weekly'])
    codex_weekly_quota = _resolve_codex_weekly_quota(codex_data)
    codex_weekly_limit = codex_weekly_quota['limit_tokens']

    # 1) Antigravity models from live transcript scan
    for m_name, m_stats in global_models_stats.items():
        if m_name == 'Unknown':
            continue
        bm = get_benchmark_for_model(m_name)
        effective_pricing = get_effective_pricing(m_name) or {}
        pricing_meta = get_pricing_provenance(m_name)

        # Calculate weekly quota % used accurately from 7-day rolling window
        is_gemini = 'gemini' in m_name.lower()
        weekly_limit = gemini_weekly_limit if is_gemini else external_weekly_limit

        weekly_used = m_stats.get('weekly_tokens', 0)
        five_h_used = m_stats.get('five_hour_tokens', 0)
        weekly_pct_used = round((weekly_used / max(1, weekly_limit)) * 100, 2) if weekly_limit > 0 else 0.0

        breakdown.append({
            'model_id': m_name,
            'canonical_model_id': bm.get('model_id', m_name),
            'reasoning_effort': bm.get('reasoning_effort'),
            'platform': 'Antigravity',
            'provider': 'OpenAI (ChatGPT Web; GPT-5.6 Sol Thinking proxy)' if pricing_meta['pricing_estimated'] else bm.get('provider', 'Unknown'),
            'badge': '~ GPT-5.6 Sol Thinking pricing proxy' if pricing_meta['pricing_estimated'] else bm.get('badge', ''),
            'source_kind': 'automatic',
            'source_label': 'Automatic Antigravity transcripts',
            'total_tokens': m_stats['total_tokens'],
            'weekly_tokens': weekly_used,
            'five_hour_tokens': five_h_used,
            'today_tokens': m_stats.get('today_tokens', 0),
            'today_cost_usd': round(m_stats.get('today_cost_usd', 0.0), 4),
            'input_tokens': m_stats['input_tokens'],
            'cached_input_tokens': m_stats.get('cached_input_tokens', 0),
            'cache_write_input_tokens': m_stats.get('cache_write_input_tokens', 0),
            'output_tokens': m_stats['output_tokens'],
            'thinking_tokens': m_stats['thinking_tokens'],
            'total_cost_usd': round(m_stats['cost_usd'], 4),
            'weekly_cost_usd': round(m_stats.get('weekly_cost_usd', 0.0), 4),
            'weekly_quota_pct_used': weekly_pct_used,
            'weekly_limit_tokens': weekly_limit,
            'weekly_limit_source': 'antigravity_calibrated_capacity',
            'weekly_quota_basis': 'antigravity_calibrated_capacity',
            'weekly_quota_source': 'antigravity_calibrated_capacity',
            'weekly_quota_pct_is_estimate': True,
            'weekly_quota_pct_label': 'Antigravity calibrated capacity',
            'sessions': m_stats['sessions_count'],
            'responses': m_stats['model_responses'],
            'tool_calls': m_stats['tool_calls'],
            'avg_tokens_per_turn': round(m_stats['output_tokens'] / max(1, m_stats['model_responses'])),
            'thinking_pct': round((m_stats['thinking_tokens'] / max(1, m_stats['output_tokens'])) * 100, 1) if m_stats['output_tokens'] > 0 else 0,
            'price_in_1m': effective_pricing.get('price_in_1m', bm.get('price_in_1m')),
            'price_out_1m': effective_pricing.get('price_out_1m', bm.get('price_out_1m')),
            **pricing_meta,
            'cost_known': model_has_cost_estimate(m_name),
            'weekly_cost_known': model_has_cost_estimate(m_name),
            'duration_minutes': round(m_stats.get('duration_minutes', 0), 1)
        })

    # 2) Automatic Codex models from local session logs
    auto_models = {}
    if codex_data and isinstance(codex_data.get('automatic_model_usage'), dict):
        auto_models = codex_data['automatic_model_usage'].get('models', {})

    auto_normalized_keys = set()
    for m_name, m_stats in auto_models.items():
        bm = get_benchmark_for_model(m_name)
        effective_pricing = get_effective_pricing(m_name) or {}
        pricing_meta = get_pricing_provenance(m_name)
        tot_toks = m_stats.get('total_tokens', 0)
        wk_toks = m_stats.get('weekly_tokens', 0)
        weekly_pct_used = round((wk_toks / max(1, codex_weekly_limit)) * 100, 2) if codex_weekly_limit > 0 else None

        auto_normalized_keys.add(_codex_variant_lookup_name(m_name))
        auto_normalized_keys.add(m_name.lower())

        breakdown.append({
            'model_id': m_name,
            'canonical_model_id': m_stats.get('model_id', bm.get('model_id', m_name)),
            'reasoning_effort': m_stats.get('reasoning_effort', bm.get('reasoning_effort')),
            'service_tier': m_stats.get('service_tier') or (
                'fast' if m_name.lower().endswith('(fast)') else 'default'
            ),
            'platform': 'Codex',
            'provider': ('OpenAI (ChatGPT Fast credits)' if pricing_meta['pricing_source'] == 'chatgpt_fast_credit_equivalent'
                         else 'OpenAI (ChatGPT Web; GPT-5.6 Sol Thinking proxy)' if _is_chatgpt_web_model(m_name)
                         else bm.get('provider', 'OpenAI (Codex)')),
            'badge': (f"Fast · {pricing_meta['fast_credit_multiplier']:g}× Standard credits"
                      if pricing_meta['pricing_source'] == 'chatgpt_fast_credit_equivalent'
                      else '~ GPT-5.6 Sol Thinking pricing proxy' if _is_chatgpt_web_model(m_name)
                      else bm.get('badge', '')),
            'source_kind': 'automatic',
            'source_label': 'Automatic Codex logs',
            'total_tokens': tot_toks,
            'weekly_tokens': wk_toks,
            'five_hour_tokens': m_stats.get('five_hour_tokens', 0),
            'today_tokens': m_stats.get('today_tokens', 0),
            'today_cost_usd': round(m_stats.get('today_cost_usd', 0.0), 4),
            'input_tokens': m_stats.get('input_tokens', 0),
            'cached_input_tokens': m_stats.get('cached_input_tokens', 0),
            'cache_write_input_tokens': m_stats.get('cache_write_input_tokens', 0),
            'output_tokens': m_stats.get('output_tokens', 0),
            'thinking_tokens': m_stats.get('thinking_tokens', 0),
            'total_cost_usd': round(m_stats.get('cost_usd', 0.0), 4),
            'weekly_cost_usd': round(m_stats.get('weekly_cost_usd', 0.0), 4),
            'weekly_quota_pct_used': weekly_pct_used,
            'weekly_limit_tokens': codex_weekly_limit,
            **{key: value for key, value in codex_weekly_quota.items() if key != 'limit_tokens'},
            'sessions': m_stats.get('sessions', 0),
            'responses': m_stats.get('responses', 0),
            'tool_calls': m_stats.get('tool_calls', 0),
            'avg_tokens_per_turn': round(m_stats.get('output_tokens', 0) / max(1, m_stats.get('responses', 1))),
            'thinking_pct': round((m_stats.get('thinking_tokens', 0) / max(1, m_stats.get('output_tokens', 1))) * 100, 1) if m_stats.get('output_tokens', 0) > 0 else 0,
            'price_in_1m': effective_pricing.get('price_in_1m', bm.get('price_in_1m')),
            'price_out_1m': effective_pricing.get('price_out_1m', bm.get('price_out_1m')),
            **pricing_meta,
            'cost_known': m_stats.get(
                'cost_known',
                bool(tot_toks == 0 or m_stats.get('cost_usd', 0) > 0 or model_has_cost_estimate(m_name))
            ),
            'weekly_cost_known': m_stats.get(
                'weekly_cost_known',
                bool(wk_toks == 0 or m_stats.get('weekly_cost_usd', 0) > 0 or model_has_cost_estimate(m_name))
            ),
            'duration_minutes': 0,
            'last_updated': m_stats.get('last_updated')
        })

    # 3) Manual Codex models (fallback for models not detected automatically)
    if codex_data and codex_data.get('models'):
        for m_name, m_stats in codex_data['models'].items():
            norm_k = _codex_variant_lookup_name(m_name)
            if norm_k in auto_normalized_keys or m_name.lower() in auto_normalized_keys:
                # Superseded by automatic entry!
                continue

            bm = get_benchmark_for_model(m_name)
            effective_pricing = get_effective_pricing(m_name) or {}
            pricing_meta = get_pricing_provenance(m_name)
            tot_toks = m_stats.get('total_tokens', 0)
            wk_toks = m_stats.get('weekly_tokens', tot_toks)
            weekly_pct_used = round((wk_toks / max(1, codex_weekly_limit)) * 100, 2) if codex_weekly_limit > 0 else None

            breakdown.append({
                'model_id': m_name,
                'canonical_model_id': bm.get('model_id', m_name),
                'reasoning_effort': bm.get('reasoning_effort'),
                'platform': 'Codex',
                'provider': 'OpenAI (ChatGPT Web; GPT-5.6 Sol Thinking proxy)' if pricing_meta['pricing_estimated'] else bm.get('provider', 'OpenAI (Codex)'),
                'badge': '~ GPT-5.6 Sol Thinking pricing proxy' if pricing_meta['pricing_estimated'] else bm.get('badge', ''),
                'source_kind': 'manual_fallback',
                'source_label': 'Manual fallback',
                'total_tokens': tot_toks,
                'weekly_tokens': wk_toks,
                'five_hour_tokens': m_stats.get('five_hour_tokens', 0),
                'input_tokens': m_stats.get('input_tokens', 0),
                'cached_input_tokens': m_stats.get('cached_input_tokens', 0),
                'cache_write_input_tokens': m_stats.get('cache_write_input_tokens', 0),
                'output_tokens': m_stats.get('output_tokens', 0),
                'thinking_tokens': m_stats.get('thinking_tokens', 0),
                'total_cost_usd': round(m_stats.get('cost_usd', 0), 4),
                'weekly_cost_usd': round(m_stats.get('weekly_cost_usd', m_stats.get('cost_usd', 0)), 4),
                'weekly_quota_pct_used': weekly_pct_used,
                'weekly_limit_tokens': codex_weekly_limit,
                **{key: value for key, value in codex_weekly_quota.items() if key != 'limit_tokens'},
                'sessions': m_stats.get('sessions', 0),
                'responses': m_stats.get('responses', 0),
                'tool_calls': m_stats.get('tool_calls', 0),
                'avg_tokens_per_turn': round(m_stats.get('output_tokens', 0) / max(1, m_stats.get('responses', 1))),
                'thinking_pct': round((m_stats.get('thinking_tokens', 0) / max(1, m_stats.get('output_tokens', 1))) * 100, 1) if m_stats.get('output_tokens', 0) > 0 else 0,
                'price_in_1m': effective_pricing.get('price_in_1m', bm.get('price_in_1m')),
                'price_out_1m': effective_pricing.get('price_out_1m', bm.get('price_out_1m')),
                **pricing_meta,
                'cost_known': m_stats.get(
                    'cost_known',
                    bool(tot_toks == 0 or m_stats.get('cost_usd', 0) > 0 or model_has_cost_estimate(m_name))
                ),
                'weekly_cost_known': m_stats.get(
                    'weekly_cost_known',
                    bool(wk_toks == 0 or m_stats.get('weekly_cost_usd', 0) > 0 or model_has_cost_estimate(m_name))
                ),
                'duration_minutes': 0,
                'last_updated': m_stats.get('last_updated')
            })

    # Sort by total_tokens descending
    breakdown.sort(key=lambda x: x['total_tokens'], reverse=True)
    return breakdown

# ---- Time-Series History & Real-Time Timeline Engine ----
TIME_SERIES_FILE = os.path.join(BASE_DIR, 'time_series_history.json')

def load_time_series_history():
    """Load persistent time-series snapshot records"""
    default = {'snapshots': [], 'updated_at': None}
    data = _read_json_file(TIME_SERIES_FILE, default)
    return data if isinstance(data, dict) else dict(default)

def record_time_series_snapshot(quotas_data, conversations=None):
    """Save periodic snapshot of inferred and rolling tokens at the current moment"""
    if not quotas_data:
        return

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    now_local = datetime.now()
    time_str = now_local.strftime('%H:%M:%S')
    date_str = now_local.strftime('%d/%m')

    w5 = quotas_data.get('five_hour_window', {})
    ww = quotas_data.get('weekly_window', {})

    g5 = w5.get('gemini', {})
    e5 = w5.get('external', {})
    gw = ww.get('gemini', {})
    ew = ww.get('external', {})

    active_model = 'Gemini'
    if conversations and len(conversations) > 0:
        active_model = conversations[0].get('model_name') or 'Gemini'

    g5_used = g5.get('used_tokens', 0)
    g5_pct = g5.get('percentage_remaining', 100.0)
    g5_lim = g5.get('limit_tokens', 1000000)
    gw_used = gw.get('used_tokens', 0)
    gw_pct = gw.get('percentage_remaining', 100.0)
    gw_lim = gw.get('limit_tokens', 2500000)

    # Capacity evidence comes from persisted calibration, never from the
    # circular used/(1-percent) inverse that used to populate this chart.
    g5_cal = g5.get('calibration') if isinstance(g5.get('calibration'), dict) else {}
    gw_cal = gw.get('calibration') if isinstance(gw.get('calibration'), dict) else {}
    g5_worker_cal = g5.get('worker_calibration') if isinstance(g5.get('worker_calibration'), dict) else {}
    gw_worker_cal = gw.get('worker_calibration') if isinstance(gw.get('worker_calibration'), dict) else {}
    g5_transcript_capacity = g5_cal.get('capacity_tokens', g5_lim)
    g5_worker_capacity = g5_worker_cal.get('capacity_tokens', g5_cal.get('worker_capacity_tokens', 0))
    g5_mixed_capacity = g5_cal.get('mixed_capacity_tokens')
    gw_transcript_capacity = gw_cal.get('capacity_tokens', gw_lim)
    gw_worker_capacity = gw_worker_cal.get('capacity_tokens', gw_cal.get('worker_capacity_tokens', 0))
    gw_mixed_capacity = gw_cal.get('mixed_capacity_tokens')

    snapshot = {
        'timestamp': now_iso,
        'time_str': time_str,
        'date_str': date_str,
        'gemini_5h_used': g5_used,
        'external_5h_used': e5.get('used_tokens', 0),
        'total_5h_used': g5_used + e5.get('used_tokens', 0),
        'gemini_5h_pct': g5_pct,
        'external_5h_pct': e5.get('percentage_remaining', 100.0),
        'gemini_5h_limit': g5_lim,
        # Deprecated compatibility field: it is no longer an independent
        # evidence series and is deliberately equal to the active limit.
        'gemini_5h_capacity_inferred': g5_lim,
        'gemini_5h_capacity_inferred_deprecated': True,
        'gemini_5h_capacity_transcript': g5_transcript_capacity,
        'gemini_5h_capacity_worker': g5_worker_capacity,
        'gemini_5h_capacity_mixed': g5_mixed_capacity,
        'gemini_5h_capacity_confidence': g5_cal.get('confidence'),
        'gemini_5h_capacity_method': g5_cal.get('method'),
        'gemini_5h_capacity_identifiability': g5_cal.get('identifiability'),
        'external_5h_limit': e5.get('limit_tokens', 60000),
        'gemini_wk_used': gw_used,
        'external_wk_used': ew.get('used_tokens', 0),
        'total_wk_used': gw_used + ew.get('used_tokens', 0),
        'gemini_wk_pct': gw_pct,
        'external_wk_pct': ew.get('percentage_remaining', 100.0),
        'gemini_wk_limit': gw_lim,
        'gemini_wk_capacity_inferred': gw_lim,
        'gemini_wk_capacity_inferred_deprecated': True,
        'gemini_wk_capacity_transcript': gw_transcript_capacity,
        'gemini_wk_capacity_worker': gw_worker_capacity,
        'gemini_wk_capacity_mixed': gw_mixed_capacity,
        'gemini_wk_capacity_confidence': gw_cal.get('confidence'),
        'gemini_wk_capacity_method': gw_cal.get('method'),
        'gemini_wk_capacity_identifiability': gw_cal.get('identifiability'),
        'external_wk_limit': ew.get('limit_tokens', 250000),
        'active_model': active_model
    }

    try:
        ts_data = load_time_series_history()
        snapshots = ts_data.get('snapshots', [])

        # Debounce: Do not record duplicate snapshots within 10s if tokens unchanged
        if snapshots:
            last = snapshots[-1]
            try:
                last_dt = datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00'))
                sec_diff = (now_utc - last_dt).total_seconds()
                capacity_keys = (
                    'gemini_5h_capacity_transcript', 'gemini_5h_capacity_worker',
                    'gemini_5h_capacity_mixed', 'gemini_5h_capacity_method',
                    'gemini_wk_capacity_transcript', 'gemini_wk_capacity_worker',
                    'gemini_wk_capacity_mixed', 'gemini_wk_capacity_method',
                )
                capacity_unchanged = all(last.get(key) == snapshot.get(key) for key in capacity_keys)
                if (sec_diff < 10 and last.get('total_5h_used') == snapshot['total_5h_used'] and
                        last.get('total_wk_used') == snapshot['total_wk_used'] and capacity_unchanged):
                    return
            except Exception:
                pass

        snapshots.append(snapshot)
        # Keep maximum 1000 snapshots FIFO
        if len(snapshots) > 1000:
            snapshots = snapshots[-1000:]

        ts_data['snapshots'] = snapshots
        ts_data['updated_at'] = now_iso
        _atomic_write_json(TIME_SERIES_FILE, ts_data)
    except Exception as e:
        print(f"Error recording time-series snapshot: {e}")

CAPACITY_SHIFT_THRESHOLD = 0.15
CAPACITY_SHIFT_CONFIRMATIONS = 2
CAPACITY_SHIFT_CONSISTENCY = 0.08
CAPACITY_SHIFT_MIN_CONFIDENCE = 0.55


def _trusted_capacity_history_events(events):
    """Keep source-clean transcript-capacity events strong enough for drift detection."""
    trusted = []
    for raw in events or []:
        if not isinstance(raw, dict):
            continue
        capacity = _safe_nonnegative_int(
            raw.get('transcript_capacity_tokens', raw.get('transcript_capacity')))
        try:
            confidence = float(raw.get('confidence') or 0.0)
        except Exception:
            confidence = 0.0
        identifiability = raw.get('identifiability') if isinstance(raw.get('identifiability'), dict) else {}
        if identifiability.get('transcript') is False:
            continue
        if capacity <= 0 or confidence < CAPACITY_SHIFT_MIN_CONFIDENCE:
            continue
        if _safe_nonnegative_int(raw.get('evidence_count')) < 3:
            continue
        if _safe_nonnegative_int(raw.get('pair_count')) < 2:
            continue
        item = dict(raw)
        item['_capacity'] = capacity
        item['_confidence'] = confidence
        trusted.append(item)
    return trusted


def _trusted_recent_cycle_capacity_events(events):
    trusted = []
    seen_evidence = set()
    for raw in events or []:
        if not isinstance(raw, dict) or not raw.get('recent_cycle_accepted'):
            continue
        capacity = _safe_nonnegative_int(raw.get('recent_cycle_capacity_tokens'))
        try:
            confidence = float(raw.get('recent_cycle_confidence') or 0.0)
        except Exception:
            confidence = 0.0
        evidence_count = _safe_nonnegative_int(raw.get('recent_cycle_evidence_count'))
        pair_count = _safe_nonnegative_int(raw.get('recent_cycle_pair_count'))
        evidence_id = raw.get('recent_cycle_evidence_id') or (
            f"{raw.get('recent_cycle_id', raw.get('cycle_id', 'default'))}|"
            f"{raw.get('timestamp')}|{evidence_count}|{pair_count}"
        )
        if (capacity <= 0 or confidence < CAPACITY_SHIFT_MIN_CONFIDENCE or
                evidence_count < 3 or pair_count < 2 or evidence_id in seen_evidence):
            continue
        seen_evidence.add(evidence_id)
        item = dict(raw)
        item['_capacity'] = capacity
        item['_confidence'] = confidence
        item['_evidence_id'] = evidence_id
        trusted.append(item)
    return trusted


def _capacity_cluster(events, tolerance=CAPACITY_SHIFT_CONSISTENCY):
    """Return a robust center and members that agree with it within tolerance."""
    if not events:
        return None, []
    center = _weighted_median([(item['_capacity'], 1.0) for item in events])
    if not center or center <= 0:
        return None, []
    members = [
        item for item in events
        if abs(item['_capacity'] - center) / center <= tolerance
    ]
    if len(members) >= 2:
        center = _weighted_median([(item['_capacity'], 1.0) for item in members])
    return center, members


def detect_quota_policy_shifts(calibration_history):
    """Detect confirmed capacity-regime changes from source-separated calibration evidence.

    A shift needs two consecutive, mutually consistent new-capacity measurements and a
    stable prior cluster. This intentionally ignores raw snapshot limits and mixed-token
    capacity so UI refreshes, source-mix changes, and one bad manual percentage cannot
    masquerade as a provider policy change.
    """
    if not isinstance(calibration_history, dict):
        return []

    shifts = []
    bucket_labels = {
        'gemini_5h': 'Gemini 5H',
        'gemini_weekly': 'Gemini Weekly',
    }
    for bucket in ('gemini_5h', 'gemini_weekly'):
        raw_events = list(calibration_history.get(bucket, []) or [])
        trusted = _trusted_capacity_history_events(raw_events)
        recent_candidates = _trusted_recent_cycle_capacity_events(raw_events)
        last_reported_new_capacity = None
        last_reported_direction = None

        for end_index in range(CAPACITY_SHIFT_CONFIRMATIONS - 1, len(recent_candidates)):
            recent = recent_candidates[end_index - CAPACITY_SHIFT_CONFIRMATIONS + 1:end_index + 1]
            new_capacity, recent_members = _capacity_cluster(recent)
            if len(recent_members) < CAPACITY_SHIFT_CONFIRMATIONS or not new_capacity:
                continue
            first_dt = _parse_iso_utc(recent_members[0].get('timestamp'))
            prior = []
            for item in trusted:
                item_dt = _parse_iso_utc(item.get('timestamp'))
                if first_dt and item_dt and item_dt >= first_dt:
                    continue
                prior.append(item)
            old_capacity, baseline_members = _capacity_cluster(prior[-4:])
            if len(baseline_members) < CAPACITY_SHIFT_CONFIRMATIONS or not old_capacity:
                continue
            relative_change = (new_capacity - old_capacity) / old_capacity
            if abs(relative_change) < CAPACITY_SHIFT_THRESHOLD:
                continue
            direction = 'INCREASE' if relative_change > 0 else 'DECREASE'
            if (last_reported_new_capacity and last_reported_direction == direction and
                    abs(new_capacity - last_reported_new_capacity) / last_reported_new_capacity <= CAPACITY_SHIFT_CONSISTENCY):
                continue
            confirming_event = recent_members[-1]
            detected_at = confirming_event.get('timestamp') or datetime.now(timezone.utc).isoformat()
            detected_dt = _parse_iso_utc(detected_at)
            local_dt = detected_dt.astimezone() if detected_dt else datetime.now().astimezone()
            pct_change = relative_change * 100.0
            sign = '+' if pct_change > 0 else ''
            confidence = min(item['_confidence'] for item in recent_members)
            shifts.append({
                'id': f'shift_{bucket}_recent_{detected_at}',
                'detected_at': detected_at,
                'date_str': local_dt.strftime('%d/%m/%Y'),
                'time_str': local_dt.strftime('%H:%M'),
                'provider': 'Tracked quota estimator',
                'bucket': bucket_labels[bucket],
                'type': direction,
                'old_capacity': int(round(old_capacity)),
                'new_capacity': int(round(new_capacity)),
                'change_pct': f'{sign}{pct_change:.1f}%',
                'status': 'DETECTED_EARLY_CONFIRMED',
                'confidence': round(confidence, 4),
                'confirmations': len(recent_members),
                'baseline_points': len(baseline_members),
                'source': 'recent_cycle_capacity_tokens',
                'note': (
                    f'Early warning confirmed by {len(recent_members)} independent recent-cycle '
                    f'capacity candidates; stable transcript baseline moved from '
                    f'{int(round(old_capacity)):,} toward {int(round(new_capacity)):,} tokens. '
                    'Long-horizon convergence is tracked separately.'
                ),
            })
            last_reported_new_capacity = new_capacity
            last_reported_direction = direction

        if len(trusted) < CAPACITY_SHIFT_CONFIRMATIONS * 2:
            continue

        for end_index in range((CAPACITY_SHIFT_CONFIRMATIONS * 2) - 1, len(trusted)):
            recent = trusted[end_index - CAPACITY_SHIFT_CONFIRMATIONS + 1:end_index + 1]
            new_capacity, recent_members = _capacity_cluster(recent)
            if len(recent_members) < CAPACITY_SHIFT_CONFIRMATIONS or not new_capacity:
                continue

            prior = trusted[:end_index - CAPACITY_SHIFT_CONFIRMATIONS + 1]
            baseline_candidates = prior[-4:]
            old_capacity, baseline_members = _capacity_cluster(baseline_candidates)
            if len(baseline_members) < CAPACITY_SHIFT_CONFIRMATIONS or not old_capacity:
                continue

            relative_change = (new_capacity - old_capacity) / old_capacity
            if abs(relative_change) < CAPACITY_SHIFT_THRESHOLD:
                continue

            direction = 'INCREASE' if relative_change > 0 else 'DECREASE'
            if (last_reported_new_capacity and last_reported_direction == direction and
                    abs(new_capacity - last_reported_new_capacity) / last_reported_new_capacity <= CAPACITY_SHIFT_CONSISTENCY):
                continue

            confirming_event = recent_members[-1]
            detected_at = confirming_event.get('timestamp') or datetime.now(timezone.utc).isoformat()
            detected_dt = _parse_iso_utc(detected_at)
            local_dt = detected_dt.astimezone() if detected_dt else datetime.now().astimezone()
            pct_change = relative_change * 100.0
            sign = '+' if pct_change > 0 else ''
            confidence = min(item['_confidence'] for item in recent_members)
            shifts.append({
                'id': f'shift_{bucket}_{detected_at}',
                'detected_at': detected_at,
                'date_str': local_dt.strftime('%d/%m/%Y'),
                'time_str': local_dt.strftime('%H:%M'),
                'provider': 'Tracked quota estimator',
                'bucket': bucket_labels[bucket],
                'type': direction,
                'old_capacity': int(round(old_capacity)),
                'new_capacity': int(round(new_capacity)),
                'change_pct': f'{sign}{pct_change:.1f}%',
                'status': 'DETECTED_CONFIRMED',
                'confidence': round(confidence, 4),
                'confirmations': len(recent_members),
                'baseline_points': len(baseline_members),
                'source': 'transcript_capacity_tokens',
                'note': (
                    f'Confirmed after {len(recent_members)} consistent calibration points; '
                    f'transcript capacity moved from {int(round(old_capacity)):,} to '
                    f'{int(round(new_capacity)):,} tokens. Mixed/source-ratio capacity was ignored.'
                ),
            })
            last_reported_new_capacity = new_capacity
            last_reported_direction = direction

    return shifts

def build_time_series_analytics(all_step_events, quotas_data):
    """Construct dynamic 5-Hour and 7-Day time-series buckets from all historical step events + live snapshots"""
    now_utc = datetime.now(timezone.utc)
    ts_data = load_time_series_history()
    live_snaps = ts_data.get('snapshots', [])

    w5 = (quotas_data or {}).get('five_hour_window', {})
    ww = (quotas_data or {}).get('weekly_window', {})
    g5_lim = w5.get('gemini', {}).get('limit_tokens', 1000000)
    e5_lim = w5.get('external', {}).get('limit_tokens', 60000)
    gw_lim = ww.get('gemini', {}).get('limit_tokens', 2500000)
    ew_lim = ww.get('external', {}).get('limit_tokens', 250000)

    # 1. 5-Hour Timeline: 20 intervals of 15 minutes each
    five_h_labels = []
    five_h_gemini = []
    five_h_external = []
    five_h_total = []

    for i in range(21):
        bucket_time = now_utc - timedelta(minutes=(20 - i) * 15)
        loc_time = bucket_time.astimezone() if bucket_time.tzinfo else bucket_time
        five_h_labels.append(loc_time.strftime('%H:%M'))

        slot_start = bucket_time - timedelta(minutes=15)
        slot_gem = 0
        slot_ext = 0
        for ev in all_step_events:
            ev_dt = ev.get('dt')
            if ev_dt and slot_start <= ev_dt <= bucket_time:
                toks = ev.get('tokens', 0)
                if ev.get('is_gemini'):
                    slot_gem += toks
                else:
                    slot_ext += toks

        five_h_gemini.append(slot_gem)
        five_h_external.append(slot_ext)
        five_h_total.append(slot_gem + slot_ext)

    # 2. 7-Day Timeline: 7 daily buckets (6 days ago -> today)
    week_labels = []
    week_gemini = []
    week_external = []
    week_total = []
    week_cumulative = []

    cum_sum = 0
    day_names = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN']
    for d in range(7):
        day_start = (now_utc - timedelta(days=6 - d)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        loc_day = day_start.astimezone() if day_start.tzinfo else day_start
        weekday_str = day_names[loc_day.weekday()]
        label = f"{weekday_str} ({loc_day.strftime('%d/%m')})"
        week_labels.append(label)

        day_gem = 0
        day_ext = 0
        for ev in all_step_events:
            ev_dt = ev.get('dt')
            if ev_dt and day_start <= ev_dt < day_end:
                toks = ev.get('tokens', 0)
                if ev.get('is_gemini'):
                    day_gem += toks
                else:
                    day_ext += toks

        tot = day_gem + day_ext
        cum_sum += tot
        week_gemini.append(day_gem)
        week_external.append(day_ext)
        week_total.append(tot)
        week_cumulative.append(cum_sum)

    # 3. Capacity Evolution Time-series (Snapshots-based)
    cap_snaps = live_snaps[-30:] if live_snaps else []
    cap_labels = [s.get('time_str', '—') for s in cap_snaps]
    cap_g5_limits = [s.get('gemini_5h_limit', g5_lim) for s in cap_snaps]
    # The old inferred series is retained as a deprecated compatibility field,
    # but source-specific calibration histories are the only evidence series.
    cap_g5_inferred = [s.get('gemini_5h_capacity_inferred', g5_lim) for s in cap_snaps]
    cap_g5_transcript = [s.get('gemini_5h_capacity_transcript', g5_lim) for s in cap_snaps]
    cap_g5_worker = [s.get('gemini_5h_capacity_worker', 0) for s in cap_snaps]
    cap_g5_mixed = [s.get('gemini_5h_capacity_mixed') for s in cap_snaps]
    cap_gw_limits = [s.get('gemini_wk_limit', gw_lim) for s in cap_snaps]
    cap_gw_transcript = [s.get('gemini_wk_capacity_transcript', gw_lim) for s in cap_snaps]
    cap_gw_worker = [s.get('gemini_wk_capacity_worker', 0) for s in cap_snaps]
    cap_gw_mixed = [s.get('gemini_wk_capacity_mixed') for s in cap_snaps]
    cap_g5_used = [s.get('gemini_5h_used', 0) for s in cap_snaps]
    calibration_history = get_capacity_calibration_history(load_quota_observations(), _active_account_email())

    def capacity_history_series(events, fallback_capacity):
        points = list(events or [])[-60:]
        if not points:
            return None
        recent_cycle_values = [
            event.get('recent_cycle_capacity_tokens') or None
            for event in points
        ]
        labels = []
        for event in points:
            dt = _parse_iso_utc(event.get('timestamp'))
            labels.append(dt.astimezone().strftime('%d/%m %H:%M') if dt else event.get('timestamp', '—'))
        return {
            'labels': labels,
            'timestamps': [event.get('timestamp') for event in points],
            'transcript_capacity': [
                event.get('transcript_capacity_tokens', event.get('transcript_capacity', fallback_capacity))
                for event in points
            ],
            'worker_capacity': [
                event.get('worker_capacity_tokens', event.get('worker_capacity', 0))
                for event in points
            ],
            'mixed_capacity': [
                event.get('mixed_capacity_tokens', event.get('mixed_capacity'))
                for event in points
            ],
            'recent_cycle_capacity': recent_cycle_values,
            'recent_cycle_confidence': [event.get('recent_cycle_confidence') for event in points],
            'recent_cycle_evidence_count': [event.get('recent_cycle_evidence_count', 0) for event in points],
            'recent_cycle_pair_count': [event.get('recent_cycle_pair_count', 0) for event in points],
            'confidence': [event.get('confidence') for event in points],
            'accepted': [bool(event.get('accepted')) for event in points],
            'evidence_count': [event.get('evidence_count', 0) for event in points],
            'pair_count': [event.get('pair_count', 0) for event in points],
            'current_capacity': (
                points[-1].get('transcript_capacity_tokens', points[-1].get('transcript_capacity', fallback_capacity))
            ),
            'current_recent_cycle_capacity': next(
                (value for value in reversed(recent_cycle_values) if value), None),
            'source': 'calibration_history',
        }

    five_hour_calibration_series = capacity_history_series(calibration_history.get('gemini_5h'), g5_lim)
    weekly_calibration_series = capacity_history_series(calibration_history.get('gemini_weekly'), gw_lim)
    policy_shifts = detect_quota_policy_shifts(calibration_history)

    return {
        'five_hour_timeline': {
            'labels': five_h_labels,
            'gemini_tokens': five_h_gemini,
            'external_tokens': five_h_external,
            'total_tokens': five_h_total,
            'gemini_limit': g5_lim,
            'external_limit': e5_lim,
            'current_used_5h': w5.get('gemini', {}).get('used_tokens', 0) + w5.get('external', {}).get('used_tokens', 0),
            'current_pct_5h': w5.get('gemini', {}).get('percentage_remaining', 100.0)
        },
        'weekly_timeline': {
            'labels': week_labels,
            'daily_gemini_tokens': week_gemini,
            'daily_external_tokens': week_external,
            'daily_total_tokens': week_total,
            'cumulative_tokens': week_cumulative,
            'gemini_limit': gw_lim,
            'external_limit': ew_lim,
            'current_used_weekly': ww.get('gemini', {}).get('used_tokens', 0) + ww.get('external', {}).get('used_tokens', 0),
            'current_pct_weekly': ww.get('gemini', {}).get('percentage_remaining', 100.0)
        },
        'capacity_evolution': {
            'labels': cap_labels,
            'gemini_5h_limits': cap_g5_limits,
            'gemini_5h_transcript_capacity': cap_g5_transcript,
            'gemini_5h_worker_capacity': cap_g5_worker,
            'gemini_5h_mixed_capacity': cap_g5_mixed,
            'gemini_5h_inferred': cap_g5_inferred,
            'gemini_5h_used': cap_g5_used,
            'weekly': {
                **(weekly_calibration_series or {
                    'labels': cap_labels,
                    'transcript_capacity': cap_gw_transcript,
                    'worker_capacity': cap_gw_worker,
                    'mixed_capacity': cap_gw_mixed,
                    'current_capacity': gw_lim,
                    'source': 'snapshots_fallback',
                }),
                'gemini_limits': cap_gw_limits,
            },
            'five_hour': {
                **(five_hour_calibration_series or {
                    'labels': cap_labels,
                    'transcript_capacity': cap_g5_transcript,
                    'worker_capacity': cap_g5_worker,
                    'mixed_capacity': cap_g5_mixed,
                    'current_capacity': g5_lim,
                    'source': 'snapshots_fallback',
                }),
            },
            'current_capacity': g5_lim,
            'weekly_current_capacity': gw_lim,
            'confidence': 'Source-specific calibration; mixed series depends on observed source mix',
            'calibration_history': calibration_history,
            'five_hour_history': calibration_history.get('gemini_5h', []),
            'weekly_history': calibration_history.get('gemini_weekly', []),
        },
        'policy_shifts': policy_shifts,
        'recent_snapshots': live_snaps[-60:]
    }

# ---- Official Google Gemini CLI Session Discovery & Parser ----

def discover_gemini_cli_session_files(tmp_dir, max_dirs=2000, max_files=500,
                                      max_file_size=16*1024*1024, max_entries=10000):
    """Discover candidate Gemini CLI session jsonl files with strict bounds."""
    if not tmp_dir or not os.path.exists(tmp_dir):
        return []

    if os.path.isfile(tmp_dir):
        if tmp_dir.endswith('.jsonl'):
            try:
                if os.path.getsize(tmp_dir) <= max_file_size:
                    return [tmp_dir]
            except Exception:
                pass
        return []

    candidate_heap = []
    visited_dirs = 0
    visited_entries = 0
    stop_walk = False
    try:
        for root, dirs, files in os.walk(tmp_dir):
            visited_dirs += 1
            if visited_dirs > max_dirs:
                break
            for f in files:
                visited_entries += 1
                if visited_entries > max_entries:
                    stop_walk = True
                    break
                if f.endswith('.jsonl'):
                    p = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(p)
                        if sz <= max_file_size:
                            candidate = (os.path.getmtime(p), p)
                            if len(candidate_heap) < max_files:
                                heapq.heappush(candidate_heap, candidate)
                            elif candidate > candidate_heap[0]:
                                heapq.heapreplace(candidate_heap, candidate)
                    except Exception:
                        continue
            if stop_walk:
                break
    except Exception:
        pass
    return [p for _, p in sorted(candidate_heap, reverse=True)]


def extract_gemini_cli_message_tokens(msg):
    """Extract authoritative tokens from an official Gemini CLI message.

    Returns (in_toks, out_toks, think_toks, total_quota_toks, has_tokens).
    """
    tokens_data = msg.get('tokens')
    if not isinstance(tokens_data, dict):
        return 0, 0, 0, 0, False

    raw_total = tokens_data.get('total')
    if raw_total is not None and isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool) and math.isfinite(raw_total) and raw_total >= 0:
        total_quota_toks = int(raw_total)
        has_tokens = True
    else:
        parts_sum = 0
        found_any = False
        for k in ('input', 'output', 'cached', 'thoughts', 'thought', 'tool'):
            v = tokens_data.get(k)
            if v is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
                parts_sum += int(v)
                found_any = True
        total_quota_toks = parts_sum
        has_tokens = (found_any and total_quota_toks > 0)

    raw_in = tokens_data.get('input')
    in_val = int(raw_in) if (raw_in is not None and isinstance(raw_in, (int, float)) and not isinstance(raw_in, bool) and math.isfinite(raw_in) and raw_in >= 0) else 0
    in_toks = min(in_val, total_quota_toks)
    out_toks = max(0, total_quota_toks - in_toks)

    raw_think = tokens_data.get('thoughts') if 'thoughts' in tokens_data else tokens_data.get('thought')
    think_toks = int(raw_think) if (raw_think is not None and isinstance(raw_think, (int, float)) and not isinstance(raw_think, bool) and math.isfinite(raw_think) and raw_think >= 0) else 0

    return in_toks, out_toks, think_toks, total_quota_toks, has_tokens


def extract_message_dt(msg, fallback_dt=None):
    for k in ('timestamp', 'created_at', 'time'):
        v = msg.get(k)
        if not v:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                f_sec = float(v)
                if math.isfinite(f_sec):
                    if f_sec > 1e11:
                        f_sec /= 1000.0
                    return datetime.fromtimestamp(f_sec, tz=timezone.utc)
            except Exception:
                pass
        elif isinstance(v, str):
            dt = _parse_iso_utc(v)
            if dt:
                return dt
    return fallback_dt


def extract_message_text(msg):
    text = msg.get('text') if 'text' in msg else msg.get('content')
    if text is None:
        text = ''
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, dict):
                parts.append(p.get('text', ''))
            else:
                parts.append(str(p))
        return ' '.join(parts)
    return str(text)


def parse_official_gemini_cli_session(file_path, max_file_size=16*1024*1024):
    """Reconstruct an official Gemini CLI session from its event-sourced JSONL records.

    Handles:
    - metadata (sessionId, projectHash, startTime, lastUpdated)
    - direct message updates keyed by id (latest record replaces by id, preserving order)
    - {$set: ...} checkpoints where $set.messages replaces the message map
    - {$rewindTo: id} removing that message and all subsequent messages
    """
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        if os.path.getsize(file_path) > max_file_size:
            return None
    except Exception:
        return None

    metadata = {}
    messages_order = []  # list of message ids in addition order
    messages_by_id = {}  # id -> message dict

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue

                if '$set' in item:
                    set_data = item['$set']
                    if isinstance(set_data, dict):
                        if 'messages' in set_data:
                            new_msgs = set_data['messages']
                            messages_order = []
                            messages_by_id = {}
                            if isinstance(new_msgs, dict):
                                for mid, mval in new_msgs.items():
                                    if isinstance(mval, dict):
                                        mid_str = str(mval.get('id') or mid)
                                        messages_order.append(mid_str)
                                        messages_by_id[mid_str] = mval
                            elif isinstance(new_msgs, list):
                                for mval in new_msgs:
                                    if isinstance(mval, dict):
                                        mid_str = str(mval.get('id') or len(messages_order))
                                        if mid_str not in messages_by_id:
                                            messages_order.append(mid_str)
                                        messages_by_id[mid_str] = mval
                        for k, v in set_data.items():
                            if k != 'messages':
                                metadata[k] = v
                    continue

                if '$rewindTo' in item:
                    target_id = str(item['$rewindTo'])
                    if target_id in messages_by_id:
                        try:
                            idx = messages_order.index(target_id)
                            to_remove = messages_order[idx:]
                            messages_order = messages_order[:idx]
                            for mid in to_remove:
                                messages_by_id.pop(mid, None)
                        except ValueError:
                            pass
                    else:
                        messages_order = []
                        messages_by_id = {}
                    continue

                if 'id' in item:
                    msg_id = str(item['id'])
                    if msg_id not in messages_by_id:
                        messages_order.append(msg_id)
                    messages_by_id[msg_id] = item
                    continue

                for k, v in item.items():
                    metadata[k] = v

    except Exception:
        return None

    final_messages = [messages_by_id[mid] for mid in messages_order if mid in messages_by_id]
    return {
        'metadata': metadata,
        'messages': final_messages
    }


def _normalize_model_timeline_range(days=7, start_date=None, end_date=None, now_local=None):
    now_local = now_local or datetime.now().astimezone()
    today_local = now_local.date()
    max_days = 366
    if start_date is not None or end_date is not None:
        if not start_date or not end_date:
            raise ValueError('Khoảng tùy chọn cần đủ ngày bắt đầu và ngày kết thúc.')
        try:
            start_day = datetime.strptime(str(start_date), '%Y-%m-%d').date()
            end_day = datetime.strptime(str(end_date), '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValueError('Ngày tùy chọn phải có định dạng YYYY-MM-DD.') from exc
        if end_day < start_day:
            raise ValueError('Ngày kết thúc phải bằng hoặc sau ngày bắt đầu.')
        if end_day > today_local:
            raise ValueError('Ngày kết thúc không được nằm trong tương lai.')
        days = (end_day - start_day).days + 1
        if days > max_days:
            raise ValueError(f'Khoảng thời gian tối đa là {max_days} ngày.')
        return start_day, end_day, days, 'custom'
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError('Số ngày phải là số nguyên.') from exc
    if days < 1 or days > max_days:
        raise ValueError(f'Số ngày phải nằm trong khoảng 1-{max_days}.')
    end_day = today_local
    start_day = end_day - timedelta(days=days - 1)
    return start_day, end_day, days, 'rolling'

MODEL_COLOR_PALETTE = (
    '#c75a5a', '#19ff19', '#c619ff', '#47ffff', '#177ee6', '#ffc619',
    '#90c75a', '#ff73dc', '#ff1919', '#c6ff19', '#19ff8c', '#38a3c7',
    '#e6a667', '#9673ff', '#ff198c', '#ff8c19', '#67e6a6', '#e6174b',
    '#41c714', '#c75a90', '#c714c7', '#e6e667', '#ff7547', '#e6e617',
    '#96ff73', '#73b9ff', '#9ac714', '#ac5ac7', '#38c75b', '#c7a338',
    '#ff19c6', '#ff7396', '#5ac7c7', '#ff9673', '#73dcff', '#c76d14',
)

# A fixed order keeps model identity stable when a date filter changes the
# ranking or hides a model. New names use the deterministic fallback below.
MODEL_COLOR_NAMES = (
    '5.6 sol xhigh', '5.6 sol high', 'gpt-6-sol xhigh',
    '5.6 luna xhigh', 'gpt-5.5 xhigh', 'chatgpt-web/high high',
    'gpt-6-astra xhigh', 'gpt-6-astra low', '5.6 terra max',
    '5.6 sol standard', 'gpt-5.5 high', 'gpt-5.5 low',
    'gpt-6-astra high', 'gpt-6-astra max', 'gpt-6-astra standard',
    '5.6 luna high', 'gpt-5.5', 'gpt-5.4',
    'gpt-5.4 high', 'Gemini 3.7 Flash (High)',
    'Gemini 3.8 Flash (High)', 'Gemini 3.5 Flash (High)',
    'codex-auto-review low', 'chatgpt-web/think low',
    'Gemini 3.5 Flash (Medium)', 'chatgpt-web/medium',
    'Claude Opus 4.6 (Thinking)', 'gpt-reserve max',
    'Gemini 3.6 Flash (High)', 'gpt-6-luna xhigh',
    'gpt-6-luna high', 'gpt-6-sol high',
    'gpt-6-luna standard', 'gpt-6-sol standard',
    'gpt-6-sol low', 'gpt-6-luna low',
)
MODEL_COLOR_BY_NAME = {
    name.casefold(): MODEL_COLOR_PALETTE[index]
    for index, name in enumerate(MODEL_COLOR_NAMES)
}
# Keep the actively used Sol Max visibly distinct from Luna Xhigh's cyan.
MODEL_COLOR_BY_NAME['gpt-6-sol max'] = '#ff8c42'


def get_model_color(name):
    """Return a stable, model-specific color across token and quota charts."""
    key = str(name or '').strip().casefold()
    if key.endswith('(fast)'):
        base_color = get_model_color(key[:-len('(fast)')].strip())
        base_rgb = tuple(int(base_color[index:index + 2], 16) / 255 for index in (1, 3, 5))
        hue, _, saturation = colorsys.rgb_to_hls(*base_rgb)
        fast_rgb = colorsys.hls_to_rgb((hue + 0.16) % 1.0, 0.62, max(0.78, saturation))
        return '#' + ''.join(f'{round(channel * 255):02x}' for channel in fast_rgb)
    known = MODEL_COLOR_BY_NAME.get(key)
    if known:
        return known
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    hue = int.from_bytes(digest[:2], 'big') % 360
    saturation = 72 + digest[2] % 17
    lightness = 55 + digest[3] % 13
    rgb = colorsys.hls_to_rgb(hue / 360, lightness / 100, saturation / 100)
    return '#' + ''.join(f'{round(channel * 255):02x}' for channel in rgb)


def build_models_daily_timeline(all_step_events, global_models_stats, codex_data=None, days=7, start_date=None, end_date=None):
    """Construct multi-model daily token/cost timeline for a rolling or custom date range."""
    now_local = datetime.now().astimezone()
    today_local = now_local.date()
    start_day, end_day, days, range_mode = _normalize_model_timeline_range(
        days=days, start_date=start_date, end_date=end_date, now_local=now_local
    )

    dates = []
    date_keys = []
    for d in range(days):
        day_value = start_day + timedelta(days=d)
        weekday = ['T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'CN'][day_value.weekday()]
        label = f"{weekday} ({day_value.strftime('%d/%m')})"
        dates.append(label)
        date_keys.append(day_value.isoformat())
    date_index = {date_key: idx for idx, date_key in enumerate(date_keys)}

    # Group step tokens and estimated cost equivalents by model/day.
    # Unknown-price usage is tracked explicitly so it is never presented as a true $0 cost.
    model_daily_map = {}
    model_daily_cost_map = {}
    model_daily_unknown_cost_tokens = {}
    for ev in all_step_events:
        ev_dt = ev.get('dt')
        ev_model = ev.get('model') or ('Gemini' if ev.get('is_gemini') else 'Other')
        ev_toks = ev.get('tokens', 0)
        if not ev_dt or ev_toks <= 0:
            continue

        idx = date_index.get(ev_dt.astimezone().date().isoformat())
        if idx is not None:
            if ev_model not in model_daily_map:
                model_daily_map[ev_model] = [0] * days
                model_daily_cost_map[ev_model] = [0.0] * days
                model_daily_unknown_cost_tokens[ev_model] = [0] * days
            model_daily_map[ev_model][idx] += ev_toks

            ev_cost = ev.get('cost_usd')
            ev_cost_known = bool(ev.get('cost_known')) if 'cost_known' in ev else ev_cost is not None
            if ev_cost_known and ev_cost is not None:
                model_daily_cost_map[ev_model][idx] += max(0.0, float(ev_cost or 0.0))
            else:
                model_daily_unknown_cost_tokens[ev_model][idx] += ev_toks

    # Group Codex events by model and day index
    codex_auto = codex_data.get('automatic_model_usage') if isinstance(codex_data, dict) else {}
    codex_events = codex_auto.get('recent_events') if isinstance(codex_auto, dict) else []
    for cev in (codex_events or []):
        ts_str = cev.get('ts')
        cev_dt = _parse_iso_utc(ts_str)
        cev_model = cev.get('model_key') or 'Codex Model'
        cev_toks = cev.get('tot', 0)
        if not cev_dt or cev_toks <= 0:
            continue
        idx = date_index.get(cev_dt.astimezone().date().isoformat())
        if idx is not None:
            if cev_model not in model_daily_map:
                model_daily_map[cev_model] = [0] * days
                model_daily_cost_map[cev_model] = [0.0] * days
                model_daily_unknown_cost_tokens[cev_model] = [0] * days
            model_daily_map[cev_model][idx] += cev_toks

            pricing_known = model_has_cost_estimate(cev_model)
            has_cost_components = any(key in cev for key in ('in', 'out', 'cached_in', 'cache_write_in'))
            cev_cost = None
            if pricing_known and has_cost_components:
                cev_cost = estimate_model_cost(
                    cev_model,
                    input_tokens=cev.get('in', 0),
                    output_tokens=cev.get('out', 0),
                    cached_input_tokens=cev.get('cached_in', 0),
                    cache_write_input_tokens=cev.get('cache_write_in', 0),
                )
            if cev_cost is not None:
                model_daily_cost_map[cev_model][idx] += max(0.0, float(cev_cost))
            else:
                model_daily_unknown_cost_tokens[cev_model][idx] += cev_toks

    # If any model from global_models_stats has today_tokens or weekly_tokens but no step events in map, register it
    for m_name, m_stat in global_models_stats.items():
        if m_name == 'Unknown':
            continue
        if m_name not in model_daily_map and end_day == today_local:
            if m_stat.get('today_tokens', 0) > 0 or m_stat.get('weekly_tokens', 0) > 0:
                model_daily_map[m_name] = [0] * days
                model_daily_cost_map[m_name] = [0.0] * days
                model_daily_unknown_cost_tokens[m_name] = [0] * days
                if m_stat.get('today_tokens', 0) > 0:
                    model_daily_map[m_name][-1] = m_stat['today_tokens']
                    if m_stat.get('today_cost_known', model_has_cost_estimate(m_name)):
                        model_daily_cost_map[m_name][-1] = max(0.0, float(m_stat.get('today_cost_usd', 0.0) or 0.0))
                    else:
                        model_daily_unknown_cost_tokens[m_name][-1] = m_stat['today_tokens']

    sorted_models = sorted(
        model_daily_map.items(),
        key=lambda item: sum(item[1]),
        reverse=True
    )

    models_result = []
    for m_name, daily_arr in sorted_models:
        daily_cost_arr = model_daily_cost_map.get(m_name, [0.0] * days)
        daily_unknown_arr = model_daily_unknown_cost_tokens.get(m_name, [0] * days)
        unknown_cost_tokens = sum(daily_unknown_arr)
        pricing_meta = get_pricing_provenance(m_name)
        models_result.append({
            'name': m_name,
            'color': get_model_color(m_name),
            'daily_tokens': daily_arr,
            'total_period_tokens': sum(daily_arr),
            'today_tokens': daily_arr[-1] if end_day == today_local and daily_arr else 0,
            'daily_cost_usd': [round(value, 8) for value in daily_cost_arr],
            'total_period_cost_usd': round(sum(daily_cost_arr), 8),
            'daily_unknown_cost_tokens': daily_unknown_arr,
            'unknown_cost_tokens': unknown_cost_tokens,
            'cost_complete': unknown_cost_tokens == 0,
            **pricing_meta,
        })

    return {
        'dates': dates,
        'date_keys': date_keys,
        'range': {
            'mode': range_mode,
            'days': days,
            'start_date': start_day.isoformat(),
            'end_date': end_day.isoformat(),
        },
        'models': models_result
    }


# ---- Core Analysis Engine (Single-Pass Fast Scan) ----
def analyze_all_conversations(sources=None, model_timeline_days=7,
                              model_timeline_start=None, model_timeline_end=None):
    conversations = []
    sources_dict = sources if sources is not None else get_transcript_sources()

    # Resolve the active account before reading any account-scoped quota state.
    # This is especially important on first run: legacy quota data must migrate
    # to the real active account instead of being stranded under __unknown__.
    accounts_db = sync_accounts_db()
    active_email = accounts_db.get('active_email')
    active_acc = accounts_db.get('accounts', {}).get(active_email, {})

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    # Beginning of the current system-local day, expressed in UTC.
    today_utc_start = _local_day_start_utc(now_utc)
    five_hours_ago = now_utc - timedelta(hours=5)
    seven_days_ago = now_utc - timedelta(days=7)

    tool_output_types = {'LIST_DIRECTORY', 'VIEW_FILE', 'GREP_SEARCH', 'RUN_COMMAND', 'CODE_ACTION', 'GENERIC'}
    global_models_stats = {}

    # Load reset anchors for the same account used throughout this analysis pass.
    real_override = load_real_quota_profile(active_email)

    def parse_anchor(ts_str):
        if not ts_str: return None
        try:
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except Exception:
            return None

    g5_anchor = parse_anchor(real_override.get('gemini_5h_reset_anchor_ts'))
    e5_anchor = parse_anchor(real_override.get('external_5h_reset_anchor_ts'))
    gw_anchor = parse_anchor(real_override.get('gemini_weekly_reset_anchor_ts'))
    ew_anchor = parse_anchor(real_override.get('external_weekly_reset_anchor_ts'))

    # Effective starting boundary: Only count tokens from the current fresh cycle
    gem_5h_window_start = max(five_hours_ago, g5_anchor) if g5_anchor else five_hours_ago
    ext_5h_window_start = max(five_hours_ago, e5_anchor) if e5_anchor else five_hours_ago
    gem_wk_window_start = max(seven_days_ago, gw_anchor) if gw_anchor else seven_days_ago
    ext_wk_window_start = max(seven_days_ago, ew_anchor) if ew_anchor else seven_days_ago

    # Single-pass rolling window accumulators
    rolling_data = {
        'gem_5h_used': 0, 'gem_5h_reqs': 0, 'oldest_gem_5h_ts': None, 'newest_gem_5h_ts': None,
        'ext_5h_used': 0, 'ext_5h_reqs': 0, 'oldest_ext_5h_ts': None, 'newest_ext_5h_ts': None,
        'gem_wk_used': 0, 'gem_wk_reqs': 0, 'oldest_gem_wk_ts': None, 'newest_gem_wk_ts': None,
        'ext_wk_used': 0, 'ext_wk_reqs': 0, 'oldest_ext_wk_ts': None, 'newest_ext_wk_ts': None,
        'g5_anchor': g5_anchor, 'e5_anchor': e5_anchor,
    }
    worker_usage = scan_gemini_worker_usage(now=now_utc)
    worker_5h_tokens, worker_5h_runs = _worker_usage_between(
        worker_usage.get('events', []), gem_5h_window_start, now_utc)
    worker_week_tokens, worker_week_runs = _worker_usage_between(
        worker_usage.get('events', []), gem_wk_window_start, now_utc)
    rolling_data.update({
        'gem_worker_5h_used': worker_5h_tokens,
        'gem_worker_wk_used': worker_week_tokens,
        'gem_worker_5h_runs': worker_5h_runs,
        'gem_worker_wk_runs': worker_week_runs,
        'gem_worker_events': worker_usage.get('events', []),
    })
    all_step_events = []

    seen_transcripts = set()
    seen_conversation_keys = set()
    source_breakdowns = {}

    for src_key, src_info in dict(sources_dict or {}).items():
        src_label = src_info.get('label') or src_key
        src_path = src_info.get('path')
        src_format = src_info.get('format')
        source_breakdowns[src_key] = {
            'key': src_key,
            'label': src_label,
            'total_conversations': 0,
            'total_tokens': 0,
            'gemini_5h_tokens': 0,
            'gemini_weekly_tokens': 0,
            'external_5h_tokens': 0,
            'external_weekly_tokens': 0,
            'user_messages': 0,
            'model_responses': 0,
            'tool_calls': 0,
        }

        if not src_path or not os.path.exists(src_path):
            continue

        if src_format == 'gemini_cli' or src_key == 'gemini_cli':
            candidate_files = discover_gemini_cli_session_files(src_path)
            for file_path in candidate_files:
                try:
                    canonical_path = os.path.realpath(file_path)
                except Exception:
                    canonical_path = os.path.abspath(file_path)

                if canonical_path in seen_transcripts:
                    continue
                seen_transcripts.add(canonical_path)

                parsed = parse_official_gemini_cli_session(file_path)
                if not parsed or not isinstance(parsed, dict):
                    continue

                metadata = parsed.get('metadata', {})
                messages = parsed.get('messages', [])

                session_id = str(metadata.get('sessionId') or os.path.splitext(os.path.basename(file_path))[0])
                conversation_key = (src_key, session_id)
                if conversation_key in seen_conversation_keys:
                    continue
                seen_conversation_keys.add(conversation_key)

                stats = {
                    'id': session_id,
                    'short_id': session_id[:8],
                    'source': src_key,
                    'source_label': src_label,
                    'total_steps': len(messages),
                    'user_messages': 0,
                    'model_responses': 0,
                    'tool_calls': 0,
                    'tool_types': {},
                    'input_tokens_est': 0,
                    'output_tokens_est': 0,
                    'thinking_tokens_est': 0,
                    'total_content_bytes': 0,
                    'user_content_bytes': 0,
                    'model_content_bytes': 0,
                    'tool_content_bytes': 0,
                    'start_time': metadata.get('startTime'),
                    'end_time': metadata.get('lastUpdated'),
                    'duration_minutes': 0,
                    'errors': 0,
                    'checkpoints': 0,
                    'first_user_msg': '',
                    'model_name': 'Gemini 2.5 Pro',
                    'models_used': set(),
                    'estimated_cost_usd': 0.0
                }

                conv_per_model_stats = {}

                for msg in messages:
                    mtype = (msg.get('type') or msg.get('role') or '').lower()
                    text = extract_message_text(msg)
                    text_bytes = len(text.encode('utf-8'))
                    stats['total_content_bytes'] += text_bytes

                    msg_dt = extract_message_dt(msg)
                    msg_ts_iso = msg_dt.isoformat() if msg_dt else (msg.get('timestamp') or msg.get('created_at') or msg.get('time'))
                    if msg_ts_iso:
                        if stats['start_time'] is None or str(msg_ts_iso) < str(stats['start_time']):
                            stats['start_time'] = str(msg_ts_iso)
                        if stats['end_time'] is None or str(msg_ts_iso) > str(stats['end_time']):
                            stats['end_time'] = str(msg_ts_iso)

                    if mtype == 'user':
                        stats['user_messages'] += 1
                        stats['user_content_bytes'] += text_bytes
                        if not stats['first_user_msg']:
                            m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', text, re.DOTALL)
                            stats['first_user_msg'] = m.group(1).strip()[:180] if m else text.strip()[:180]
                    elif mtype == 'gemini':
                        stats['model_responses'] += 1
                        stats['model_content_bytes'] += text_bytes

                        model_str = msg.get('model') or metadata.get('model') or 'Gemini 2.5 Pro'
                        stats['models_used'].add(model_str)
                        stats['model_name'] = model_str

                        in_toks, out_toks, think_toks, total_quota_toks, has_tokens = extract_gemini_cli_message_tokens(msg)

                        stats['input_tokens_est'] += in_toks
                        stats['output_tokens_est'] += out_toks
                        stats['thinking_tokens_est'] += think_toks

                        tool_calls = msg.get('toolCalls') or msg.get('tool_calls') or []
                        n_tools = 0
                        if isinstance(tool_calls, list) and tool_calls:
                            n_tools = len(tool_calls)
                            stats['tool_calls'] += n_tools
                            for tc in tool_calls:
                                t_name = tc.get('name') if isinstance(tc, dict) else 'tool'
                                stats['tool_types'][t_name] = stats['tool_types'].get(t_name, 0) + 1

                        if model_str not in conv_per_model_stats:
                            conv_per_model_stats[model_str] = {
                                'user_messages': 0, 'model_responses': 0, 'tool_calls': 0,
                                'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_usd': 0.0,
                                'weekly_tokens': 0, 'weekly_cost_usd': 0.0, 'five_hour_tokens': 0, 'five_hour_cost_usd': 0.0,
                                'today_tokens': 0, 'today_cost_usd': 0.0
                            }

                        step_cost = estimate_model_cost(model_str, input_tokens=in_toks, output_tokens=out_toks) or 0.0
                        stats['estimated_cost_usd'] += step_cost

                        m_stat = conv_per_model_stats[model_str]
                        m_stat['model_responses'] += 1
                        m_stat['tool_calls'] += n_tools
                        m_stat['input_tokens'] += in_toks
                        m_stat['output_tokens'] += out_toks
                        m_stat['thinking_tokens'] += think_toks
                        m_stat['cost_usd'] += step_cost

                        if msg_dt:
                            is_gem = True
                            all_step_events.append({'dt': msg_dt, 'tokens': total_quota_toks, 'is_gemini': is_gem, 'model': model_str, 'cost_usd': step_cost, 'cost_known': model_has_cost_estimate(model_str)})

                            if msg_dt >= gem_wk_window_start:
                                m_stat['weekly_tokens'] += total_quota_toks
                                m_stat['weekly_cost_usd'] += step_cost
                                if has_tokens and total_quota_toks > 0:
                                    rolling_data['gem_wk_used'] += total_quota_toks
                                    rolling_data['gem_wk_reqs'] += 1
                                    if rolling_data['oldest_gem_wk_ts'] is None or msg_dt < rolling_data['oldest_gem_wk_ts']:
                                        rolling_data['oldest_gem_wk_ts'] = msg_dt
                                    if rolling_data['newest_gem_wk_ts'] is None or msg_dt > rolling_data['newest_gem_wk_ts']:
                                        rolling_data['newest_gem_wk_ts'] = msg_dt

                            if msg_dt >= gem_5h_window_start:
                                m_stat['five_hour_tokens'] += total_quota_toks
                                m_stat['five_hour_cost_usd'] += step_cost
                                if has_tokens and total_quota_toks > 0:
                                    rolling_data['gem_5h_used'] += total_quota_toks
                                    rolling_data['gem_5h_reqs'] += 1
                                    if rolling_data['oldest_gem_5h_ts'] is None or msg_dt < rolling_data['oldest_gem_5h_ts']:
                                        rolling_data['oldest_gem_5h_ts'] = msg_dt
                                    if rolling_data['newest_gem_5h_ts'] is None or msg_dt > rolling_data['newest_gem_5h_ts']:
                                        rolling_data['newest_gem_5h_ts'] = msg_dt

                if stats['start_time'] and stats['end_time']:
                    try:
                        s_dt = _parse_iso_utc(stats['start_time'])
                        e_dt = _parse_iso_utc(stats['end_time'])
                        if s_dt and e_dt:
                            stats['duration_minutes'] = round(max(0.0, (e_dt - s_dt).total_seconds()) / 60, 1)
                    except Exception:
                        pass

                stats['estimated_cost_usd'] = round(stats['estimated_cost_usd'], 4)
                stats['models_used'] = sorted(stats['models_used']) if stats['models_used'] else [stats['model_name']]

                # Merge into global_models_stats
                for m_name, m_stats in conv_per_model_stats.items():
                    if m_name not in global_models_stats:
                        global_models_stats[m_name] = {
                            'model_name': m_name,
                            'sessions_count': 0,
                            'user_messages': 0,
                            'model_responses': 0,
                            'tool_calls': 0,
                            'input_tokens': 0,
                            'output_tokens': 0,
                            'thinking_tokens': 0,
                            'total_tokens': 0,
                            'cost_usd': 0.0,
                            'weekly_tokens': 0,
                            'weekly_cost_usd': 0.0,
                            'five_hour_tokens': 0,
                            'today_tokens': 0,
                            'today_cost_usd': 0.0,
                            'duration_minutes': 0.0
                        }
                    g = global_models_stats[m_name]
                    g['sessions_count'] += 1
                    g['user_messages'] += stats['user_messages']
                    g['model_responses'] += m_stats['model_responses']
                    g['tool_calls'] += m_stats['tool_calls']
                    g['input_tokens'] += m_stats['input_tokens']
                    g['output_tokens'] += m_stats['output_tokens']
                    g['thinking_tokens'] += m_stats['thinking_tokens']
                    g['total_tokens'] += (m_stats['input_tokens'] + m_stats['output_tokens'])
                    g['cost_usd'] += m_stats['cost_usd']
                    g['weekly_tokens'] += m_stats.get('weekly_tokens', 0)
                    g['weekly_cost_usd'] += m_stats.get('weekly_cost_usd', 0.0)
                    g['five_hour_tokens'] += m_stats.get('five_hour_tokens', 0)
                    g['today_tokens'] += m_stats.get('today_tokens', 0)
                    g['today_cost_usd'] += m_stats.get('today_cost_usd', 0.0)

                primary_m = stats['model_name']
                if primary_m in global_models_stats:
                    global_models_stats[primary_m]['duration_minutes'] += stats.get('duration_minutes', 0.0)

                # Update source breakdown
                sb = source_breakdowns[src_key]
                sb['total_conversations'] += 1
                sb['total_tokens'] += (stats['input_tokens_est'] + stats['output_tokens_est'])
                sb['user_messages'] += stats['user_messages']
                sb['model_responses'] += stats['model_responses']
                sb['tool_calls'] += stats['tool_calls']
                for m_stats in conv_per_model_stats.values():
                    sb['gemini_5h_tokens'] += m_stats.get('five_hour_tokens', 0)
                    sb['gemini_weekly_tokens'] += m_stats.get('weekly_tokens', 0)

                conversations.append(stats)
            continue

        try:
            conv_dirs = os.listdir(src_path)
        except Exception:
            continue

        for conv_id in conv_dirs:
            conv_path = os.path.join(src_path, conv_id)
            if not os.path.isdir(conv_path):
                continue

            transcript_path = os.path.join(conv_path, '.system_generated', 'logs', 'transcript.jsonl')
            if not os.path.exists(transcript_path) or is_cloud_offline_file(transcript_path):
                continue

            try:
                canonical_path = os.path.realpath(transcript_path)
            except Exception:
                canonical_path = os.path.abspath(transcript_path)

            if canonical_path in seen_transcripts:
                continue
            seen_transcripts.add(canonical_path)

            stats = {
                'id': conv_id,
                'short_id': conv_id[:8],
                'source': src_key,
                'source_label': src_label,
                'total_steps': 0,
                'user_messages': 0,
                'model_responses': 0,
                'tool_calls': 0,
                'tool_types': {},
                'input_tokens_est': 0,
                'output_tokens_est': 0,
                'thinking_tokens_est': 0,
                'total_content_bytes': 0,
                'user_content_bytes': 0,
                'model_content_bytes': 0,
                'tool_content_bytes': 0,
                'start_time': None,
                'end_time': None,
                'duration_minutes': 0,
                'errors': 0,
                'checkpoints': 0,
                'first_user_msg': '',
                'model_name': 'Unknown',
                'models_used': set(),
                'estimated_cost_usd': 0.0
            }

            active_model = 'Gemini 3.5 Flash (Medium)'
            conv_per_model_stats = {}

            try:
                with open(transcript_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            step = json.loads(line)
                        except Exception:
                            continue

                        stats['total_steps'] += 1
                        step_type = step.get('type', '')
                        content = step.get('content', '') or ''
                        created = step.get('created_at', '')

                        step_dt = None
                        if created:
                            try:
                                step_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                            except Exception:
                                pass
                            if stats['start_time'] is None or created < stats['start_time']:
                                stats['start_time'] = created
                            if stats['end_time'] is None or created > stats['end_time']:
                                stats['end_time'] = created

                        if step_type == 'USER_INPUT':
                            stats['user_messages'] += 1
                            u_bytes = len(content.encode('utf-8'))
                            stats['user_content_bytes'] += u_bytes
                            step_in_toks = estimate_tokens(content)
                            stats['input_tokens_est'] += step_in_toks

                            if not stats['first_user_msg']:
                                m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', content, re.DOTALL)
                                stats['first_user_msg'] = m.group(1).strip()[:180] if m else content.strip()[:180]

                            for m_match in re.finditer(r'Model Selection.*?from\s+.*?\s+to\s+(.+?)\.\s*(?:No need|\n|<\/USER_SETTINGS_CHANGE>)', content, re.DOTALL):
                                m_name = m_match.group(1).strip()
                                if m_name and m_name.lower() != 'none':
                                    active_model = m_name
                                    stats['models_used'].add(m_name)
                                    stats['model_name'] = m_name

                            if active_model not in conv_per_model_stats:
                                conv_per_model_stats[active_model] = {
                                    'user_messages': 0, 'model_responses': 0, 'tool_calls': 0,
                                    'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_usd': 0.0,
                                    'weekly_tokens': 0, 'weekly_cost_usd': 0.0, 'five_hour_tokens': 0, 'five_hour_cost_usd': 0.0,
                                    'today_tokens': 0, 'today_cost_usd': 0.0
                                }

                            step_cost = estimate_model_cost(active_model, input_tokens=step_in_toks) or 0.0
                            stats['estimated_cost_usd'] += step_cost
                            conv_per_model_stats[active_model]['input_tokens'] += step_in_toks
                            conv_per_model_stats[active_model]['user_messages'] += 1
                            conv_per_model_stats[active_model]['cost_usd'] += step_cost

                            # Rolling Windows Single-Pass Accumulation
                            if step_dt:
                                is_gem = 'gemini' in active_model.lower()
                                all_step_events.append({'dt': step_dt, 'tokens': step_in_toks, 'is_gemini': is_gem, 'model': active_model, 'cost_usd': step_cost, 'cost_known': model_has_cost_estimate(active_model)})
                                wk_start = gem_wk_window_start if is_gem else ext_wk_window_start
                                h5_start = gem_5h_window_start if is_gem else ext_5h_window_start

                                if step_dt >= wk_start:
                                    conv_per_model_stats[active_model]['weekly_tokens'] += step_in_toks
                                    conv_per_model_stats[active_model]['weekly_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_wk_used'] += step_in_toks
                                        rolling_data['gem_wk_reqs'] += 1
                                        if rolling_data['oldest_gem_wk_ts'] is None or step_dt < rolling_data['oldest_gem_wk_ts']:
                                            rolling_data['oldest_gem_wk_ts'] = step_dt
                                        if rolling_data['newest_gem_wk_ts'] is None or step_dt > rolling_data['newest_gem_wk_ts']:
                                            rolling_data['newest_gem_wk_ts'] = step_dt
                                    else:
                                        rolling_data['ext_wk_used'] += step_in_toks
                                        rolling_data['ext_wk_reqs'] += 1
                                        if rolling_data['oldest_ext_wk_ts'] is None or step_dt < rolling_data['oldest_ext_wk_ts']:
                                            rolling_data['oldest_ext_wk_ts'] = step_dt
                                        if rolling_data['newest_ext_wk_ts'] is None or step_dt > rolling_data['newest_ext_wk_ts']:
                                            rolling_data['newest_ext_wk_ts'] = step_dt

                                if step_dt >= h5_start:
                                    conv_per_model_stats[active_model]['five_hour_tokens'] += step_in_toks
                                    conv_per_model_stats[active_model]['five_hour_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_5h_used'] += step_in_toks
                                        rolling_data['gem_5h_reqs'] += 1
                                        if rolling_data['oldest_gem_5h_ts'] is None or step_dt < rolling_data['oldest_gem_5h_ts']:
                                            rolling_data['oldest_gem_5h_ts'] = step_dt
                                        if rolling_data['newest_gem_5h_ts'] is None or step_dt > rolling_data['newest_gem_5h_ts']:
                                            rolling_data['newest_gem_5h_ts'] = step_dt
                                    else:
                                        rolling_data['ext_5h_used'] += step_in_toks
                                        rolling_data['ext_5h_reqs'] += 1
                                        if rolling_data['oldest_ext_5h_ts'] is None or step_dt < rolling_data['oldest_ext_5h_ts']:
                                            rolling_data['oldest_ext_5h_ts'] = step_dt
                                        if rolling_data['newest_ext_5h_ts'] is None or step_dt > rolling_data['newest_ext_5h_ts']:
                                            rolling_data['newest_ext_5h_ts'] = step_dt

                                if step_dt >= today_utc_start:
                                    conv_per_model_stats[active_model]['today_tokens'] += step_in_toks
                                    conv_per_model_stats[active_model]['today_cost_usd'] += step_cost

                        elif step_type == 'PLANNER_RESPONSE':
                            stats['model_responses'] += 1
                            m_bytes = len(content.encode('utf-8'))
                            stats['model_content_bytes'] += m_bytes
                            out_toks = estimate_tokens(content)

                            t_tokens = 0
                            thinking = step.get('thinking', '')
                            if thinking:
                                t_tokens = estimate_tokens(thinking)
                                stats['thinking_tokens_est'] += t_tokens
                                out_toks += t_tokens
                                stats['model_content_bytes'] += len(thinking.encode('utf-8'))

                            n_tools = 0
                            tool_calls = step.get('tool_calls', [])
                            if tool_calls:
                                n_tools = len(tool_calls)
                                stats['tool_calls'] += n_tools
                                for tc in tool_calls:
                                    t_name = tc.get('name', 'other')
                                    stats['tool_types'][t_name] = stats['tool_types'].get(t_name, 0) + 1
                                    args_str = json.dumps(tc.get('args', {}))
                                    out_toks += estimate_tokens(args_str)

                            stats['output_tokens_est'] += out_toks

                            if active_model not in conv_per_model_stats:
                                conv_per_model_stats[active_model] = {
                                    'user_messages': 0, 'model_responses': 0, 'tool_calls': 0,
                                    'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_usd': 0.0,
                                    'weekly_tokens': 0, 'weekly_cost_usd': 0.0, 'five_hour_tokens': 0, 'five_hour_cost_usd': 0.0,
                                    'today_tokens': 0, 'today_cost_usd': 0.0
                                }

                            step_cost = estimate_model_cost(active_model, output_tokens=out_toks) or 0.0
                            stats['estimated_cost_usd'] += step_cost
                            conv_per_model_stats[active_model]['output_tokens'] += out_toks
                            conv_per_model_stats[active_model]['thinking_tokens'] += t_tokens
                            conv_per_model_stats[active_model]['tool_calls'] += n_tools
                            conv_per_model_stats[active_model]['model_responses'] += 1
                            conv_per_model_stats[active_model]['cost_usd'] += step_cost

                            # Rolling Windows Single-Pass Accumulation
                            if step_dt:
                                is_gem = 'gemini' in active_model.lower()
                                all_step_events.append({'dt': step_dt, 'tokens': out_toks, 'is_gemini': is_gem, 'model': active_model, 'cost_usd': step_cost, 'cost_known': model_has_cost_estimate(active_model)})
                                wk_start = gem_wk_window_start if is_gem else ext_wk_window_start
                                h5_start = gem_5h_window_start if is_gem else ext_5h_window_start
                                if step_dt >= wk_start:
                                    conv_per_model_stats[active_model]['weekly_tokens'] += out_toks
                                    conv_per_model_stats[active_model]['weekly_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_wk_used'] += out_toks
                                        if rolling_data['oldest_gem_wk_ts'] is None or step_dt < rolling_data['oldest_gem_wk_ts']:
                                            rolling_data['oldest_gem_wk_ts'] = step_dt
                                        if rolling_data['newest_gem_wk_ts'] is None or step_dt > rolling_data['newest_gem_wk_ts']:
                                            rolling_data['newest_gem_wk_ts'] = step_dt
                                    else:
                                        rolling_data['ext_wk_used'] += out_toks
                                        if rolling_data['oldest_ext_wk_ts'] is None or step_dt < rolling_data['oldest_ext_wk_ts']:
                                            rolling_data['oldest_ext_wk_ts'] = step_dt
                                        if rolling_data['newest_ext_wk_ts'] is None or step_dt > rolling_data['newest_ext_wk_ts']:
                                            rolling_data['newest_ext_wk_ts'] = step_dt

                                if step_dt >= h5_start:
                                    conv_per_model_stats[active_model]['five_hour_tokens'] += out_toks
                                    conv_per_model_stats[active_model]['five_hour_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_5h_used'] += out_toks
                                        if rolling_data['oldest_gem_5h_ts'] is None or step_dt < rolling_data['oldest_gem_5h_ts']:
                                            rolling_data['oldest_gem_5h_ts'] = step_dt
                                        if rolling_data['newest_gem_5h_ts'] is None or step_dt > rolling_data['newest_gem_5h_ts']:
                                            rolling_data['newest_gem_5h_ts'] = step_dt
                                    else:
                                        rolling_data['ext_5h_used'] += out_toks
                                        if rolling_data['oldest_ext_5h_ts'] is None or step_dt < rolling_data['oldest_ext_5h_ts']:
                                            rolling_data['oldest_ext_5h_ts'] = step_dt
                                        if rolling_data['newest_ext_5h_ts'] is None or step_dt > rolling_data['newest_ext_5h_ts']:
                                            rolling_data['newest_ext_5h_ts'] = step_dt

                                if step_dt >= today_utc_start:
                                    conv_per_model_stats[active_model]['today_tokens'] += out_toks
                                    conv_per_model_stats[active_model]['today_cost_usd'] += step_cost

                        elif step_type in tool_output_types:
                            stats['tool_content_bytes'] += len(content.encode('utf-8'))
                            in_toks = estimate_tokens(content)
                            stats['input_tokens_est'] += in_toks

                            if active_model not in conv_per_model_stats:
                                conv_per_model_stats[active_model] = {
                                    'user_messages': 0, 'model_responses': 0, 'tool_calls': 0,
                                    'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_usd': 0.0,
                                    'weekly_tokens': 0, 'weekly_cost_usd': 0.0, 'five_hour_tokens': 0, 'five_hour_cost_usd': 0.0,
                                    'today_tokens': 0, 'today_cost_usd': 0.0
                                }
                            step_cost = estimate_model_cost(active_model, input_tokens=in_toks) or 0.0
                            stats['estimated_cost_usd'] += step_cost
                            conv_per_model_stats[active_model]['input_tokens'] += in_toks
                            conv_per_model_stats[active_model]['cost_usd'] += step_cost

                            if step_dt:
                                is_gem = 'gemini' in active_model.lower()
                                all_step_events.append({'dt': step_dt, 'tokens': in_toks, 'is_gemini': is_gem, 'model': active_model, 'cost_usd': step_cost, 'cost_known': model_has_cost_estimate(active_model)})
                                wk_start = gem_wk_window_start if is_gem else ext_wk_window_start
                                h5_start = gem_5h_window_start if is_gem else ext_5h_window_start

                                if step_dt >= wk_start:
                                    conv_per_model_stats[active_model]['weekly_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['weekly_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_wk_used'] += in_toks
                                        if rolling_data['oldest_gem_wk_ts'] is None or step_dt < rolling_data['oldest_gem_wk_ts']:
                                            rolling_data['oldest_gem_wk_ts'] = step_dt
                                        if rolling_data['newest_gem_wk_ts'] is None or step_dt > rolling_data['newest_gem_wk_ts']:
                                            rolling_data['newest_gem_wk_ts'] = step_dt
                                    else:
                                        rolling_data['ext_wk_used'] += in_toks
                                        if rolling_data['oldest_ext_wk_ts'] is None or step_dt < rolling_data['oldest_ext_wk_ts']:
                                            rolling_data['oldest_ext_wk_ts'] = step_dt
                                        if rolling_data['newest_ext_wk_ts'] is None or step_dt > rolling_data['newest_ext_wk_ts']:
                                            rolling_data['newest_ext_wk_ts'] = step_dt
                                if step_dt >= h5_start:
                                    conv_per_model_stats[active_model]['five_hour_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['five_hour_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_5h_used'] += in_toks
                                        if rolling_data['oldest_gem_5h_ts'] is None or step_dt < rolling_data['oldest_gem_5h_ts']:
                                            rolling_data['oldest_gem_5h_ts'] = step_dt
                                        if rolling_data['newest_gem_5h_ts'] is None or step_dt > rolling_data['newest_gem_5h_ts']:
                                            rolling_data['newest_gem_5h_ts'] = step_dt
                                    else:
                                        rolling_data['ext_5h_used'] += in_toks
                                        if rolling_data['oldest_ext_5h_ts'] is None or step_dt < rolling_data['oldest_ext_5h_ts']:
                                            rolling_data['oldest_ext_5h_ts'] = step_dt
                                        if rolling_data['newest_ext_5h_ts'] is None or step_dt > rolling_data['newest_ext_5h_ts']:
                                            rolling_data['newest_ext_5h_ts'] = step_dt

                                if step_dt >= today_utc_start:
                                    conv_per_model_stats[active_model]['today_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['today_cost_usd'] += step_cost

                        elif step_type == 'ERROR_MESSAGE':
                            stats['errors'] += 1

                        elif step_type in ('CHECKPOINT', 'CONVERSATION_HISTORY', 'KNOWLEDGE_ARTIFACTS', 'SYSTEM_MESSAGE'):
                            in_toks = estimate_tokens(content)
                            stats['input_tokens_est'] += in_toks

                            if active_model not in conv_per_model_stats:
                                conv_per_model_stats[active_model] = {
                                    'user_messages': 0, 'model_responses': 0, 'tool_calls': 0,
                                    'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_usd': 0.0,
                                    'weekly_tokens': 0, 'weekly_cost_usd': 0.0, 'five_hour_tokens': 0, 'five_hour_cost_usd': 0.0,
                                    'today_tokens': 0, 'today_cost_usd': 0.0
                                }
                            step_cost = estimate_model_cost(active_model, input_tokens=in_toks) or 0.0
                            stats['estimated_cost_usd'] += step_cost
                            conv_per_model_stats[active_model]['input_tokens'] += in_toks
                            conv_per_model_stats[active_model]['cost_usd'] += step_cost

                            if step_dt:
                                is_gem = 'gemini' in active_model.lower()
                                wk_start = gem_wk_window_start if is_gem else ext_wk_window_start
                                h5_start = gem_5h_window_start if is_gem else ext_5h_window_start

                                if step_dt >= wk_start:
                                    conv_per_model_stats[active_model]['weekly_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['weekly_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_wk_used'] += in_toks
                                        if rolling_data['oldest_gem_wk_ts'] is None or step_dt < rolling_data['oldest_gem_wk_ts']:
                                            rolling_data['oldest_gem_wk_ts'] = step_dt
                                        if rolling_data['newest_gem_wk_ts'] is None or step_dt > rolling_data['newest_gem_wk_ts']:
                                            rolling_data['newest_gem_wk_ts'] = step_dt
                                    else:
                                        rolling_data['ext_wk_used'] += in_toks
                                        if rolling_data['oldest_ext_wk_ts'] is None or step_dt < rolling_data['oldest_ext_wk_ts']:
                                            rolling_data['oldest_ext_wk_ts'] = step_dt
                                        if rolling_data['newest_ext_wk_ts'] is None or step_dt > rolling_data['newest_ext_wk_ts']:
                                            rolling_data['newest_ext_wk_ts'] = step_dt
                                all_step_events.append({'dt': step_dt, 'tokens': in_toks, 'is_gemini': is_gem, 'model': active_model, 'cost_usd': step_cost, 'cost_known': model_has_cost_estimate(active_model)})
                                if step_dt >= h5_start:
                                    conv_per_model_stats[active_model]['five_hour_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['five_hour_cost_usd'] += step_cost
                                    if is_gem:
                                        rolling_data['gem_5h_used'] += in_toks
                                        if rolling_data['oldest_gem_5h_ts'] is None or step_dt < rolling_data['oldest_gem_5h_ts']:
                                            rolling_data['oldest_gem_5h_ts'] = step_dt
                                        if rolling_data['newest_gem_5h_ts'] is None or step_dt > rolling_data['newest_gem_5h_ts']:
                                            rolling_data['newest_gem_5h_ts'] = step_dt
                                    else:
                                        rolling_data['ext_5h_used'] += in_toks
                                        if rolling_data['oldest_ext_5h_ts'] is None or step_dt < rolling_data['oldest_ext_5h_ts']:
                                            rolling_data['oldest_ext_5h_ts'] = step_dt
                                        if rolling_data['newest_ext_5h_ts'] is None or step_dt > rolling_data['newest_ext_5h_ts']:
                                            rolling_data['newest_ext_5h_ts'] = step_dt

                                if step_dt >= today_utc_start:
                                    conv_per_model_stats[active_model]['today_tokens'] += in_toks
                                    conv_per_model_stats[active_model]['today_cost_usd'] += step_cost

                        stats['total_content_bytes'] += len(content.encode('utf-8'))

                if stats['start_time'] and stats['end_time']:
                    try:
                        s_dt = datetime.fromisoformat(stats['start_time'].replace('Z', '+00:00'))
                        e_dt = datetime.fromisoformat(stats['end_time'].replace('Z', '+00:00'))
                        stats['duration_minutes'] = round((e_dt - s_dt).total_seconds() / 60, 1)
                    except Exception:
                        pass

                stats['estimated_cost_usd'] = round(stats['estimated_cost_usd'], 4)
                stats['model_name'] = active_model
                stats['models_used'] = list(stats['models_used']) if stats['models_used'] else [active_model]

                # Merge into global_models_stats
                for m_name, m_stats in conv_per_model_stats.items():
                    if m_name not in global_models_stats:
                        global_models_stats[m_name] = {
                            'model_name': m_name,
                            'sessions_count': 0,
                            'user_messages': 0,
                            'model_responses': 0,
                            'tool_calls': 0,
                            'input_tokens': 0,
                            'output_tokens': 0,
                            'thinking_tokens': 0,
                            'total_tokens': 0,
                            'cost_usd': 0.0,
                            'weekly_tokens': 0,
                            'weekly_cost_usd': 0.0,
                            'five_hour_tokens': 0,
                            'today_tokens': 0,
                            'today_cost_usd': 0.0,
                            'duration_minutes': 0.0
                        }
                    g = global_models_stats[m_name]
                    g['sessions_count'] += 1
                    g['user_messages'] += m_stats['user_messages']
                    g['model_responses'] += m_stats['model_responses']
                    g['tool_calls'] += m_stats['tool_calls']
                    g['input_tokens'] += m_stats['input_tokens']
                    g['output_tokens'] += m_stats['output_tokens']
                    g['thinking_tokens'] += m_stats['thinking_tokens']
                    g['total_tokens'] += (m_stats['input_tokens'] + m_stats['output_tokens'])
                    g['cost_usd'] += m_stats['cost_usd']
                    g['weekly_tokens'] += m_stats.get('weekly_tokens', 0)
                    g['weekly_cost_usd'] += m_stats.get('weekly_cost_usd', 0.0)
                    g['five_hour_tokens'] += m_stats.get('five_hour_tokens', 0)
                    g['today_tokens'] = g.get('today_tokens', 0) + m_stats.get('today_tokens', 0)
                    g['today_cost_usd'] = g.get('today_cost_usd', 0.0) + m_stats.get('today_cost_usd', 0.0)

                if active_model in global_models_stats:
                    global_models_stats[active_model]['duration_minutes'] += stats.get('duration_minutes', 0.0)

                # Update source breakdown
                sb = source_breakdowns[src_key]
                sb['total_conversations'] += 1
                sb['total_tokens'] += (stats['input_tokens_est'] + stats['output_tokens_est'])
                sb['user_messages'] += stats['user_messages']
                sb['model_responses'] += stats['model_responses']
                sb['tool_calls'] += stats['tool_calls']
                for m_name, m_stats in conv_per_model_stats.items():
                    if 'gemini' in m_name.lower():
                        sb['gemini_5h_tokens'] += m_stats.get('five_hour_tokens', 0)
                        sb['gemini_weekly_tokens'] += m_stats.get('weekly_tokens', 0)
                    else:
                        sb['external_5h_tokens'] += m_stats.get('five_hour_tokens', 0)
                        sb['external_weekly_tokens'] += m_stats.get('weekly_tokens', 0)

                conversations.append(stats)
            except Exception as e:
                print(f"Error parsing conversation {conv_id} from {src_key}: {e}")

    conversations.sort(key=lambda x: x.get('start_time') or '', reverse=True)

    source_breakdowns['gemini_worker'] = {
        'key': 'gemini_worker',
        'label': worker_usage.get('source_label'),
        'total_conversations': worker_usage.get('diagnostics', {}).get('reports_counted', 0),
        'total_tokens': worker_week_tokens,
        'gemini_5h_tokens': worker_5h_tokens,
        'gemini_weekly_tokens': worker_week_tokens,
        'external_5h_tokens': 0,
        'external_weekly_tokens': 0,
        'user_messages': 0,
        'model_responses': worker_week_runs,
        'tool_calls': 0,
        'token_unit': GEMINI_WORKER_ESTIMATOR_ID,
        'diagnostics': worker_usage.get('diagnostics', {}),
    }

    # Load Codex usage before building the leaderboard so locally observed
    # models can appear even when AA has not published a benchmark row yet.
    codex_db = load_codex_usage()
    codex_rate_limits = get_codex_rate_limits()
    codex_db['rate_limits'] = codex_rate_limits
    codex_auto_models = scan_codex_model_usage()
    if 'quota_efficiency_timeline' not in codex_auto_models:
        codex_auto_models = dict(codex_auto_models)
        codex_auto_models['quota_efficiency_timeline'] = build_codex_quota_efficiency_timeline(
            codex_auto_models.get('quota_observations') or []
        )
    if 'weekly_capacity_history' not in codex_auto_models:
        codex_auto_models = dict(codex_auto_models)
        codex_auto_models['weekly_capacity_history'] = build_codex_weekly_capacity_history(
            codex_auto_models.get('quota_observations') or []
        )
    codex_db['automatic_model_usage'] = codex_auto_models

    automatic_model_rows = codex_auto_models.get('models') or {}
    automatic_model_keys = (
        list(automatic_model_rows.keys())
        if isinstance(automatic_model_rows, dict)
        else [row.get('model_name') for row in automatic_model_rows if isinstance(row, dict)]
    )
    locally_observed_keys = {
        key for key in list(global_models_stats.keys()) + automatic_model_keys
        if isinstance(key, str) and key.strip()
    }

    # The Codex client refreshes its picker cache independently of this server.
    # Re-read it for each dashboard build so new choices appear without a code release.
    refresh_codex_runtime_models()
    aa_sync_status = prepare_aa_benchmarks()

    # Leaderboard calculation
    leaderboard = []
    # Include every AA row, every currently selectable Codex choice, and every
    # locally observed model. Unbenchmarked rows remain explicitly unranked;
    # local heuristic scores are never presented as AA measurements.
    all_model_keys = list(dict.fromkeys(
        [
            key for key, entry in BENCHMARK_DATABASE.items()
            if (entry.get('benchmark_source') == ARTIFICIAL_ANALYSIS_SOURCE or
                entry.get('selectable_in_codex'))
        ] + sorted(locally_observed_keys)
    ))

    for m_key in all_model_keys:
        bm = get_benchmark_for_model(m_key)
        is_fast_variant = m_key.lower().endswith('(fast)')
        emp = global_models_stats.get(m_key, {
            'model_name': m_key,
            'sessions_count': 0,
            'user_messages': 0,
            'model_responses': 0,
            'tool_calls': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'thinking_tokens': 0,
            'total_tokens': 0,
            'cost_usd': 0.0,
            'weekly_tokens': 0,
            'weekly_cost_usd': 0.0,
            'five_hour_tokens': 0,
            'duration_minutes': 0.0,
        })

        # Artificial Analysis has no separate measurement for the Fast service
        # tier. Keep its local usage visible without inventing an AA ranking.
        aa_metrics = (get_verified_aa_metrics(bm) if not is_fast_variant else
                      {metric: None for metric in AA_LEADERBOARD_METRICS})
        effective_pricing = get_effective_pricing(m_key) or {}
        intelligence_index = aa_metrics['intelligence_index']
        cost_per_task = aa_metrics['cost_per_task']
        val_score = aa_value_score(intelligence_index, cost_per_task)
        avg_tokens_turn = round(emp['output_tokens'] / max(1, emp['model_responses']), 0)
        avg_tools_turn = round(emp['tool_calls'] / max(1, emp['model_responses']), 1)
        thinking_pct = round((emp['thinking_tokens'] / max(1, emp['output_tokens'])) * 100, 1) if emp['output_tokens'] > 0 else 0

        leaderboard.append({
            'model_name': m_key,
            'display_name': ((bm.get('display_name') or m_key) + ' (fast)'
                             if is_fast_variant else bm.get('display_name') or m_key),
            'provider': bm.get('provider', 'Custom'),
            'intelligence_index': intelligence_index,
            'coding_score': aa_metrics['coding_score'],
            'reasoning_score': aa_metrics['reasoning_score'],
            'speed_tps': aa_metrics['speed_tps'],
            'ttft_sec': aa_metrics['ttft_sec'],
            'price_in_1m': (effective_pricing.get('price_in_1m') if is_fast_variant
                            else bm.get('price_in_1m') if bm.get('price_in_1m') is not None
                            else effective_pricing.get('price_in_1m')),
            'price_cached_in_1m': (
                effective_pricing.get('price_cached_in_1m') if is_fast_variant
                else bm.get('price_cached_in_1m') if bm.get('price_cached_in_1m') is not None
                else effective_pricing.get('price_cached_in_1m')
            ),
            'price_out_1m': (effective_pricing.get('price_out_1m') if is_fast_variant
                             else bm.get('price_out_1m') if bm.get('price_out_1m') is not None
                             else effective_pricing.get('price_out_1m')),
            'codex_credit_in_1m': (float(bm['codex_credit_in_1m']) * _codex_fast_credit_multiplier(m_key)
                                   if is_fast_variant and bm.get('codex_credit_in_1m') is not None
                                   else bm.get('codex_credit_in_1m')),
            'codex_credit_cached_in_1m': (
                float(bm['codex_credit_cached_in_1m']) * _codex_fast_credit_multiplier(m_key)
                if is_fast_variant and bm.get('codex_credit_cached_in_1m') is not None
                else bm.get('codex_credit_cached_in_1m')
            ),
            'codex_credit_out_1m': (float(bm['codex_credit_out_1m']) * _codex_fast_credit_multiplier(m_key)
                                    if is_fast_variant and bm.get('codex_credit_out_1m') is not None
                                    else bm.get('codex_credit_out_1m')),
            'codex_credit_rate_source_url': bm.get('codex_credit_rate_source_url'),
            'codex_credit_rate_as_of': bm.get('codex_credit_rate_as_of'),
            'codex_credit_rate_speed': bm.get('codex_credit_rate_speed'),
            'pricing_estimated': bool(effective_pricing.get('pricing_estimated')),
            'pricing_basis_model': effective_pricing.get('pricing_basis_model'),
            'cost_per_task': cost_per_task,
            'context_window': bm['context_window'],
            'max_input_tokens': bm.get('max_input_tokens'),
            'max_output_tokens': bm.get('max_output_tokens'),
            'model_id': bm.get('model_id'),
            'reasoning_effort': bm.get('reasoning_effort'),
            'benchmark_source': 'fast_variant_unmeasured' if is_fast_variant else bm.get('benchmark_source'),
            'benchmark_source_url': bm.get('benchmark_source_url'),
            'benchmark_as_of': bm.get('benchmark_as_of'),
            'benchmark_index_version': bm.get('benchmark_index_version'),
            'benchmark_status': bm.get('benchmark_status'),
            'benchmark_note': ('Fast has no separate Artificial Analysis result; local quota usage is measured.'
                               if is_fast_variant else bm.get('benchmark_note')),
            'family': bm.get('family'),
            'recommended': bool(bm.get('recommended')),
            'recommendation_order': bm.get('recommendation_order'),
            'decision_label': bm.get('decision_label'),
            'decision_note': bm.get('decision_note'),
            'generation_status': bm.get('generation_status', 'current'),
            'metadata_source': bm.get('metadata_source'),
            'metadata_source_url': bm.get('metadata_source_url'),
            'selectable_in_codex': bool(bm.get('selectable_in_codex')),
            'availability_source': bm.get('availability_source'),
            'availability_as_of': bm.get('availability_as_of'),
            'selection_order': bm.get('selection_order'),
            'observed_locally': m_key in locally_observed_keys,
            'badge': bm.get('badge', 'Chưa có benchmark'),
            'best_for': bm.get('best_for', 'Chưa có metadata đáng tin cậy cho model này.'),
            'value_score': val_score,
            'local_sessions': emp['sessions_count'],
            'local_responses': emp['model_responses'],
            'local_tool_calls': emp['tool_calls'],
            'local_total_tokens': emp['total_tokens'],
            'local_weekly_tokens': emp['weekly_tokens'],
            'local_cost_usd': round(emp['cost_usd'], 4),
            'local_weekly_cost_usd': round(emp.get('weekly_cost_usd', 0.0), 4),
            'local_avg_tokens_turn': avg_tokens_turn,
            'local_avg_tools_turn': avg_tools_turn,
            'local_thinking_pct': thinking_pct
        })

    sort_and_assign_aa_ranks(leaderboard)

    # Accounts & Quotas (Single-pass fast result)
    quotas_data = calculate_quotas(
        rolling_data,
        active_acc.get('limits'),
        active_email=active_email,
    )

    # Time-Series Analytics & Snapshot Persistence
    record_time_series_snapshot(quotas_data, conversations)
    time_series_analytics = build_time_series_analytics(all_step_events, quotas_data)

    summary = {
        'total_conversations': len(conversations),
        'total_user_messages': sum(c['user_messages'] for c in conversations),
        'total_model_responses': sum(c['model_responses'] for c in conversations),
        'total_tool_calls': sum(c['tool_calls'] for c in conversations),
        'total_input_tokens': sum(c['input_tokens_est'] for c in conversations),
        'total_output_tokens': sum(c['output_tokens_est'] for c in conversations),
        'total_thinking_tokens': sum(c.get('thinking_tokens_est', 0) for c in conversations),
        'total_content_bytes': sum(c['total_content_bytes'] for c in conversations),
        'total_steps': sum(c['total_steps'] for c in conversations),
        'total_errors': sum(c['errors'] for c in conversations),
        'total_estimated_cost_usd': round(sum(c['estimated_cost_usd'] for c in conversations), 3),
        'tool_usage_global': {},
        'models_distribution': {},
        'leaderboard': leaderboard,
        'aa_sync': aa_sync_status,
        'quotas': quotas_data,
        'accounts_manager': accounts_db,
        'current_account': active_acc,
        'models_breakdown': build_models_breakdown(global_models_stats, conversations, codex_db, quotas_data),
        'models_daily_timeline': build_models_daily_timeline(
            all_step_events, global_models_stats, codex_db,
            days=model_timeline_days,
            start_date=model_timeline_start,
            end_date=model_timeline_end,
        ),
        'codex_quota_efficiency': codex_auto_models.get('quota_efficiency', {
            'available': False,
            'models': [],
        }),
        'codex_usage': codex_db,
        'tracker_build': 'task-outcomes-v1',
        'time_series': time_series_analytics,
        'source_breakdown': source_breakdowns,
        'source_breakdowns': source_breakdowns,
        'sources': source_breakdowns,
        'generated_at': datetime.now().isoformat()
    }

    color_names = {name for name in global_models_stats if isinstance(name, str) and name}
    color_names.update(name for name in automatic_model_keys if isinstance(name, str) and name)
    color_names.update(model['name'] for model in summary['models_daily_timeline']['models'])
    color_names.update(
        row['model_name'] for row in leaderboard
        if isinstance(row.get('model_name'), str) and row['model_name']
    )
    for timeline_key in ('quota_efficiency_timeline', 'quota_per_task_timeline'):
        timeline = codex_auto_models.get(timeline_key) or {}
        for window in (timeline.get('windows') or {}).values():
            color_names.update(
                row['model_key'] for row in window.get('models', [])
                if isinstance(row, dict) and isinstance(row.get('model_key'), str)
            )
    summary['model_colors'] = {
        name: get_model_color(name) for name in sorted(color_names)
    }

    for c in conversations:
        for tool, count in c.get('tool_types', {}).items():
            summary['tool_usage_global'][tool] = summary['tool_usage_global'].get(tool, 0) + count
        m_name = c.get('model_name') or 'Default'
        summary['models_distribution'][m_name] = summary['models_distribution'].get(m_name, 0) + 1

    return {
        'summary': summary,
        'conversations': conversations
    }


def get_conversation_details(conv_id, source=None, sources=None):
    # Conversation ids are alphanumeric identifiers (UUIDs, hashes, or short IDs).
    # Reject separators/dot segments so the API can never escape source roots.
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(conv_id or '')):
        return {'error': 'Invalid conversation id'}

    source_key = source or 'ide'
    sources_dict = get_transcript_sources(sources)
    if source_key not in sources_dict:
        return {'error': f'Invalid source: {source_key}'}

    src_info = sources_dict[source_key]
    source_dir = src_info.get('path')
    if not source_dir or not os.path.exists(source_dir):
        return {'error': 'Conversation not found'}

    src_format = src_info.get('format')
    if src_format == 'gemini_cli' or source_key == 'gemini_cli':
        try:
            real_source_dir = os.path.realpath(source_dir)
        except Exception:
            real_source_dir = os.path.abspath(source_dir)

        candidate_files = discover_gemini_cli_session_files(source_dir)
        target_file = None
        for p in candidate_files:
            try:
                real_p = os.path.realpath(p)
                if os.path.commonpath([real_source_dir, real_p]) != real_source_dir:
                    continue
            except Exception:
                continue

            base_name = os.path.splitext(os.path.basename(p))[0]
            if base_name == conv_id:
                target_file = p
                break
            parsed = parse_official_gemini_cli_session(p)
            if parsed and parsed.get('metadata', {}).get('sessionId') == conv_id:
                target_file = p
                break

        if not target_file:
            return {'error': 'Conversation not found'}

        parsed = parse_official_gemini_cli_session(target_file)
        if not parsed or not isinstance(parsed, dict):
            return {'error': 'Conversation not found'}

        messages = parsed.get('messages', [])
        steps = []
        for idx, msg in enumerate(messages):
            mtype = (msg.get('type') or msg.get('role') or '').lower()
            text = extract_message_text(msg)
            is_user = (mtype == 'user')

            in_toks, out_toks, think_toks, total_quota_toks, has_tokens = extract_gemini_cli_message_tokens(msg)
            tool_calls = msg.get('toolCalls') or msg.get('tool_calls') or []
            msg_dt = extract_message_dt(msg)
            msg_ts_iso = msg_dt.isoformat() if msg_dt else (msg.get('timestamp') or msg.get('created_at') or '')

            step_type = 'USER_INPUT' if is_user else 'PLANNER_RESPONSE'
            preview = text.strip()[:300]
            if is_user:
                m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', text, re.DOTALL)
                if m:
                    preview = m.group(1).strip()[:300]

            steps.append({
                'step_index': idx,
                'type': step_type,
                'source': 'gemini_cli',
                'status': 'DONE',
                'created_at': msg_ts_iso,
                'tokens_est': total_quota_toks,
                'size_bytes': len(text.encode('utf-8')),
                'preview': preview,
                'tool_calls': tool_calls if isinstance(tool_calls, list) else [],
                'has_thinking': bool(think_toks > 0 or msg.get('thoughts') or msg.get('thought'))
            })

        return {
            'id': conv_id,
            'source': 'gemini_cli',
            'source_label': src_info.get('label', 'Gemini CLI (Official)'),
            'total_steps': len(steps),
            'steps': steps
        }

    try:
        real_source_dir = os.path.realpath(source_dir)
        conv_dir = os.path.join(source_dir, conv_id)
        transcript_path = os.path.join(conv_dir, '.system_generated', 'logs', 'transcript.jsonl')
        real_transcript_path = os.path.realpath(transcript_path)
        if os.path.commonpath([real_source_dir, real_transcript_path]) != real_source_dir:
            return {'error': 'Invalid conversation id'}
    except Exception:
        return {'error': 'Invalid conversation id'}

    if not os.path.exists(transcript_path):
        return {'error': 'Conversation not found'}

    steps = []
    with open(transcript_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                step_type = item.get('type', '')
                content = item.get('content', '') or ''

                step_data = {
                    'step_index': item.get('step_index', 0),
                    'type': step_type,
                    'source': item.get('source', ''),
                    'status': item.get('status', ''),
                    'created_at': item.get('created_at', ''),
                    'tokens_est': estimate_tokens(content),
                    'size_bytes': len(content.encode('utf-8')),
                    'preview': '',
                    'tool_calls': item.get('tool_calls', []),
                    'has_thinking': bool(item.get('thinking'))
                }

                if step_type == 'USER_INPUT':
                    m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', content, re.DOTALL)
                    step_data['preview'] = m.group(1).strip() if m else content.strip()[:300]
                elif step_type == 'PLANNER_RESPONSE':
                    step_data['preview'] = content.strip()[:300]
                else:
                    step_data['preview'] = content.strip()[:200]

                steps.append(step_data)
            except Exception:
                continue

    return {
        'id': conv_id,
        'source': source_key,
        'source_label': src_info.get('label', source_key),
        'total_steps': len(steps),
        'steps': steps
    }

class TrackerHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        # The dashboard is served by this same local server, so broad CORS is
        # unnecessary and would let arbitrary web pages mutate local quota data.
        origin = self.headers.get('Origin')
        allowed_origins = {
            f'http://localhost:{PORT}',
            f'http://127.0.0.1:{PORT}',
        }
        if origin in allowed_origins:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # Local dashboard assets must reflect the running server after a
        # restart. Keep API response caching semantics unchanged.
        static_path = urllib.parse.urlparse(self.path).path.lower()
        if static_path in ('', '/') or static_path.endswith(('.html', '.js', '.css')):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    @_serialize_persistence
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_len) if content_len > 0 else b''

        if parsed.path == '/api/codex-missions/review':
            try:
                body = json.loads(post_body.decode('utf-8')) if post_body else {}
                saved = update_codex_mission_review(body)
                response_bytes = json.dumps({
                    'status': 'ok',
                    'updated_at': saved.get('updated_at'),
                }, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                response_bytes = json.dumps(
                    {'status': 'error', 'message': str(exc)}, ensure_ascii=False
                ).encode('utf-8')
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)
            return

        # Switch Active Account
        if parsed.path == '/api/account/switch':
            query = urllib.parse.parse_qs(parsed.query)
            target_email = query.get('email', [''])[0]
            if target_email and os.path.exists(ACCOUNTS_FILE):
                try:
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)
                    if target_email in acc_data.get('accounts', {}):
                        acc_data['active_email'] = target_email
                        _atomic_write_json(ACCOUNTS_FILE, acc_data)
                except Exception: pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        # Update Custom Limits
        if parsed.path == '/api/account/limits':
            try:
                body = json.loads(post_body.decode('utf-8'))
                email = body.get('email')
                limits = body.get('limits')
                if email and limits and os.path.exists(ACCOUNTS_FILE):
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)
                    if email in acc_data.get('accounts', {}):
                        acc_data['accounts'][email]['limits'] = limits
                        _atomic_write_json(ACCOUNTS_FILE, acc_data)
            except Exception: pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        # Save manual IDE percentages as observations for the self-calibrating estimator.
        if parsed.path == '/api/account/real-quotas':
            try:
                body = json.loads(post_body.decode('utf-8'))
                g5_in_pct = float(body.get('gemini_5h_pct', 61.0))
                e5_in_pct = float(body.get('external_5h_pct', 100.0))
                gw_in_pct = float(body.get('gemini_weekly_pct', 93.0))
                ew_in_pct = float(body.get('external_weekly_pct', 82.0))
                for field_name, pct_value in (
                    ('gemini_5h_pct', g5_in_pct),
                    ('external_5h_pct', e5_in_pct),
                    ('gemini_weekly_pct', gw_in_pct),
                    ('external_weekly_pct', ew_in_pct),
                ):
                    if not (0.0 <= pct_value <= 100.0):
                        raise ValueError(f'{field_name} must be between 0 and 100')

                # Step 1: Snapshot RAW rolling tokens. Never feed estimated/anchored
                # used_tokens back into the estimator or it becomes self-referential.
                now_utc = datetime.now(timezone.utc)
                now_iso = now_utc.isoformat()
                analysis = analyze_all_conversations()
                q_summary = analysis.get('summary', {}).get('quotas', {})
                w5 = q_summary.get('five_hour_window', {})
                ww = q_summary.get('weekly_window', {})

                cur_g5_used = w5.get('gemini', {}).get('tracked_used_tokens', w5.get('gemini', {}).get('used_tokens', 0))
                cur_e5_used = w5.get('external', {}).get('tracked_used_tokens', w5.get('external', {}).get('used_tokens', 0))
                cur_gw_used = ww.get('gemini', {}).get('tracked_used_tokens', ww.get('gemini', {}).get('used_tokens', 0))
                cur_ew_used = ww.get('external', {}).get('tracked_used_tokens', ww.get('external', {}).get('used_tokens', 0))
                cur_g5_worker_used = w5.get('gemini', {}).get('worker_used_tokens', 0)
                cur_gw_worker_used = ww.get('gemini', {}).get('worker_used_tokens', 0)

                # Step 2: Load current account, prior capacities, and persistent
                # reset/cycle metadata. Existing account limits are priors, not truth.
                acc_data = {}
                active_email = None
                if os.path.exists(ACCOUNTS_FILE):
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)
                    active_email = acc_data.get('active_email')

                active_acc = acc_data.get('accounts', {}).get(active_email, {})
                existing_limits = active_acc.get('limits', {})
                save_data = load_real_quota_profile(active_email)

                # Step 3: Explicit reset starts a new observation cycle. We anchor
                # tracked usage at zero at "now" so no pair can cross the boundary.
                reset_flags = {
                    'gemini_5h': bool(body.get('gemini_5h_reset_cycle') or body.get('reset_5h_anchor')),
                    'external_5h': bool(body.get('external_5h_reset_cycle') or body.get('reset_e5_anchor')),
                    'gemini_weekly': bool(body.get('gemini_weekly_reset_cycle')),
                    'external_weekly': bool(body.get('external_weekly_reset_cycle')),
                }
                cycle_keys = {
                    'gemini_5h': 'gemini_5h_cycle_id',
                    'external_5h': 'external_5h_cycle_id',
                    'gemini_weekly': 'gemini_weekly_cycle_id',
                    'external_weekly': 'external_weekly_cycle_id',
                }
                reset_anchor_keys = {
                    'gemini_5h': 'gemini_5h_reset_anchor_ts',
                    'external_5h': 'external_5h_reset_anchor_ts',
                    'gemini_weekly': 'gemini_weekly_reset_anchor_ts',
                    'external_weekly': 'external_weekly_reset_anchor_ts',
                }
                for bucket, is_reset in reset_flags.items():
                    if is_reset:
                        save_data[cycle_keys[bucket]] = now_iso
                        save_data[reset_anchor_keys[bucket]] = now_iso

                # Step 4: Record all four manual readings and estimate capacity from
                # accumulated same-cycle deltas. A single reading remains only a hint.
                obs_store = load_quota_observations()
                tracked_by_bucket = {
                    'gemini_5h': 0 if reset_flags['gemini_5h'] else cur_g5_used,
                    'external_5h': 0 if reset_flags['external_5h'] else cur_e5_used,
                    'gemini_weekly': 0 if reset_flags['gemini_weekly'] else cur_gw_used,
                    'external_weekly': 0 if reset_flags['external_weekly'] else cur_ew_used,
                }
                pct_by_bucket = {
                    'gemini_5h': g5_in_pct,
                    'external_5h': e5_in_pct,
                    'gemini_weekly': gw_in_pct,
                    'external_weekly': ew_in_pct,
                }
                limit_keys = {
                    'gemini_5h': 'gemini_5h_tokens',
                    'external_5h': 'external_5h_tokens',
                    'gemini_weekly': 'gemini_weekly_tokens',
                    'external_weekly': 'external_weekly_tokens',
                }
                calibration = {}
                worker_events = scan_gemini_worker_usage(now=now_utc).get('events', [])
                for bucket in QUOTA_BUCKET_DEFAULTS:
                    cycle_id = save_data.get(cycle_keys[bucket], 'default')
                    record_quota_observation(
                        obs_store, active_email, bucket, pct_by_bucket[bucket],
                        tracked_by_bucket[bucket], cycle_id=cycle_id,
                        pct_rounding_half_width=0.5, timestamp=now_iso,
                        worker_used_tokens=(
                            0 if reset_flags[bucket] else
                            (cur_g5_worker_used if bucket == 'gemini_5h' else cur_gw_worker_used)
                        ) if bucket.startswith('gemini_') else None
                    )
                    account_obs = obs_store.get('accounts', {}).get(active_email or '__unknown__', {}).get(bucket, [])
                    calibration_obs = account_obs
                    if bucket.startswith('gemini_'):
                        calibration_obs = []
                        for raw_obs in account_obs:
                            enriched = dict(raw_obs)
                            if enriched.get('worker_used_tokens') is None:
                                enriched['worker_used_tokens'] = _worker_tokens_at(
                                    worker_events, enriched.get('timestamp'), QUOTA_BUCKET_WINDOWS[bucket])
                                enriched['worker_estimator_id'] = GEMINI_WORKER_ESTIMATOR_ID
                            calibration_obs.append(enriched)
                        # Keep the original cycle ID; worker movement is a second
                        # source in the joint fit, not a synthetic reset.
                    prior = existing_limits.get(limit_keys[bucket], QUOTA_BUCKET_DEFAULTS[bucket])
                    worker_prior = (save_data.get('gemini_worker_5h_capacity_tokens', 0)
                                    if bucket == 'gemini_5h' else
                                    save_data.get('gemini_worker_weekly_capacity_tokens', 0))
                    calibration[bucket] = estimate_quota_capacity(
                        calibration_obs, prior, QUOTA_BUCKET_WINDOWS[bucket],
                        worker_prior if bucket.startswith('gemini_') else 0
                    )
                    if bucket.startswith('gemini_'):
                        recent_candidate = estimate_recent_cycle_capacity_candidate(
                            calibration_obs, QUOTA_BUCKET_WINDOWS[bucket], worker_prior)
                        if recent_candidate:
                            calibration[bucket].update({
                                'recent_cycle_capacity_tokens': recent_candidate['capacity_tokens'],
                                'recent_cycle_confidence': recent_candidate['confidence'],
                                'recent_cycle_evidence_count': recent_candidate['evidence_count'],
                                'recent_cycle_pair_count': recent_candidate['pair_count'],
                                'recent_cycle_id': recent_candidate['cycle_id'],
                                'recent_cycle_evidence_id': recent_candidate['evidence_id'],
                                'recent_cycle_accepted': True,
                            })

                for bucket, profile_key in (
                    ('gemini_5h', 'gemini_worker_5h_capacity_tokens'),
                    ('gemini_weekly', 'gemini_worker_weekly_capacity_tokens'),
                ):
                    account_obs = obs_store.get('accounts', {}).get(active_email or '__unknown__', {}).get(bucket, [])
                    enriched_obs = []
                    for raw_obs in account_obs:
                        enriched = dict(raw_obs)
                        if enriched.get('worker_used_tokens') is None:
                            enriched['worker_used_tokens'] = _worker_tokens_at(
                                worker_events, enriched.get('timestamp'), QUOTA_BUCKET_WINDOWS[bucket])
                            enriched['worker_estimator_id'] = GEMINI_WORKER_ESTIMATOR_ID
                        enriched_obs.append(enriched)
                    worker_cal = estimate_worker_quota_capacity(
                        enriched_obs, calibration[bucket]['capacity_tokens'],
                        save_data.get(profile_key, 0), QUOTA_BUCKET_WINDOWS[bucket])
                    calibration[bucket]['worker_calibration'] = worker_cal
                    calibration[bucket]['worker_capacity_tokens'] = worker_cal.get('capacity_tokens', 0)
                    record_capacity_calibration_event(
                        obs_store, active_email, bucket, calibration[bucket], worker_cal,
                        cycle_id=save_data.get(cycle_keys[bucket], 'default'), timestamp=now_iso)
                    if worker_cal.get('accepted') or worker_cal.get('method') == 'worker_observation_pair':
                        save_data[profile_key] = worker_cal['capacity_tokens']
                save_quota_observations(obs_store)
                capacity_history = get_capacity_calibration_history(obs_store, active_email)

                # Step 5: Only promote robust learned capacities to account priors.
                # Optional explicit token limits still win immediately if supplied.
                new_limits = dict(existing_limits)
                for bucket in QUOTA_BUCKET_DEFAULTS:
                    key = limit_keys[bucket]
                    direct_val = body.get(key)
                    if direct_val is not None:
                        try:
                            direct_val = int(direct_val)
                            if direct_val > 0:
                                new_limits[key] = direct_val
                                continue
                        except Exception:
                            pass
                    cal = calibration[bucket]
                    if cal.get('method') in ('multi_observation_robust', 'multi_source_robust') and cal.get('confidence', 0) >= 0.55:
                        new_limits[key] = cal['capacity_tokens']
                    else:
                        new_limits.setdefault(key, QUOTA_BUCKET_DEFAULTS[bucket])

                if active_email and active_email in acc_data.get('accounts', {}):
                    acc_data['accounts'][active_email]['limits'] = new_limits
                    _atomic_write_json(ACCOUNTS_FILE, acc_data)

                save_data.update({
                    'gemini_5h_pct': g5_in_pct,
                    'external_5h_pct': e5_in_pct,
                    'gemini_weekly_pct': gw_in_pct,
                    'external_weekly_pct': ew_in_pct,
                    'gemini_5h_tokens_calculated': calibration['gemini_5h']['capacity_tokens'],
                    'external_5h_tokens_calculated': calibration['external_5h']['capacity_tokens'],
                    'gemini_weekly_tokens_calculated': calibration['gemini_weekly']['capacity_tokens'],
                    'external_weekly_tokens_calculated': calibration['external_weekly']['capacity_tokens'],
                    'cur_g5_used': tracked_by_bucket['gemini_5h'],
                    'cur_e5_used': tracked_by_bucket['external_5h'],
                    'cur_gw_used': tracked_by_bucket['gemini_weekly'],
                    'cur_ew_used': tracked_by_bucket['external_weekly'],
                    'cur_g5_worker_used': 0 if reset_flags['gemini_5h'] else cur_g5_worker_used,
                    'cur_gw_worker_used': 0 if reset_flags['gemini_weekly'] else cur_gw_worker_used,
                    'calibration': calibration,
                    'updated_at': now_iso,
                    'source': 'IDE Settings (Multi-observation Self-Calibrating Estimator)'
                })

                # Optional Custom Reset Timers
                g5_custom_mins = body.get('gemini_5h_reset_minutes')
                if g5_custom_mins is not None:
                    try:
                        g5_m = float(g5_custom_mins)
                        if g5_m > 0:
                            save_data['gemini_5h_reset_at'] = (now_utc + timedelta(minutes=g5_m)).isoformat()
                        else:
                            save_data['gemini_5h_reset_at'] = None
                    except Exception:
                        pass

                e5_custom_mins = body.get('external_5h_reset_minutes')
                if e5_custom_mins is not None:
                    try:
                        e5_m = float(e5_custom_mins)
                        if e5_m > 0:
                            save_data['external_5h_reset_at'] = (now_utc + timedelta(minutes=e5_m)).isoformat()
                        else:
                            save_data['external_5h_reset_at'] = None
                    except Exception:
                        pass

                # Optional Custom Weekly Reset Timers
                gw_custom_hours = body.get('gemini_weekly_reset_hours')
                if gw_custom_hours is not None:
                    try:
                        gw_h = float(gw_custom_hours)
                        if gw_h > 0:
                            save_data['gemini_weekly_reset_at'] = (now_utc + timedelta(hours=gw_h)).isoformat()
                        else:
                            save_data['gemini_weekly_reset_at'] = None
                    except Exception:
                        pass

                ew_custom_hours = body.get('external_weekly_reset_hours')
                if ew_custom_hours is not None:
                    try:
                        ew_h = float(ew_custom_hours)
                        if ew_h > 0:
                            save_data['external_weekly_reset_at'] = (now_utc + timedelta(hours=ew_h)).isoformat()
                        else:
                            save_data['external_weekly_reset_at'] = None
                    except Exception:
                        pass

                save_real_quota_profile(save_data, active_email)

                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'ok',
                    'limits': new_limits,
                    'calibration': calibration,
                    'capacity_history': capacity_history,
                    'calibration_history': capacity_history,
                    'save_data': save_data
                }).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))
                return

        # Quick Reset Quota Cycle Endpoint
        if parsed.path == '/api/account/reset-cycle':
            try:
                body = json.loads(post_body.decode('utf-8')) if post_body else {}
                target = body.get('target', 'gemini_5h')
                pct = float(body.get('remaining_pct', 90.0))
                if not (0.0 <= pct <= 100.0):
                    raise ValueError('remaining_pct must be between 0 and 100')
                now_iso = datetime.now(timezone.utc).isoformat()

                acc_data = {}
                active_email = None
                if os.path.exists(ACCOUNTS_FILE):
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)
                    active_email = acc_data.get('active_email')

                real_data = load_real_quota_profile(active_email)

                target_buckets = []
                if target in ('gemini_5h', 'all'):
                    target_buckets.append('gemini_5h')
                if target in ('external_5h', 'all'):
                    target_buckets.append('external_5h')
                if target in ('gemini_weekly', 'all_weekly'):
                    target_buckets.append('gemini_weekly')
                if target in ('external_weekly', 'all_weekly'):
                    target_buckets.append('external_weekly')
                if not target_buckets:
                    raise ValueError(f'Unsupported reset target: {target}')

                bucket_fields = {
                    'gemini_5h': ('gemini_5h_pct', 'gemini_5h_reset_anchor_ts', 'gemini_5h_cycle_id', 'cur_g5_used', 'gemini_5h_tokens_calculated', 'gemini_5h_tokens'),
                    'external_5h': ('external_5h_pct', 'external_5h_reset_anchor_ts', 'external_5h_cycle_id', 'cur_e5_used', 'external_5h_tokens_calculated', 'external_5h_tokens'),
                    'gemini_weekly': ('gemini_weekly_pct', 'gemini_weekly_reset_anchor_ts', 'gemini_weekly_cycle_id', 'cur_gw_used', 'gemini_weekly_tokens_calculated', 'gemini_weekly_tokens'),
                    'external_weekly': ('external_weekly_pct', 'external_weekly_reset_anchor_ts', 'external_weekly_cycle_id', 'cur_ew_used', 'external_weekly_tokens_calculated', 'external_weekly_tokens'),
                }
                obs_store = load_quota_observations()
                calibration = dict(real_data.get('calibration') or {})
                active_limits = acc_data.get('accounts', {}).get(active_email, {}).get('limits', {})
                reset_worker_events = scan_gemini_worker_usage(now=now_iso).get('events', [])

                for bucket in target_buckets:
                    pct_key, anchor_key, cycle_key, used_key, capacity_key, limit_key = bucket_fields[bucket]
                    real_data[pct_key] = pct
                    real_data[anchor_key] = now_iso
                    real_data[cycle_key] = now_iso
                    real_data[used_key] = 0
                    record_quota_observation(
                        obs_store, active_email, bucket, pct, 0,
                        cycle_id=now_iso, pct_rounding_half_width=0.5, timestamp=now_iso,
                        worker_used_tokens=0 if bucket.startswith('gemini_') else None
                    )
                    account_obs = obs_store.get('accounts', {}).get(active_email or '__unknown__', {}).get(bucket, [])
                    calibration_obs = account_obs
                    if bucket.startswith('gemini_'):
                        calibration_obs = []
                        for raw_obs in account_obs:
                            enriched = dict(raw_obs)
                            if enriched.get('worker_used_tokens') is None:
                                enriched['worker_used_tokens'] = _worker_tokens_at(
                                    reset_worker_events, enriched.get('timestamp'), QUOTA_BUCKET_WINDOWS[bucket])
                                enriched['worker_estimator_id'] = GEMINI_WORKER_ESTIMATOR_ID
                            calibration_obs.append(enriched)
                        # Keep the original cycle ID; worker movement is a second
                        # source in the joint fit, not a synthetic reset.
                    prior = active_limits.get(limit_key, QUOTA_BUCKET_DEFAULTS[bucket])
                    worker_prior_key = ('gemini_worker_5h_capacity_tokens' if bucket == 'gemini_5h'
                                        else 'gemini_worker_weekly_capacity_tokens')
                    cal = estimate_quota_capacity(
                        calibration_obs, prior, QUOTA_BUCKET_WINDOWS[bucket],
                        real_data.get(worker_prior_key, 0) if bucket.startswith('gemini_') else 0)
                    worker_cal = None
                    if bucket.startswith('gemini_'):
                        recent_candidate = estimate_recent_cycle_capacity_candidate(
                            calibration_obs, QUOTA_BUCKET_WINDOWS[bucket],
                            real_data.get(worker_prior_key, 0))
                        if recent_candidate:
                            cal.update({
                                'recent_cycle_capacity_tokens': recent_candidate['capacity_tokens'],
                                'recent_cycle_confidence': recent_candidate['confidence'],
                                'recent_cycle_evidence_count': recent_candidate['evidence_count'],
                                'recent_cycle_pair_count': recent_candidate['pair_count'],
                                'recent_cycle_id': recent_candidate['cycle_id'],
                                'recent_cycle_evidence_id': recent_candidate['evidence_id'],
                                'recent_cycle_accepted': True,
                            })
                        worker_cal = estimate_worker_quota_capacity(
                            calibration_obs, cal.get('capacity_tokens', prior),
                            real_data.get(worker_prior_key, 0), QUOTA_BUCKET_WINDOWS[bucket])
                        cal['worker_calibration'] = worker_cal
                        cal['worker_capacity_tokens'] = worker_cal.get('capacity_tokens', 0)
                        record_capacity_calibration_event(
                            obs_store, active_email, bucket, cal, worker_cal,
                            cycle_id=now_iso, timestamp=now_iso)
                    calibration[bucket] = cal
                    real_data[capacity_key] = cal['capacity_tokens']

                save_quota_observations(obs_store)
                real_data['calibration'] = calibration
                real_data['source'] = 'IDE Settings (Multi-observation Self-Calibrating Estimator)'

                real_data['updated_at'] = now_iso
                save_real_quota_profile(real_data, active_email)

                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok', 'message': f'Đã đặt lại chu kỳ {target} thành công!'}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))
                return

        # Add / Register New Gmail Account
        if parsed.path == '/api/account/add':
            try:
                body = json.loads(post_body.decode('utf-8'))
                email = body.get('email', '').strip().lower()
                name = body.get('name', '').strip() or email.split('@')[0]
                tier = body.get('tier', '').strip() or 'Google AI Pro'
                pic = body.get('profile_pic', '').strip() or 'https://lh3.googleusercontent.com/a/default-user'
                make_active = body.get('make_active', True)

                if email and os.path.exists(ACCOUNTS_FILE):
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)

                    now_iso = datetime.now(timezone.utc).isoformat()
                    existing = acc_data.get('accounts', {}).get(email, {})
                    acc_data.setdefault('accounts', {})[email] = {
                        'email': email,
                        'name': name,
                        'profile_pic': pic,
                        'tier': tier,
                        'first_seen': existing.get('first_seen', now_iso),
                        'last_active': now_iso,
                        'limits': existing.get('limits', {
                            'gemini_5h_tokens': 500000,
                            'external_5h_tokens': 60000,
                            'gemini_weekly_tokens': 2500000,
                            'external_weekly_tokens': 250000
                        })
                    }
                    if make_active:
                        acc_data['active_email'] = email

                    _atomic_write_json(ACCOUNTS_FILE, acc_data)
            except Exception as e:
                pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        # Delete / Remove Account
        if parsed.path == '/api/account/delete':
            try:
                body = json.loads(post_body.decode('utf-8'))
                email = body.get('email', '').strip().lower()
                if email and os.path.exists(ACCOUNTS_FILE):
                    with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                        acc_data = json.load(f)
                    if email in acc_data.get('accounts', {}):
                        del acc_data['accounts'][email]
                        if acc_data.get('active_email') == email:
                            remaining = list(acc_data.get('accounts', {}).keys())
                            acc_data['active_email'] = remaining[0] if remaining else None
                        _atomic_write_json(ACCOUNTS_FILE, acc_data)
            except Exception as e:
                pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        # ---- Codex Usage Endpoints (Supports 'add' and 'overwrite' modes) ----
        if parsed.path == '/api/codex-usage':
            try:
                body = json.loads(post_body.decode('utf-8'))
                codex_data = apply_codex_usage_update(load_codex_usage(), body)
                save_codex_usage(codex_data)
                codex_data['rate_limits'] = get_codex_rate_limits()
                codex_data['automatic_model_usage'] = scan_codex_model_usage()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                resp = json.dumps({'status': 'ok', 'data': codex_data}, ensure_ascii=False).encode('utf-8')
                self.wfile.write(resp)
                return
            except Exception as e:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'error', 'message': str(e)}).encode('utf-8'))
                return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ('/api/data', '/api/refresh'):
            query = urllib.parse.parse_qs(parsed.query)
            timeline_start = query.get('model_timeline_start', [None])[0]
            timeline_end = query.get('model_timeline_end', [None])[0]
            timeline_days_raw = query.get('model_timeline_days', ['7'])[0]
            try:
                timeline_days = None if str(timeline_days_raw).strip().lower() == 'custom' else int(timeline_days_raw)
                data = analyze_all_conversations(
                    model_timeline_days=timeline_days,
                    model_timeline_start=timeline_start,
                    model_timeline_end=timeline_end,
                )
                try:
                    _write_live_static_snapshot_once(data)
                except OSError:
                    pass  # The live API still works if the optional file export fails.
            except (TypeError, ValueError) as exc:
                response_bytes = json.dumps(
                    {'status': 'error', 'message': str(exc)},
                    ensure_ascii=False,
                ).encode('utf-8')
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(response_bytes)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(response_bytes)
                return
            response_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(response_bytes)
            return

        if parsed.path == '/api/codex-usage':
            codex_data = load_codex_usage()
            codex_data['rate_limits'] = get_codex_rate_limits()
            codex_data['automatic_model_usage'] = scan_codex_model_usage()
            response_bytes = json.dumps(codex_data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(response_bytes)
            return

        if parsed.path == '/api/timeseries':
            data = analyze_all_conversations()
            ts_data = data.get('summary', {}).get('time_series', {})
            response_bytes = json.dumps(ts_data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(response_bytes)
            return

        if parsed.path == '/api/conversation':
            query = urllib.parse.parse_qs(parsed.query)
            conv_id = query.get('id', [''])[0]
            source_key = query.get('source', [None])[0]
            if not conv_id:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'{"error":"Missing id parameter"}')
                return

            details = get_conversation_details(conv_id, source=source_key)
            status_code = 200
            if 'error' in details:
                status_code = 404 if details['error'] == 'Conversation not found' else 400
            response_bytes = json.dumps(details, ensure_ascii=False).encode('utf-8')

            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.end_headers()
            self.wfile.write(response_bytes)
            return

        super().do_GET()

import sys
try:
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

def run_server():
    # Loopback-only by default: this tracker exposes account/quota mutation APIs
    # and should not be reachable from other devices on the LAN.
    server_address = (SERVER_HOST, PORT)
    socketserver.ThreadingTCPServer.allow_reuse_address = True

    with socketserver.ThreadingTCPServer(server_address, TrackerHTTPHandler) as httpd:
        print("==================================================")
        print("[OK] Usage Tracker server is running (Multi-threaded)!")
        print(f"[URL] Open in browser: http://localhost:{PORT}")
        print(f"[API] Live endpoint: http://localhost:{PORT}/api/data")
        print("==================================================")

        # Serve immediately. The live page requests /api/data on load; a full
        # startup scan can exceed the launcher's readiness timeout and make it
        # kill an otherwise healthy server before it accepts any connections.
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


if __name__ == '__main__':
    run_server()
