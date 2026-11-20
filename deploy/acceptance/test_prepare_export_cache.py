"""stdlib 自检：默认 plan 不触碰文件系统，预算和参数门禁。"""
import contextlib
import io
from unittest.mock import patch
import prepare_export_cache as p


def test_plan():
    with patch('builtins.open', side_effect=AssertionError('unexpected I/O')), \
         patch.object(p.Path, 'exists', side_effect=AssertionError('unexpected I/O')), \
         contextlib.redirect_stdout(io.StringIO()):
        assert p.main([]) == 0
        assert p.main(['--plan']) == 0
    assert not p.parser().parse_args([]).execute
    assert p.budget(1, 2, 3, 4) == 10 + 22 * p.GIB
    try:
        p.budget(0, -1, 0, 0)
    except RuntimeError:
        pass
    else:
        raise AssertionError('negative budget accepted')


if __name__ == '__main__':
    test_plan()
    print('PASS: parser / zero-I/O plan / budget')
