<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://potatoqi.github.io/EnvWeaver/0.0.1/assets/envweaver-logo-dark.png">
    <img src="https://potatoqi.github.io/EnvWeaver/0.0.1/assets/envweaver-logo.png" alt="EnvWeaver Logo" width="96" height="96">
  </picture>
</p>

<h1 align="center">EnvWeaver</h1>

EnvWeaver 是一个 Python 包，将不同数据来源的 Agent 任务、运行环境和评分方式统一保存，方便复用同一套流程生产任务、评测 Agent，并为训练准备数据。网页面板可管理任务和环境、查看 Agent 的执行轨迹。

以下命令在仓库根目录运行，需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。首次运行会自动安装所需依赖。

## 文档面板

[在线文档](https://potatoqi.github.io/EnvWeaver/)提供版本切换。本地实时预览：

```bash
uv run --group docs mkdocs serve
```

打开 <http://127.0.0.1:8000>，查看项目介绍和接口用法。

## 管理面板

```bash
uv run --extra management python -m envweaver.management --port 8788
```

打开 <http://127.0.0.1:8788>，在 Task Pool、Runtime Pool 或 Traj Pool 页签中点击“切换目录”，选择要查看的数据目录。
