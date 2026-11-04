from pathlib import Path

import pytest

from super_brain.models import Host
from super_brain.probe import _remote_json

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", [False, True])
async def test_probe_accepts_only_completed_remote_program(tmp_path, failure):
    known = Path(__file__).parents[2] / ".runtime/test-ssh/known_hosts"
    if not known.is_file():
        pytest.skip("未配置 SSH localhost 的测试主机密钥")
    host = Host(name="单次探测", address="localhost", known_hosts_file=str(known))
    program = "import json,sys\nprint(json.dumps({'path':json.loads(sys.argv[1])['path']}))\n"
    if failure:
        program += "raise RuntimeError('探测尚未完成')\n"
        with pytest.raises(ConnectionError, match="探测尚未完成"):
            await _remote_json(host, program, {"path": str(tmp_path)})
    else:
        result = await _remote_json(host, program, {"path": str(tmp_path)})
        assert result == {"path": str(tmp_path)}
