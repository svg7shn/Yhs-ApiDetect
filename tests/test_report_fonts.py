import io
import os
from pathlib import Path

import pytest
from PIL import Image

from web import image_report as report


@pytest.mark.parametrize('bold', [False, True])
def test_windows_cjk_font(bold):
    if os.name != 'nt' or not (report._WINDOWS_FONTS / 'msyh.ttc').exists():
        pytest.skip('Windows Microsoft YaHei required')
    font = report._load_font(32, bold)
    assert bytes(font.getmask('中')) != bytes(font.getmask('文'))
    assert font.size == 32
    assert Path(font.path).parent == report._WINDOWS_FONTS


@pytest.mark.parametrize('protocol', ['anthropic', 'openai', 'gemini'])
def test_report_jpeg(protocol):
    image = Image.open(io.BytesIO(report.render_report_jpg({'protocol': protocol})))
    assert image.format == 'JPEG'
    assert image.size == (1400, 1000)
    image.load()
