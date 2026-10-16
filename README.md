# EnvWeaver

提供 Runtime、Agent/LLM、Verifier 和 Task Pool 接口，让 Agent 根据真实数据源编写可复用的任务生产 Pipeline。

Python 3.11+，尚未发布到 PyPI。开发期间安装到运行 Pipeline 的环境中：

```bash
pip install -e /path/to/EnvWeaver
```

- [接口用法](docs/index.md)：调用示例、参数说明、后端配置。
- [设计说明](docs/design.md)：职责边界、任务与资产约定、验证范围。

Pipeline 的复杂度、流程、运行环境和隔离方式由使用者决定。文档中的短代码只解释接口用法。

项目开发使用 uv：

```bash
uv sync
uv run pytest
uv build
uv run --group docs mkdocs serve
```

接口文档从 docstring 自动生成；修改约定见 [AGENTS.md](AGENTS.md)。
