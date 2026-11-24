# data-qd-212 · 管线批次界面核算

根据顺序批次计划和沿线累计体积表，把终端实收量归回各批次，输出批次短量、已经通过终端的界面位置，并逐段核对允许损耗。

它是离线数据 API，不控制泵阀，也不是库存或通用报表系统。完整字段与假设见 docs/contract.md。

## 安装与验证

Python 3.11+；运行仅标准库，构建固定 setuptools 80.9.0、wheel 0.45.1。

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python --offline -r requirements-build.lock
    uv pip install --python .venv/bin/python --offline --no-index --no-build-isolation -e .
    .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python examples/run_sample.py

样例起点 200 体积单位，终端 190；红批次完整交付 120，蓝批次交付 70、短量 10。

## 边界

本版不推断界面混合宽度、温压密度折算或中途支线收发。所有读数由调用方先统一单位，异常数据整批拒绝，不输出部分核算。
