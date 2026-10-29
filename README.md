# EnvWeaver

提供 Runtime、Verifier、Task Pool、Agent 和 Eval 接口，让 Agent 根据真实数据源编写可复用的任务生产 Pipeline。

Python 3.11+，尚未发布到 PyPI。开发期间安装到运行 Pipeline 的环境中：

```bash
pip install -e /path/to/EnvWeaver
```

- [项目简介](docs/index.md)：在 Data Pipeline、评测和训练中使用 EnvWeaver。
- 接口手册：[Runtime](docs/runtime.md) · [Verifier](docs/verifier.md) · [Task](docs/task.md) · [Agent](docs/agent.md) · [Eval](docs/evaluation.md)
- [设计说明](docs/design.md)：职责边界、任务与资产约定、验证范围。
- [资产管理](web/README.md)：Task Pool、Runtime Pool 的网页管理与 HTTP API。

项目开发使用 uv：

```bash
uv sync
uv run pytest
uv build
uv run --group docs mkdocs serve
```

接口文档从 docstring 自动生成；修改约定见 [AGENTS.md](AGENTS.md)。

文档版本与包版本一致，`dev` 为开发版，`latest` 指向最新正式版。用 mike 保存和查看本地版本：

```bash
uv run --group docs mike deploy dev
uv run --group docs mike deploy --update-aliases "$(uv version --short)" latest
uv run --group docs mike set-default latest
uv run --group docs mike serve --dev-addr 127.0.0.1:8779
```

这些命令将快照保存在本地 `gh-pages` 分支，不修改包版本。`mike serve` 查看已保存的快照；实时编辑仍使用 `mkdocs serve`。CI 在默认分支更新时归档 `dev`，在 `vX.Y.Z` 标签发布时归档对应包版本；网站托管另行配置。
