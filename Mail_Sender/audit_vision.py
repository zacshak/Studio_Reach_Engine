"""Read-only synthetic inference audit. Never reads or changes lead records."""
import json
import os
import draft_runtime as runtime


def main():
    models = json.loads(os.environ['CHECK_MODELS'])
    if not isinstance(models, list) or not 1 <= len(models) <= 20 or not all(isinstance(m, str) and 0 < len(m) < 200 for m in models):
        raise ValueError('CHECK_MODELS must contain 1-20 model IDs')
    runtime.ATTEMPTS = 1
    client, _ = runtime.configured_client()
    results = []
    for model in models:
        print(f'Auditing {model}', flush=True)
        try:
            runtime.preflight(client, model)
            result = {'model': model, 'result': 'image_and_json_passed'}
        except runtime.ProviderUnavailable as error:
            cause = error.__cause__
            message = str(cause).lower()
            if 'does not accept image' in message or 'non-text content parts' in message:
                category = 'images_rejected'
            elif getattr(cause, 'status_code', None) == 404:
                category = 'route_not_found'
            elif getattr(cause, 'status_code', None) == 429:
                category = 'rate_limited'
            else:
                category = 'unverified'
            result = {'model': model, 'result': category, 'detail': str(error)}
        results.append(result)
        print('VISION_AUDIT ' + json.dumps(result), flush=True)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\nVision audit (synthetic images only):\n\n```json\n' + json.dumps(results, indent=2) + '\n```\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
