"""Read the official Artificial Analysis Free Data API for leaderboard metrics.

The API key is supplied by the local server; this module never sends it to the
browser or stores it in the benchmark snapshot.
"""

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone


AA_FREE_MODELS_URL = 'https://artificialanalysis.ai/api/v2/language/models/free'
AA_DATA_API_DOCS_URL = 'https://artificialanalysis.ai/data-api/docs'
_GPT_FAMILY_NAME = re.compile(
    r'^GPT-(\d+(?:\.\d+)?)\s+(Astra|Sol|Terra|Luna)\s+'
    r'\((Non-reasoning|none|low|medium|high|xhigh|max|ultra)\)$', re.I
)
_GPT_SIMPLE_NAME = re.compile(
    r'^GPT-(\d+(?:\.\d+)?)\s+\((Non-reasoning|none|low|medium|high|xhigh|max|ultra)\)$', re.I
)


def canonical_model_key(model):
    """Map only exact AA effort variants to the tracker's compound identity."""
    if not isinstance(model, dict):
        return None
    name = str(model.get('name') or '').strip()
    creator = str((model.get('model_creator') or {}).get('name') or '').casefold()
    family = _GPT_FAMILY_NAME.fullmatch(name)
    if family and creator == 'openai':
        version, family_name, effort = family.groups()
        effort = {'medium': 'standard', 'non-reasoning': 'none'}.get(effort.casefold(), effort.casefold())
        return (f'5.6 {family_name.casefold()} {effort}' if version == '5.6'
                else f'gpt-{version}-{family_name.casefold()} {effort}')
    simple = _GPT_SIMPLE_NAME.fullmatch(name)
    if simple and creator == 'openai':
        version, effort = simple.groups()
        effort = {'medium': 'standard', 'non-reasoning': 'none'}.get(effort.casefold(), effort.casefold())
        return f'gpt-{version} {effort}'
    if creator == 'google' and re.fullmatch(r'Gemini \d+(?:\.\d+)? Flash \((?:High|Medium|Low)\)', name):
        return name
    return None


def _finite_number(value, maximum):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def parse_free_model(model):
    key = canonical_model_key(model)
    if not key:
        return None
    evaluations = model.get('evaluations') or {}
    performance = model.get('performance') or {}
    cost = model.get('artificial_analysis_intelligence_index_cost') or {}
    per_task = cost.get('cost_per_task') or {}
    intelligence = _finite_number(evaluations.get('artificial_analysis_intelligence_index'), 100)
    if intelligence is None:
        return None
    slug = str(model.get('slug') or '')
    source_url = (f'https://artificialanalysis.ai/models/{slug}'
                  if re.fullmatch(r'[a-z0-9-]+', slug) else AA_DATA_API_DOCS_URL)
    return key, {
        'intelligence_index': intelligence,
        'coding_score': _finite_number(evaluations.get('artificial_analysis_coding_index'), 100),
        'speed_tps': _finite_number(performance.get('median_output_tokens_per_second'), 2000),
        'ttft_sec': _finite_number(performance.get('median_time_to_first_token_seconds'), 1000),
        'cost_per_task': _finite_number(per_task.get('total_cost'), 1000),
        'source_url': source_url,
        'benchmark_status': 'published',
    }


def fetch_free_models(api_key, opener=urllib.request.urlopen, max_pages=20):
    """Fetch all pages, rejecting incomplete or malformed results atomically."""
    if not api_key:
        raise ValueError('Artificial Analysis API key is missing')
    models = {}
    version = None
    page = 1
    while page <= max_pages:
        url = f'{AA_FREE_MODELS_URL}?{urllib.parse.urlencode({"page": page})}'
        request = urllib.request.Request(
            url, headers={'x-api-key': api_key, 'Accept': 'application/json',
                          'User-Agent': 'UsageTracker/1.0'}, method='GET'
        )
        with opener(request, timeout=15) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise ValueError('Artificial Analysis returned an invalid model list')
        pagination = payload.get('pagination') or {}
        if pagination.get('page') != page:
            raise ValueError('Artificial Analysis pagination changed unexpectedly')
        if not isinstance(pagination.get('has_more'), bool):
            raise ValueError('Artificial Analysis pagination is incomplete')
        current_version = payload.get('intelligence_index_version')
        if current_version is not None:
            if version is not None and str(current_version) != str(version):
                raise ValueError('Artificial Analysis index version changed mid-fetch')
            version = current_version
        for model in payload['data']:
            parsed = parse_free_model(model)
            if parsed:
                key, metrics = parsed
                models[key] = metrics
        if not pagination.get('has_more'):
            break
        page += 1
    else:
        raise ValueError('Artificial Analysis pagination exceeded the safety limit')
    if not models:
        raise ValueError('Artificial Analysis returned no supported model variants')
    return {
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'index_version': str(version) if version is not None else None,
        'models': models,
    }
