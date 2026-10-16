"""Validate complete local review exports before publication."""
import os
import json
from PIL import Image

SPRITE_NAME = 'SpriteSheet_Screenshots.png'


def validate_export(folder, require_marker=True, appid=None):
    if require_marker and not os.path.isfile(os.path.join(folder, '.complete')):
        raise ValueError('media export is incomplete: completion marker missing')
    if appid is not None:
        with open(os.path.join(folder, f'{appid}.json'), encoding='utf-8') as source:
            if not isinstance(json.load(source), dict):
                raise ValueError('media export metadata must be a JSON object')
    images = [name for name in os.listdir(folder) if name.lower().endswith(('.png', '.jpg', '.jpeg'))]
    if SPRITE_NAME not in images:
        raise ValueError('media export is incomplete: sprite sheet missing')
    if not any(name != SPRITE_NAME for name in images):
        raise ValueError('media export is incomplete: source screenshots missing')
    for name in images:
        path = os.path.join(folder, name)
        with Image.open(path) as image:
            if name == SPRITE_NAME and image.format != 'PNG':
                raise ValueError('sprite sheet must be an actual PNG image')
            image.verify()
        with Image.open(path) as image:
            image.load()  # verify alone does not decode all JPEG pixel data.
    return images
